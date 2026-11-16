"""Web push endpoints + the notification test page (/app/notify-test).

The page turns notifications on for the phone it's opened on, fires every kind
in the test catalog, and shows the delivery trail the service worker reports
back (received → clicked → opened) next to what the person says they saw.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from mise.adapters.rest.app import PREFIX, _h, _shell
from mise.db import get_session
from mise.models import PushEvent, PushSubscription
from mise.services import push as push_service

router = APIRouter(tags=["push"])

_ACK_STAGES = {"received", "clicked", "opened", "report"}


def _by_endpoint(db: Session, endpoint: str | None) -> PushSubscription | None:
    if not endpoint:
        return None
    return db.exec(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()


@router.get("/push/badge")
def push_badge(db: Session = Depends(get_session)) -> dict:
    """How many meals in the next two days are unplanned; the app shows it
    as the red number on the icon each time it opens."""
    from mise.services import reminders
    return {"count": len(reminders.empty_meals(db, reminders.household_today(db)))}


@router.get("/push/prefs")
def push_prefs(endpoint: str = "", db: Session = Depends(get_session)) -> dict:
    """Which reminders this phone gets. `on` is False when the phone hasn't
    turned notifications on at all."""
    from mise.services import reminders
    sub = _by_endpoint(db, endpoint)
    muted = set((sub.muted_kinds or []) if sub else [])
    return {"on": sub is not None, "kinds": [
        {"key": k, "label": label, "detail": detail, "on": k not in muted}
        for k, label, detail in reminders.REMINDER_KINDS]}


@router.post("/push/prefs")
async def push_prefs_set(req: Request, db: Session = Depends(get_session)) -> dict:
    from mise.services import reminders
    body = await req.json()
    kind = body.get("kind")
    if kind not in {k for k, _, _ in reminders.REMINDER_KINDS}:
        raise HTTPException(status_code=400, detail="Unknown reminder")
    sub = _by_endpoint(db, body.get("endpoint"))
    if sub is None:
        raise HTTPException(status_code=400, detail="Turn on notifications for this phone first.")
    muted = [k for k in (sub.muted_kinds or []) if k != kind]
    if not body.get("on"):
        muted.append(kind)
    sub.muted_kinds = muted
    db.add(sub)
    db.commit()
    return {"kind": kind, "on": kind not in muted}


@router.get("/push/key")
def push_key() -> dict:
    return {"publicKey": push_service.public_key()}


@router.post("/push/subscribe")
async def push_subscribe(req: Request, db: Session = Depends(get_session)) -> dict:
    body = await req.json()
    sub = body.get("subscription") or {}
    keys = sub.get("keys") or {}
    try:
        row = push_service.subscribe(
            db, sub.get("endpoint", ""), keys.get("p256dh", ""), keys.get("auth", ""),
            label=body.get("label"), origin=body.get("origin"),
            user_agent=req.headers.get("user-agent"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"id": row.id, "label": row.label}


@router.post("/push/unsubscribe")
async def push_unsubscribe(req: Request, db: Session = Depends(get_session)) -> dict:
    body = await req.json()
    return {"removed": push_service.unsubscribe(db, body.get("endpoint", ""))}


@router.get("/push/devices")
def push_devices(db: Session = Depends(get_session)) -> list[dict]:
    rows = db.exec(select(PushSubscription).order_by(PushSubscription.id)).all()
    return [{
        "id": r.id, "label": r.label or f"Device {r.id}",
        "service": r.endpoint.split("/")[2] if "//" in r.endpoint else "",
        "created_at": r.created_at.isoformat(), "last_ok_at": r.last_ok_at and r.last_ok_at.isoformat(),
        "last_error": r.last_error,
    } for r in rows]


@router.post("/push/devices/{sub_id}/delete")
def push_device_delete(sub_id: int, db: Session = Depends(get_session)) -> dict:
    row = db.get(PushSubscription, sub_id)
    if row is None:
        raise HTTPException(status_code=404)
    db.delete(row)
    db.commit()
    return {"removed": True}


@router.post("/push/test")
async def push_test(req: Request, db: Session = Depends(get_session)) -> dict:
    body = await req.json()
    kind = body.get("kind", "")
    if body.get("target") == "all":
        subs = list(db.exec(select(PushSubscription)).all())
    else:
        one = _by_endpoint(db, body.get("endpoint"))
        subs = [one] if one else []
    if not subs:
        raise HTTPException(status_code=400, detail="No phone has notifications turned on yet.")
    try:
        test_id = push_service.run_test(db, kind, subs, lead=push_service.TEST_LEAD_SECONDS)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"test_id": test_id, "devices": len(subs)}


@router.post("/push/ack")
async def push_ack(req: Request, db: Session = Depends(get_session)) -> dict:
    """Called by the service worker (received/clicked) and the page
    (opened/report). Unknown stages are refused so the log stays meaningful."""
    body = await req.json()
    stage = body.get("stage", "")
    test_id = str(body.get("test_id") or "")
    if stage not in _ACK_STAGES or not test_id:
        raise HTTPException(status_code=400, detail="Bad ack")
    sub = _by_endpoint(db, body.get("endpoint"))
    push_service.log_event(db, test_id, str(body.get("kind") or "?"), stage,
                           sub.id if sub else None, body.get("detail"))
    return {"ok": True}


@router.get("/push/events")
def push_events(db: Session = Depends(get_session), limit: int = 200) -> list[dict]:
    """Recent tests, newest first, each with its per-device stage trail."""
    events = db.exec(select(PushEvent).order_by(PushEvent.id.desc()).limit(min(limit, 500))).all()
    labels = {s.id: (s.label or f"Device {s.id}") for s in db.exec(select(PushSubscription)).all()}
    tests: dict[str, dict] = {}
    for e in reversed(events):
        t = tests.setdefault(e.test_id, {"test_id": e.test_id, "kind": e.kind,
                                         "at": e.created_at.isoformat(), "trail": []})
        t["trail"].append({"stage": e.stage, "device": labels.get(e.subscription_id, "unknown phone")
                           if e.subscription_id else "unknown phone",
                           "detail": e.detail, "at": e.created_at.isoformat()})
    return sorted(tests.values(), key=lambda t: t["at"], reverse=True)


@router.get("/app/notify-test", response_class=HTMLResponse)
def notify_test_page() -> HTMLResponse:
    kinds = [{"key": k.key, "label": k.label, "look_for": k.look_for, "group": k.group,
              "local": k.local} for k in push_service.TEST_KINDS]

    def cards(group: str) -> str:
        return "".join(
        f"""<div class="card p-4" data-kind="{_h(k['key'])}">
          <div class="flex items-center gap-3">
            <div class="flex-1 min-w-0">
              <div class="serif text-base font-medium">{_h(k['label'])}</div>
              <div class="text-xs text-stone-500 mt-0.5">{_h(k['look_for'])}</div>
            </div>
            <button type="button" class="nt-send shrink-0 bg-stone-900 text-white text-sm font-medium px-3 py-2 rounded-lg">{'Do it' if k['local'] else 'Send'}</button>
          </div>
          <div class="nt-after mt-3 text-sm" hidden>
            <div class="nt-status text-stone-500"></div>
            <div class="mt-2 flex flex-wrap gap-2 items-center">
              <span class="text-stone-600">As described?</span>
              <button type="button" class="nt-report border text-emerald-800 px-3 py-1 rounded-lg" style="border-color:#6ee7b7" data-answer="yes">Yes</button>
              <button type="button" class="nt-report border border-amber-300 text-amber-900 px-3 py-1 rounded-lg" data-answer="partly">Partly</button>
              <button type="button" class="nt-report border text-rose-800 px-3 py-1 rounded-lg" style="border-color:#fda4af" data-answer="no">No</button>
            </div>
          </div>
        </div>"""
        for k in kinds if k["group"] == group
    )
    body = f"""
      <div class="mb-1 uppercase text-stone-400" style="font-size:10px;letter-spacing:0.25em">Notifications</div>
      <h1 class="serif text-4xl font-medium tracking-tight leading-none">Settings.</h1>
      <p class="text-sm text-stone-500 mt-2">Turn notifications on for this phone and choose which reminders it gets. Tests are further down.</p>

      <div id="nt-opened" hidden class="mt-4 rounded-xl px-4 py-3 text-sm" style="background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46"></div>

      <div class="card p-4 mt-5">
        <div class="serif text-lg font-medium">This phone</div>
        <dl id="nt-checks" class="mt-2 text-sm grid gap-1" style="grid-template-columns:auto 1fr"></dl>
        <label class="block text-sm mt-4 text-stone-600" for="nt-label">Name this phone</label>
        <input id="nt-label" class="mt-1 w-full border border-stone-300 rounded-lg px-3 py-2 text-base" placeholder="e.g. Kitchen iPhone" maxlength="60">
        <div class="mt-3 flex flex-wrap gap-2">
          <button type="button" id="nt-enable" class="text-white text-sm font-medium px-4 py-2 rounded-lg" style="background:#b45309">Turn on notifications</button>
          <button type="button" id="nt-disable" class="border border-stone-300 text-sm px-4 py-2 rounded-lg" hidden>Turn off on this phone</button>
        </div>
        <div id="nt-msg" class="mt-3 text-sm"></div>
      </div>

      <div class="card p-4 mt-4">
        <div class="serif text-lg font-medium">Reminders on this phone</div>
        <p class="text-xs text-stone-500 mt-1">Each phone chooses for itself. Switching one off here doesn't change the other phone.</p>
        <div id="nt-prefs" class="mt-3 space-y-3 text-sm"></div>
      </div>

      <div class="card p-4 mt-4">
        <div class="serif text-lg font-medium">Send to</div>
        <div class="mt-2 flex gap-4 text-sm">
          <label class="flex items-center gap-2"><input type="radio" name="nt-target" value="this" checked> This phone</label>
          <label class="flex items-center gap-2"><input type="radio" name="nt-target" value="all"> Every phone</label>
        </div>
        <div id="nt-devices" class="mt-3 text-sm text-stone-600"></div>
      </div>

      <div class="mt-6 mb-2 serif text-xl font-medium">Red number on the icon</div>
      <p class="text-xs text-stone-500 mb-3">Go down the list in order. After each one, go to the home screen, look at the Mise icon, come back and answer. Each "Set" is followed by ways to clear it; if a clear works, the next "Set" puts a number back.</p>
      <div class="space-y-2">{cards("badge")}</div>

      <div class="mt-6 mb-2 serif text-xl font-medium">Other tests</div>
      <p class="text-xs text-stone-500 mb-3">Tip: after tapping Send, go to the home screen or lock the phone. iPhones usually don't show a notification while you're looking at the app.</p>
      <div class="space-y-2">{cards("other")}</div>

      <div class="mt-8 mb-2 serif text-xl font-medium">What happened</div>
      <p class="text-xs text-stone-500 mb-3"><b>sent</b>: Apple accepted it. <b>received</b>: the phone's background script got it. <b>clicked</b>: you tapped it or a button. <b>opened</b>: the tap landed on this page. <b>report</b>: your answer.</p>
      <div id="nt-log" class="space-y-2 text-sm"></div>
      <div style="height:6rem"></div>
    """
    script = """
