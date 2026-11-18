/**
 * Mise service worker — offline-first for /app/* surfaces.
 *
 * Strategy:
 *   - GETs to /app/, /app/plan, /app/shop, /app/recipe/*, /static/* →
 *     network-first with a cache fallback. Successful responses are
 *     written to the cache so the next offline open works.
 *   - Mutations (POST/PATCH/DELETE) while offline → queued to
 *     IndexedDB and replayed when network returns. The page that
 *     issued them gets an optimistic 202 Accepted with {queued: true}
 *     so its UI can carry on. Pure read endpoints aren't queued.
 *   - The "static" runtime cache is versioned; bumping CACHE_NAME
 *     forces a refresh on the next activate.
 */

const CACHE_NAME  = 'mise-app-v49';
const QUEUE_DB    = 'mise-sync-v1';
const QUEUE_STORE = 'pending';

// Cache the shell on install so the first offline open works even if
// the user hasn't visited the page online yet (won't be reachable for
// a fresh install but cheap to attempt).
const PRECACHE = [
  '/mise/static/tw.css?v=6',
  '/mise/static/manifest.json',
  '/mise/static/measure.js?v=5',
  '/mise/static/ptr.js?v=4',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(c => c.addAll(PRECACHE).catch(() => {}))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only intercept same-origin /mise/app/*, /mise/static/*, and a few
  // mutation endpoints. Leave everything else (cross-origin fonts,
  // /preview/, /docs, etc.) to the network.
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;
  // Push API calls must hit the network directly: never queue or cache.
  if (path.startsWith('/mise/push/')) return;
  const isAppPath = path.startsWith('/mise/app') || path.startsWith('/mise/static/');
  const isMutation = req.method !== 'GET' && req.method !== 'HEAD';
  if (!isAppPath && !isMutation) return;

  if (isMutation) {
    event.respondWith(handleMutation(req));
    return;
  }
  // Polling endpoints — the shop sync poll needs fresh data every
  // tick. Don't cache, don't even handle in the SW.
  if (path === '/mise/app/shop/checks') return;
  event.respondWith(networkFirst(req));
});

// Stale-while-revalidate. Serve the cached HTML immediately (instant
// tab switch), kick off the network refresh in the background, and
// update the cache with the fresh response for the next visit. This
// is what makes Library / Plan / Shop hops feel instantaneous instead
// of waiting on a 200–400ms server round-trip every time.
//
// The trade-off: a check made on another device might show one beat
// stale here, but the shop page's 3s sync poll picks that up almost
// immediately and reconciles the DOM without a reload.
async function networkFirst(req) {
  const cache = await caches.open(CACHE_NAME);
  const networkPromise = fetch(req).then(res => {
    if (res.ok && res.type !== 'opaque') {
      cache.put(req, res.clone());
    }
    return res;
  }).catch(() => null);
  const cached = await cache.match(req);
  if (cached) return cached;
  const network = await networkPromise;
  if (network) return network;
  return new Response(
    `<!doctype html><meta charset=utf-8>
     <title>Offline · Mise</title>
     <body style="font-family:system-ui;padding:2rem;background:#FAF7F2;color:#1c1917">
     <h1 style="font-family:Georgia,serif">Offline.</h1>
     <p>This surface hasn't been cached yet. Connect, open it once, and it'll be available offline next time.</p>
     </body>`,
    { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
  );
}

// ----- mutation queue (POST/PATCH/DELETE) ------------------------------------

// A successful write can invalidate ANY app page that renders the
// changed data — the plan grid, the review queue, the library count,
// a recipe detail. The fetch handler serves all of /app/* stale-while-
// revalidate, so unless we evict it the next visit shows the old state
// until a second load (looks like the action didn't take). Rather than
// maintain a fragile allowlist of "pages a mutation might touch" (which
// always misses the next new page), purge every cached app HTML page on
// any successful mutation. Static assets (/static/*) are untouched —
// CSS/manifest don't change from a write. The next /app navigation pays
// one network round-trip for fresh data, then re-caches; routine tab
// switches stay instant.
async function purgeAppPages() {
  const cache = await caches.open(CACHE_NAME);
  const keys = await cache.keys();
  await Promise.all(keys.map(k => {
    const p = new URL(k.url).pathname;
    return (p.startsWith('/mise/app') && !p.startsWith('/mise/static/'))
      ? cache.delete(k) : null;
  }));
}

async function handleMutation(req) {
  try {
    // Try the network first. Body is consumed if we read it, so clone.
    const res = await fetch(req.clone());
    // A successful write can change any app page — drop their cached
    // HTML so the next visit re-fetches fresh (no manual refresh).
    if (res.ok) { try { await purgeAppPages(); } catch (_) {} }
    return res;
  } catch (e) {
    // Network unavailable — queue the request.
    const serialised = await serialiseRequest(req);
    await enqueue(serialised);
    // Try to register background sync if supported; replay on next
    // page load anyway as a fallback.
    if ('sync' in self.registration) {
      try { await self.registration.sync.register('mise-sync'); } catch (_) {}
    }
    return new Response(
      JSON.stringify({ queued: true, status: 'pending sync' }),
      { status: 202, headers: { 'Content-Type': 'application/json' } }
    );
  }
}

self.addEventListener('sync', (event) => {
  if (event.tag === 'mise-sync') {
    event.waitUntil(replayQueue());
  }
});

// Also listen for an explicit "replay" message from the page so we can
// flush the queue when network returns even without background-sync.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'replay-queue') {
    event.waitUntil(replayQueue());
  }
});

async function serialiseRequest(req) {
  const body = req.method === 'GET' || req.method === 'HEAD'
    ? null : await req.clone().text();
  const headers = {};
  for (const [k, v] of req.headers.entries()) headers[k] = v;
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    url: req.url,
    method: req.method,
    headers,
    body,
    createdAt: Date.now(),
  };
}

async function replayQueue() {
  const all = await drain();
  for (const item of all) {
    try {
      await fetch(item.url, {
        method: item.method,
        headers: item.headers,
        body: item.body,
      });
    } catch (e) {
      // Re-enqueue on failure so we try again later.
      await enqueue(item);
    }
  }
  // Tell open clients we synced something.
  const clients = await self.clients.matchAll({ includeUncontrolled: true });
  for (const client of clients) {
    client.postMessage({ type: 'queue-flushed', count: all.length });
  }
}

// ----- IndexedDB persistence -------------------------------------------------

function openDB() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(QUEUE_DB, 1);
    r.onupgradeneeded = () => {
      r.result.createObjectStore(QUEUE_STORE, { keyPath: 'id' });
    };
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

async function enqueue(item) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readwrite');
    tx.objectStore(QUEUE_STORE).put(item);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function drain() {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readwrite');
    const store = tx.objectStore(QUEUE_STORE);
    const items = [];
    store.openCursor().onsuccess = (e) => {
      const cur = e.target.result;
      if (cur) { items.push(cur.value); cur.delete(); cur.continue(); }
    };
    tx.oncomplete = () => resolve(items);
    tx.onerror = () => reject(tx.error);
  });
}

// ----- push notifications ----------------------------------------------------
// Two payload shapes arrive here:
//  - Mise's own JSON {title, body, url, tag, actions, badge, test_id, kind, …}
//  - Apple's Declarative Web Push {web_push: 8030, notification: {…}}. iOS 18.4+
//    shows these itself; the worker only sees them when `mutable` is set, or on
//    browsers without declarative support (then we show it ourselves).
// The test id travels in the tap URL (?ntest=) because iOS has been seen not
// to fire notificationclick, so the opened page is the reliable tap signal.

function testFromUrl(u) {
  try { const q = new URL(u, self.location.origin).searchParams; return { id: q.get('ntest'), kind: q.get('kind') }; }
  catch (e) { return {}; }
}

async function pushAck(testId, kind, stage, detail) {
  if (!testId) return;
  try {
    const sub = await self.registration.pushManager.getSubscription();
    await fetch('/mise/push/ack', {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ test_id: testId, kind, stage, detail, endpoint: sub && sub.endpoint }),
    });
  } catch (e) { /* login expired or offline: the log just misses this step */ }
}

