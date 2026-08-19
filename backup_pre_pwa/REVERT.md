# Reverting the PWA / push-notification changes

If anything is funny after the PWA + reminders update, restore the app to the
previous state like this (from the Juvonno-test folder):

1. Restore the changed files:
   cp backup_pre_pwa/app.py app.py
   cp backup_pre_pwa/requirements.txt requirements.txt
   cp backup_pre_pwa/README.md README.md

2. Delete the files that were added (all of them are new — nothing else uses them):
   rm -f sw.js push_store.py push_service.py vapid_keys.json
   rm -f assets/manifest.json assets/img/icon-180.png assets/img/icon-192.png assets/img/icon-512.png

3. Redeploy / restart the app.

Notes
- scat6.db gains one extra table (push_subscriptions); the old code ignores it,
  so the database does NOT need to be touched to revert.
- If you added FLASK_SECRET_KEY / VAPID_* environment variables, they are
  harmless to leave in place.
- On phones that installed the app: remove the Home Screen icon; in
  Settings → Safari you can clear the site's data to drop the service worker.