<script>
(function() {
  const P = __PREFIX__;
  const KINDS = __KINDS__;
  const $ = (s, r=document) => r.querySelector(s);
  const msg = (t, tone) => { const m = $('#nt-msg'); m.textContent = t || ''; m.className = 'mt-3 text-sm ' + (tone === 'bad' ? 'text-rose-700' : tone === 'good' ? 'text-emerald-700' : 'text-stone-600'); };
  let endpoint = null;

  async function api(path, body) {
    let r;
    try {
      r = await fetch(P + path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', redirect: 'manual',
        headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    } catch (e) { throw new Error('Could not reach Mise. If this keeps happening, close and reopen the app to log in again.'); }
    if (r.type === 'opaqueredirect' || r.status === 0 || r.status === 302) throw new Error('Your login has expired. Close and reopen Mise to log in again.');
    if (!r.ok) { let d = ''; try { d = (await r.json()).detail; } catch (e) {} throw new Error(d || ('Error ' + r.status)); }
    return r.json();
  }

  function b64ToBytes(s) {
    const pad = '='.repeat((4 - s.length % 4) % 4);
    const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
  }

  const standalone = window.navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  const iosMatch = navigator.userAgent.match(/OS (\\d+)_(\\d+)/);
  const iosVer = iosMatch ? iosMatch[1] + '.' + iosMatch[2] : null;

  // serviceWorker.ready never settles if the worker isn't registered; give up after 4 s.
  async function reg() {
    if (!('serviceWorker' in navigator)) return null;
    return Promise.race([navigator.serviceWorker.ready, new Promise(r => setTimeout(() => r(null), 4000))]);
  }

  async function renderChecks() {
    const r = await reg().catch(() => null);
    let sub = null;
    try { sub = r && r.pushManager ? await r.pushManager.getSubscription() : null; } catch (e) {}
    endpoint = sub ? sub.endpoint : null;
    const rows = [
      ['Opened from home screen', standalone ? 'yes' : 'no (needed on iPhone)', standalone],
      ['iOS version', iosVer || 'not an iPhone', !iosVer || parseFloat(iosVer) >= 16.4],
      ['Notifications supported', 'Notification' in window ? 'yes' : 'no', 'Notification' in window],
      ['Push supported', 'PushManager' in window ? 'yes' : 'no', 'PushManager' in window],
      ['iOS 18.4 format supported', 'pushManager' in window ? 'yes' : 'no', null],
      ['Icon badge supported', 'setAppBadge' in navigator ? 'yes' : 'no', null],
      ['Permission', 'Notification' in window ? Notification.permission : 'n/a', 'Notification' in window && Notification.permission === 'granted'],
      ['This phone turned on', sub ? 'yes' : 'no', !!sub],
    ];
    $('#nt-checks').innerHTML = rows.map(([k, v, ok]) =>
      `<dt class="text-stone-500 pr-3">${k}</dt><dd class="${ok === true ? 'text-emerald-700' : ok === false ? 'text-rose-700' : 'text-stone-800'}">${v}</dd>`).join('');
    $('#nt-enable').textContent = sub ? 'Re-register this phone' : 'Turn on notifications';
    $('#nt-disable').hidden = !sub;
  }

  async function renderPrefs() {
    const box = $('#nt-prefs');
    try {
      const p = await api('/push/prefs?endpoint=' + encodeURIComponent(endpoint || ''));
      if (!p.on) { box.innerHTML = '<div class="text-stone-500">Turn on notifications above first.</div>'; return; }
      box.innerHTML = p.kinds.map(k => `<label class="flex items-start gap-3">
        <input type="checkbox" class="nt-pref mt-1" style="width:1.25rem;height:1.25rem" data-kind="${esc(k.key)}" ${k.on ? 'checked' : ''}>
        <span><span class="font-medium">${esc(k.label)}</span><br><span class="text-xs text-stone-500">${esc(k.detail)}</span></span></label>`).join('');
      box.querySelectorAll('.nt-pref').forEach(cb => cb.addEventListener('change', async () => {
        cb.disabled = true;
        try { await api('/push/prefs', { endpoint, kind: cb.dataset.kind, on: cb.checked }); }
        catch (e) { cb.checked = !cb.checked; alert(e.message); }
        cb.disabled = false;
      }));
    } catch (e) { box.textContent = e.message; }
  }

  async function renderDevices() {
    try {
      const ds = await api('/push/devices');
      $('#nt-devices').innerHTML = ds.length ? ('Phones with notifications on: ' + ds.map(d =>
        `<span class="inline-block mr-2"><b>${esc(d.label)}</b>${d.last_error ? ' <span class="text-rose-700">(last send failed)</span>' : ''} <button type="button" class="text-stone-400 underline" data-del="${d.id}">remove</button></span>`).join('')) : 'No phones have notifications on yet.';
    } catch (e) { $('#nt-devices').textContent = e.message; }
  }
  $('#nt-devices').addEventListener('click', async (e) => {
    const id = e.target.dataset && e.target.dataset.del; if (!id) return;
    if (!confirm('Remove this phone from notifications?')) return;
    await api('/push/devices/' + id + '/delete', {}); renderDevices();
  });

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  $('#nt-enable').addEventListener('click', async () => {
    msg('Working…');
    try {
      if (!('Notification' in window) || !('PushManager' in window)) throw new Error(standalone ? 'This phone does not support notifications. iPhones need iOS 16.4 or newer.' : 'Open Mise from its home-screen icon first, then come back to this page (More → Notification test).');
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') throw new Error(perm === 'denied' ? 'Notifications are blocked. Turn them on in Settings → Notifications → Mise, then try again.' : 'Permission was not given.');
      const r = await reg();
      if (!r) throw new Error("Mise's background script is not running. Close and reopen the app, then try again.");
      const { publicKey } = await api('/push/key');
      let sub = await r.pushManager.getSubscription();
      if (!sub) sub = await r.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(publicKey) });
      const label = $('#nt-label').value.trim();
      try { if (label) localStorage.setItem('mise:push-label', label); } catch (e) {}
      await api('/push/subscribe', { subscription: sub.toJSON(), label: label || null, origin: location.origin });
      msg('Notifications are on for this phone. Try Plain first.', 'good');
    } catch (e) { msg(e.message || String(e), 'bad'); }
    renderChecks().then(renderPrefs); renderDevices();
  });

  $('#nt-disable').addEventListener('click', async () => {
    try {
      const r = await reg(); const sub = r ? await r.pushManager.getSubscription() : null;
      if (sub) { await api('/push/unsubscribe', { endpoint: sub.endpoint }); await sub.unsubscribe(); }
      msg('Turned off on this phone.');
    } catch (e) { msg(e.message, 'bad'); }
    renderChecks().then(renderPrefs); renderDevices();
  });

  const lastTest = {};
  const LOCAL = Object.fromEntries(KINDS.map(k => [k.key, k.local]));
  document.querySelectorAll('[data-kind]').forEach(card => {
    const kind = card.dataset.kind;
    card.querySelector('.nt-send').addEventListener('click', async (ev) => {
      const btn = ev.currentTarget; const after = card.querySelector('.nt-after'); const st = card.querySelector('.nt-status');
      after.hidden = false; st.className = 'nt-status text-stone-500'; st.textContent = 'Sending…'; btn.disabled = true;
      if (LOCAL[kind]) {
        // Runs right here in the app; no notification involved.
        const test_id = 'p' + Math.random().toString(16).slice(2, 10);
        lastTest[kind] = test_id;
        let detail;
        try {
          if (!('setAppBadge' in navigator)) throw new Error('this phone has no icon number support in the app');
          if (kind === 'page_set') await navigator.setAppBadge(7);
          else if (kind === 'page_clear') await navigator.clearAppBadge();
          else await navigator.setAppBadge(0);
          detail = 'ran in the app without an error';
          st.textContent = 'Done. Go to the home screen, look at the Mise icon, then come back and answer.';
        } catch (e) { detail = 'failed: ' + (e.message || e); st.className = 'nt-status text-rose-700'; st.textContent = 'It failed: ' + (e.message || e); }
        api('/push/ack', { test_id, kind, stage: 'received', endpoint, detail }).then(renderLog).catch(() => {});
        btn.disabled = false;
        return;
      }
      const target = document.querySelector('input[name=nt-target]:checked').value;
      try {
        if (target === 'this' && !endpoint) throw new Error('Turn on notifications for this phone first.');
        const res = await api('/push/test', { kind, target, endpoint });
        lastTest[kind] = res.test_id;
        st.textContent = 'Sending to ' + res.devices + (res.devices === 1 ? ' phone' : ' phones') + (kind === 'delayed' ? ' in 1 minute. Lock the phone now.' : ' in 2 seconds. Go to the home screen now.');
        setTimeout(renderLog, 1500);
      } catch (e) { st.className = 'nt-status text-rose-700'; st.textContent = e.message; }
      btn.disabled = false;
    });
    card.querySelectorAll('.nt-report').forEach(b => b.addEventListener('click', async () => {
      const test_id = lastTest[kind]; if (!test_id) return;
      let note = '';
      if (b.dataset.answer !== 'yes') note = prompt('What did you see? (optional)') || '';
      try {
        await api('/push/ack', { test_id, kind, stage: 'report', endpoint, detail: b.dataset.answer + (note ? ': ' + note : '') });
        card.querySelector('.nt-status').textContent = 'Thanks, saved: ' + b.dataset.answer + '.';
        renderLog();
      } catch (e) { card.querySelector('.nt-status').textContent = e.message; }
    }));
  });

  async function renderLog() {
    try {
      const tests = await api('/push/events?limit=300');
      const label = Object.fromEntries(KINDS.map(k => [k.key, k.label]));
      $('#nt-log').innerHTML = tests.length ? tests.slice(0, 25).map(t => {
        const when = new Date(t.at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        const trail = t.trail.map(s => `<li><b>${esc(s.stage)}</b> · ${esc(s.device)}${s.detail ? ' · <span class="text-stone-500">' + esc(s.detail) + '</span>' : ''}</li>`).join('');
        return `<div class="card p-3"><div class="flex justify-between"><span class="font-medium">${esc(label[t.kind] || t.kind)}</span><span class="text-stone-400 text-xs">${when}</span></div><ul class="mt-1 text-xs" style="line-height:1.6">${trail}</ul></div>`;
      }).join('') : '<div class="text-stone-500">Nothing sent yet.</div>';
    } catch (e) { $('#nt-log').textContent = e.message; }
  }

  (async function init() {
    try { const l = localStorage.getItem('mise:push-label'); if (l) $('#nt-label').value = l; } catch (e) {}
    renderDevices(); renderLog();
    await renderChecks();
    renderPrefs();
    const q = new URLSearchParams(location.search);
    if (q.get('ntest')) {
      const action = q.get('action');
      const box = $('#nt-opened'); box.hidden = false;
      box.textContent = 'Opened from a notification ✓' + (action ? ' (button: ' + action + ')' : '');
      api('/push/ack', { test_id: q.get('ntest'), kind: q.get('kind') || '?', stage: 'opened', endpoint,
        detail: (standalone ? 'in the home-screen app' : 'in a browser tab') + (action ? ', button ' + action : '') })
        .catch(e => { box.textContent += ' — but saving that failed: ' + e.message; });
      history.replaceState(null, '', location.pathname);
    }
    setInterval(() => { if (!document.hidden) renderLog(); }, 5000);
  })();
})();
</script>"""
    script = script.replace("__PREFIX__", json.dumps(PREFIX)).replace("__KINDS__", json.dumps(kinds))
    return HTMLResponse(_shell("Notifications", body + script, active="tools"))
