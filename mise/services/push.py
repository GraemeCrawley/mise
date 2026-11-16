"""Web push: VAPID keys, subscriptions, sending, and the test catalog.

Keys live next to the SQLite file (the /data volume in the container) and are
generated on first use, so a fresh install needs no setup and nothing secret is
ever in the repo. Payloads are JSON the Mise service worker understands; the
`declarative*` kinds use Apple's iOS 18.4 Declarative Web Push shape instead,
which iOS can show without running the service worker.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush
from sqlmodel import Session, select

from mise.models import PushEvent, PushSubscription

PREFIX = os.environ.get("MISE_URL_PREFIX", "")
# The VAPID "sub" claim must be mailto: or a bare https origin (no path). The
# app's own origin works with Apple (proven by the budget app's trial) and keeps
# any personal email out of push requests. VAPID_SUBJECT overrides.
_FALLBACK_SUB = "https://localhost"


def _vapid_sub(sub: PushSubscription) -> str:
    explicit = os.environ.get("VAPID_SUBJECT")
    if explicit:
        return explicit
    origin = (sub.origin or "").rstrip("/")
    return origin if origin.startswith("https://") and origin.count("/") == 2 else _FALLBACK_SUB
TTL_SECONDS = 600
_key_lock = threading.Lock()


def _key_path() -> Path:
    explicit = os.environ.get("VAPID_KEY_FILE")
    if explicit:
        return Path(explicit)
    db_url = os.environ.get("DATABASE_URL", "sqlite:///./mise.db")
    if db_url.startswith("sqlite:///") and ":memory:" not in db_url:
        return Path(db_url.removeprefix("sqlite:///")).parent / "vapid_private.pem"
    return Path("vapid_private.pem")


def _vapid() -> Vapid:
    path = _key_path()
    with _key_lock:
        if not path.exists():
            v = Vapid()
            v.generate_keys()
            path.parent.mkdir(parents=True, exist_ok=True)
            v.save_key(str(path))
            os.chmod(path, 0o600)
        return Vapid.from_file(str(path))


def public_key() -> str:
    """Application server key for pushManager.subscribe (base64url, raw point)."""
    raw = _vapid().public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ---------------------------------------------------------------- subscriptions

def subscribe(db: Session, endpoint: str, p256dh: str, auth: str, *,
              label: str | None = None, origin: str | None = None,
              user_agent: str | None = None) -> PushSubscription:
    if not endpoint.startswith("https://") or not p256dh or not auth:
        raise ValueError("Incomplete push subscription")
    sub = db.exec(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()
    if sub is None:
        sub = PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth)
    sub.p256dh, sub.auth = p256dh, auth
    if label:
        sub.label = label.strip()[:60]
    sub.origin = origin or sub.origin
    sub.user_agent = (user_agent or sub.user_agent or "")[:300] or None
    sub.last_error = None
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def unsubscribe(db: Session, endpoint: str) -> bool:
    sub = db.exec(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()
    if sub is None:
        return False
    db.delete(sub)
    db.commit()
    return True


def log_event(db: Session, test_id: str, kind: str, stage: str,
              subscription_id: int | None = None, detail: str | None = None) -> None:
    db.add(PushEvent(test_id=test_id[:40], kind=kind[:40], stage=stage[:20],
                     subscription_id=subscription_id, detail=(detail or None) and detail[:500]))
    db.commit()


# ---------------------------------------------------------------- sending

def send(db: Session, sub: PushSubscription, payload: dict, *, test_id: str, kind: str,
         urgency: str = "normal") -> bool:
    """Deliver one payload. Gone subscriptions (404/410) are deleted."""
    info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
    try:
        res = webpush(info, data=json.dumps(payload), vapid_private_key=_vapid(),
                      vapid_claims={"sub": _vapid_sub(sub)}, ttl=TTL_SECONDS,
                      headers={"Urgency": urgency}, timeout=10)
        status = getattr(res, "status_code", 0)
        sub.last_ok_at, sub.last_error = datetime.now(UTC), None
        db.add(sub)
        db.commit()
        log_event(db, test_id, kind, "sent", sub.id, f"push service answered {status}")
        return True
    except WebPushException as e:
        status = e.response.status_code if e.response is not None else None
        body = (e.response.text[:200] if e.response is not None else str(e))
        detail = f"{status}: {body}" if status else str(e)[:300]
        log_event(db, test_id, kind, "send_failed", sub.id, detail)
        if status in (404, 410):
            db.delete(sub)
        else:
            sub.last_error = detail
            db.add(sub)
        db.commit()
        return False
    except Exception as e:  # network, bad key — never take the request down
        log_event(db, test_id, kind, "send_failed", sub.id, f"{type(e).__name__}: {e}"[:300])
        return False


# ---------------------------------------------------------------- test catalog

@dataclass(frozen=True)
class TestKind:
    key: str
    label: str
    look_for: str
    group: str = "other"
    local: bool = False  # runs on the page itself, no push sent


_HOME = " Then go to the home screen and look at the Mise icon."
TEST_KINDS: tuple[TestKind, ...] = (
    # Red number on the icon: set one, then try each way of clearing it.
    TestKind("badge", "Set 3 by notification",
             "A notification arrives and sets a red 3." + _HOME, "badge"),
    TestKind("clear_badge", "Clear by notification (A)",
             "A notification arrives and clears the number (clearAppBadge)." + _HOME, "badge"),
    TestKind("clear_badge_zero", "Clear by notification (B)",
             "Same, but sets the number to 0 instead (setAppBadge 0)." + _HOME, "badge"),
    TestKind("declarative_badge", "Set 5, iOS 18.4 format",
             "Apple's newer format sets a red 5." + _HOME, "badge"),
    TestKind("declarative_clear", "Clear, iOS 18.4 format",
             "Apple's newer format sets the number to 0." + _HOME, "badge"),
    TestKind("page_set", "Set 7 from the app",
             "No notification. The app sets a red 7 right away." + _HOME, "badge", local=True),
    TestKind("page_clear", "Clear from the app (A)",
             "No notification. The app clears the number (clearAppBadge)." + _HOME, "badge", local=True),
    TestKind("page_zero", "Clear from the app (B)",
             "No notification. The app sets the number to 0 (setAppBadge 0)." + _HOME, "badge",
             local=True),
    TestKind("plain", "Plain", "A notification from Mise with a title and one line of text."),
    TestKind("link", "Opens a page",
             "Tap it. This page should open with a green 'Opened from a notification' banner."),
    TestKind("actions", "Buttons",
             "Long-press it. Look for 'Done' and 'Tomorrow' buttons under the text."),
    TestKind("replace", "Replace",
             "Two arrive 5 seconds apart. The second should replace the first, leaving one."),
    TestKind("stack", "Three at once", "Three arrive together. See whether they stack under Mise."),
    TestKind("silent", "Silent", "Arrives with no sound or vibration."),
    TestKind("image", "Picture", "Long-press it. Look for a picture of an orange bowl."),
    TestKind("long", "Long text", "Long-press it to see whether the full text shows."),
    TestKind("delayed", "Locked phone (1 min)",
             "Lock the phone right after tapping. It should light up within about a minute."),
    TestKind("urgent", "High urgency",
             "Sent marked urgent. Compare with Plain: does it arrive faster or louder?"),
    TestKind("declarative", "iOS 18.4 format",
             "Apple's newer format that iOS shows without Mise's background script. Tap it: "
             "this page should open with the green banner."),
    TestKind("declarative_mutable", "iOS 18.4 format, edited",
             "Same newer format, but Mise's background script rewrites the title to start "
             "with '✏️'. Shows whether both work together."),
)
_KINDS = {k.key: k for k in TEST_KINDS}


def _tap_url(sub: PushSubscription, test_id: str, kind: str) -> str:
    path = f"{PREFIX}/app/notify-test?ntest={test_id}&kind={kind}"
    return f"{sub.origin}{path}" if sub.origin else path


def build_payloads(kind: str, sub: PushSubscription, test_id: str) -> list[tuple[float, dict]]:
    """(delay_seconds, payload) pairs for one test on one device."""
    if kind not in _KINDS:
        raise ValueError(f"Unknown test: {kind}")
    if _KINDS[kind].local:
        raise ValueError("That test runs in the app, not by notification.")
    url = _tap_url(sub, test_id, kind)
    base = {"title": f"Mise test: {_KINDS[kind].label}", "body": "", "url": url,
            "test_id": test_id, "kind": kind, "tag": f"mise-test-{test_id}"}

    def p(**kw) -> dict:
        return {**base, **kw}

    if kind == "plain":
        return [(0, p(body="If you can read this, notifications work on this phone."))]
    if kind == "link":
        return [(0, p(body="Tap to open the test page."))]
    if kind == "actions":
        return [(0, p(body="Long-press for buttons.",
                      actions=[{"action": "done", "title": "Done"},
                               {"action": "tomorrow", "title": "Tomorrow"}]))]
    if kind == "badge":
        return [(0, p(body="Check the Mise icon on your home screen.", badge=3))]
    if kind == "clear_badge":
        return [(0, p(body="The badge should be gone now.", badge=0))]
    if kind == "clear_badge_zero":
        return [(0, p(body="The badge should be gone now.", badge=0, badge_zero=True))]
    if kind == "replace":
        tag = f"mise-replace-{test_id}"
        return [(0, p(title="Mise test: Replace (1 of 2)", body="Wait 5 seconds…", tag=tag)),
                (5, p(title="Mise test: Replace (2 of 2)", body="This should have replaced 1 of 2.",
                      tag=tag, renotify=True))]
    if kind == "stack":
        return [(0, p(title=f"Mise test: Stack {i} of 3", body="Nothing planned for Friday dinner.",
                      tag=f"mise-stack-{test_id}-{i}")) for i in (1, 2, 3)]
    if kind == "silent":
        return [(0, p(body="This one should be quiet.", silent=True))]
    if kind == "image":
        img = f"{sub.origin or ''}{PREFIX}/static/notify-test.png"
        return [(0, p(body="Long-press to look for a picture.", image=img))]
    if kind == "long":
        return [(0, p(body=("Prep day tomorrow: Coconut Thai Curry for lunches and Turkey Chili for "
                            "dinners. 23 items on the shopping list, 6 already ticked off. Chili takes "
                            "about 55 minutes and the curry about 40, so starting by 4:30 gets dinner "
                            "on the table by 6. End of the long text test.")))]
    if kind == "delayed":
        return [(60, p(body="Sent one minute after you tapped. Did the locked phone light up?"))]
    if kind == "urgent":
        return [(0, p(body="Marked high urgency.", urgency="high"))]
    if kind in ("declarative", "declarative_mutable", "declarative_badge", "declarative_clear"):
        msg: dict = {"web_push": 8030, "notification": {
            "title": base["title"], "body": "Tap to open the test page.", "navigate": url}}
        if kind == "declarative_mutable":
            msg["mutable"] = True
        if kind == "declarative_badge":
            msg["notification"]["app_badge"] = "5"
        if kind == "declarative_clear":
            msg["notification"]["app_badge"] = "0"
        return [(0, msg)]
    raise ValueError(kind)


# Gap between tapping Send and the push leaving, so the phone can get to the
# home screen first (iOS hides banners for the app that's open).
TEST_LEAD_SECONDS = 2


def run_test(db: Session, kind: str, subs: list[PushSubscription], *,
             schedule=None, lead: float = 0) -> str:
    """Send one test to each device. Delayed parts go to `schedule(delay, fn)`
    (a background timer in production; tests pass their own)."""
    test_id = secrets.token_hex(4)
    bind = db.get_bind()
    for sub in subs:
        for delay, payload in build_payloads(kind, sub, test_id):
            delay += lead
            urgency = payload.pop("urgency", "normal") if "web_push" not in payload else "normal"
            if delay <= 0:
                send(db, sub, payload, test_id=test_id, kind=kind, urgency=urgency)
                continue
            sub_id = sub.id

            def later(payload=payload, urgency=urgency, sub_id=sub_id) -> None:
                # Timers fire together (3-at-once, two phones); SQLite allows one writer.
                with _send_lock, Session(bind) as s:
                    target = s.get(PushSubscription, sub_id)
                    if target is not None:
                        send(s, target, payload, test_id=test_id, kind=kind, urgency=urgency)

            log_event(db, test_id, kind, "scheduled", sub.id, f"sending in {int(delay)} s")
            (schedule or _timer)(delay, later)
    return test_id


_send_lock = threading.Lock()


def _timer(delay: float, fn) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
