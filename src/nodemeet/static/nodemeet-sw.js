/*! nodemeet service worker: shows push notifications (PolyForm Noncommercial 1.0.0) */
self.addEventListener('push', (event) => {
  let m = {};
  try { m = event.data ? event.data.json() : {}; } catch (e) { m = { title: 'nodemeet', body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(m.title || 'nodemeet', {
    body: m.body || '', icon: m.icon || undefined, data: { url: m.url || '/' },
    tag: (m.data && m.data.event) || undefined, renotify: true,
    requireInteraction: m.urgency === 'high',
  }));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) { if (c.url === url && 'focus' in c) return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