self.addEventListener('push', (event) => {
  event.waitUntil((async () => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; }
    catch (e) { data = { title: 'Mise', body: event.data ? event.data.text() : '' }; }

    if (data.web_push === 8030 && data.notification) {
      const n = data.notification;
      const t = testFromUrl(n.navigate);
      if (event.notification) {
        // iOS declarative + mutable: the system proposed this notification;
        // rewriting it proves the worker can edit declarative messages.
        await self.registration.showNotification('✏️ ' + (n.title || 'Mise'), {
          body: n.body || '', data: { url: n.navigate, test_id: t.id, kind: t.kind } });
        await pushAck(t.id, t.kind, 'received', 'declarative, edited by the worker');
      } else {
        await self.registration.showNotification(n.title || 'Mise', {
          body: n.body || '', data: { url: n.navigate, test_id: t.id, kind: t.kind } });
        if (n.app_badge != null && self.navigator.setAppBadge) {
          try { await self.navigator.setAppBadge(Number(n.app_badge)); } catch (e) {}
        }
        await pushAck(t.id, t.kind, 'received', 'declarative shown by the worker (phone did not handle it natively)');
      }
      return;
    }

    const opts = { body: data.body || '', data: { url: data.url, test_id: data.test_id, kind: data.kind } };
    if (data.tag) opts.tag = data.tag;
    if (data.renotify) opts.renotify = true;
    if (data.silent) opts.silent = true;
    if (data.actions) opts.actions = data.actions;
    if (data.image) opts.image = data.image;
    let badgeNote = '';
    if (data.badge != null) {
      if (self.navigator.setAppBadge) {
        try {
          if (data.badge) { await self.navigator.setAppBadge(data.badge); badgeNote = ', badge set to ' + data.badge; }
          else if (data.badge_zero) { await self.navigator.setAppBadge(0); badgeNote = ', setAppBadge(0) ran'; }
          else { await self.navigator.clearAppBadge(); badgeNote = ', clearAppBadge() ran'; }
        } catch (e) { badgeNote = ', badge failed: ' + e; }
      } else { badgeNote = ', badge not supported in worker'; }
    }
    await self.registration.showNotification(data.title || 'Mise', opts);
    await pushAck(data.test_id, data.kind, 'received', 'shown by the worker' + badgeNote);
  })());
});

self.addEventListener('notificationclick', (event) => {
  const d = event.notification.data || {};
  event.notification.close();
  event.waitUntil((async () => {
    await pushAck(d.test_id, d.kind, 'clicked', event.action ? 'button: ' + event.action : 'tapped the notification');
    let url = d.url || '/mise/app/';
    if (event.action) url += (url.includes('?') ? '&' : '?') + 'action=' + encodeURIComponent(event.action);
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      try { await w.focus(); if ('navigate' in w) { await w.navigate(url); return; } } catch (e) {}
    }
    await self.clients.openWindow(url);
  })());
});
