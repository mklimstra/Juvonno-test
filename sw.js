/* SCAT6 Tool service worker.
 *
 * Deliberately minimal: it exists for installability and push notifications.
 * It does NOT cache or intercept app traffic, so the app always serves fresh
 * from the server and this file can be removed without side effects (see
 * backup_pre_pwa/REVERT.md). */

self.addEventListener('install', function (event) {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'SCAT6 Tool', body: event.data ? event.data.text() : '' };
  }
  var title = data.title || 'SCAT6 Tool';
  var options = {
    body: data.body || '',
    icon: '/assets/img/icon-192.png',
    badge: '/assets/img/icon-192.png',
    tag: data.tag || 'scat6-reminder',
    renotify: true,
    data: { url: data.url || '/' }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        if ('focus' in list[i]) { return list[i].focus(); }
      }
      return self.clients.openWindow(url);
    })
  );
});
