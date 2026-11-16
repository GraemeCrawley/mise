"""Web push: key generation, subscribe/unsubscribe, the test catalog, sending
(with the real HTTP call stubbed), gone-subscription cleanup and the ack trail."""

from __future__ import annotations

import json

import pytest
from pywebpush import WebPushException
from sqlmodel import select

from mise.models import PushEvent, PushSubscription
from mise.services import push as push_service

SUB = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "pk", "auth": "au"}}


@pytest.fixture(autouse=True)
def _keyfile(tmp_path, monkeypatch):
    monkeypatch.setenv("VAPID_KEY_FILE", str(tmp_path / "vapid.pem"))


@pytest.fixture
def sent(monkeypatch):
    calls = []

    class Res:
        status_code = 201

    def fake(info, data=None, **kw):
        calls.append({"endpoint": info["endpoint"], "data": json.loads(data), **kw})
        return Res()

    monkeypatch.setattr(push_service, "webpush", fake)
    return calls


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(push_service, "_timer", lambda delay, fn: fn())


def _subscribe(client, endpoint=SUB["endpoint"], label="Kitchen"):
    sub = {**SUB, "endpoint": endpoint}
    r = client.post("/push/subscribe", json={"subscription": sub, "label": label,
                                             "origin": "https://example.test"})
    assert r.status_code == 200
    return r.json()


def test_public_key_is_generated_once_and_stable(client, tmp_path):
    k1 = client.get("/push/key").json()["publicKey"]
    k2 = client.get("/push/key").json()["publicKey"]
    assert k1 == k2 and len(k1) == 87  # 65-byte uncompressed point, base64url
    assert (tmp_path / "vapid.pem").exists()


def test_subscribe_is_idempotent_by_endpoint(client, session):
    _subscribe(client)
    _subscribe(client, label="Renamed")
    rows = session.exec(select(PushSubscription)).all()
    assert len(rows) == 1 and rows[0].label == "Renamed"


def test_subscribe_rejects_incomplete(client):
    r = client.post("/push/subscribe", json={"subscription": {"endpoint": "http://x"}})
    assert r.status_code == 400


def test_every_test_kind_builds(session):
    sub = PushSubscription(endpoint="https://e", p256dh="a", auth="b", origin="https://o")
    for k in push_service.TEST_KINDS:
        if k.local:
            with pytest.raises(ValueError):
                push_service.build_payloads(k.key, sub, "t1")
            continue
        parts = push_service.build_payloads(k.key, sub, "t1")
        assert parts
        for _, payload in parts:
            url = payload.get("url") or payload["notification"]["navigate"]
            assert url.startswith("https://o") and "ntest=t1" in url


def test_declarative_shape(session):
    sub = PushSubscription(endpoint="https://e", p256dh="a", auth="b", origin="https://o")
    [(_, msg)] = push_service.build_payloads("declarative_mutable", sub, "t1")
    assert msg["web_push"] == 8030 and msg["mutable"] is True
    assert set(msg["notification"]) == {"title", "body", "navigate"}


def test_send_plain_to_this_phone(client, session, sent):
    _subscribe(client)
    r = client.post("/push/test", json={"kind": "plain", "endpoint": SUB["endpoint"]})
    assert r.status_code == 200
    assert len(sent) == 1 and sent[0]["vapid_claims"]["sub"] == "https://example.test"
    stages = [e.stage for e in session.exec(select(PushEvent)).all()]
    assert stages == ["scheduled", "sent"]
    assert "in 2 s" in session.exec(select(PushEvent)).first().detail


def test_send_to_all_and_urgency(client, sent):
    _subscribe(client)
    _subscribe(client, endpoint="https://web.push.apple.com/def", label="Other")
    r = client.post("/push/test", json={"kind": "urgent", "target": "all"})
    assert r.json()["devices"] == 2
    assert all(c["headers"]["Urgency"] == "high" for c in sent)
    assert all("urgency" not in c["data"] for c in sent)


def test_delayed_parts_are_scheduled(session, sent):
    sub = push_service.subscribe(session, SUB["endpoint"], "pk", "au")
    scheduled = []
    push_service.run_test(session, "replace", [sub], schedule=lambda d, fn: scheduled.append((d, fn)))
    assert len(sent) == 1 and scheduled[0][0] == 5
    scheduled[0][1]()  # fire the timer by hand
    assert len(sent) == 2 and sent[1]["data"]["tag"] == sent[0]["data"]["tag"]


def test_no_phone_gives_clear_error(client):
    r = client.post("/push/test", json={"kind": "plain", "endpoint": "https://nope"})
    assert r.status_code == 400 and "turned on" in r.json()["detail"]


def test_gone_subscription_is_removed(client, session, monkeypatch):
    _subscribe(client)

    class Res:
        status_code = 410
        text = "gone"

    def boom(*a, **kw):
        raise WebPushException("gone", response=Res())

    monkeypatch.setattr(push_service, "webpush", boom)
    client.post("/push/test", json={"kind": "plain", "endpoint": SUB["endpoint"]})
    assert session.exec(select(PushSubscription)).all() == []
    ev = session.exec(select(PushEvent).order_by(PushEvent.id.desc())).first()
    assert ev.stage == "send_failed" and ev.detail.startswith("410")


def test_ack_trail_and_events(client, sent):
    _subscribe(client)
    tid = client.post("/push/test", json={"kind": "link", "endpoint": SUB["endpoint"]}).json()["test_id"]
    for stage in ("received", "opened", "report"):
        assert client.post("/push/ack", json={"test_id": tid, "kind": "link", "stage": stage,
                                               "endpoint": SUB["endpoint"], "detail": "x"}).status_code == 200
    assert client.post("/push/ack", json={"test_id": tid, "stage": "bogus"}).status_code == 400
    [t] = client.get("/push/events").json()
    assert [s["stage"] for s in t["trail"]] == ["scheduled", "sent", "received", "opened", "report"]
    assert t["trail"][2]["device"] == "Kitchen"


def test_test_page_renders(client):
    r = client.get("/app/notify-test")
    assert r.status_code == 200
    for k in push_service.TEST_KINDS:
        assert f'data-kind="{k.key}"' in r.text


def test_test_page_script_parses(client, tmp_path):
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = re.findall(r"<script>(.*?)</script>", client.get("/app/notify-test").text, re.S)[-1]
    f = tmp_path / "page.js"
    f.write_text(js)
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_vapid_sub_is_a_bare_origin(monkeypatch):
    from py_vapid import Vapid
    monkeypatch.delenv("VAPID_SUBJECT", raising=False)
    for origin, want in [("https://a.example", "https://a.example"), ("http://127.0.0.1:8000", "https://localhost"),
                         (None, "https://localhost")]:
        got = push_service._vapid_sub(PushSubscription(endpoint="https://e", p256dh="a", auth="b", origin=origin))
        assert got == want
        v = Vapid()
        v.generate_keys()
        v.sign({"sub": got, "aud": "https://push.example"})  # raises on a malformed sub


def test_badge_clear_variants(session):
    sub = PushSubscription(endpoint="https://e", p256dh="a", auth="b", origin="https://o")
    [(_, a)] = push_service.build_payloads("clear_badge", sub, "t1")
    [(_, b)] = push_service.build_payloads("clear_badge_zero", sub, "t1")
    [(_, c)] = push_service.build_payloads("declarative_clear", sub, "t1")
    assert a["badge"] == 0 and "badge_zero" not in a
    assert b["badge"] == 0 and b["badge_zero"] is True
    assert c["notification"]["app_badge"] == "0"
