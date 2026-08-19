# push_service.py — VAPID key management, web-push sending, and the background
# reminder loop. Removable without side effects (see backup_pre_pwa/REVERT.md).
#
# Keys: set VAPID_PRIVATE_KEY (PEM or base64) / VAPID_PUBLIC_KEY /
# VAPID_CLAIMS_EMAIL in the deployment environment for stable keys across
# restarts. If unset, a key pair is generated once and stored next to the app
# in vapid_keys.json — fine for testing; use env vars in real deployment,
# because if the private key changes, existing phone subscriptions go stale
# and each device must re-enable reminders.
from __future__ import annotations
import base64, json, os, threading, time, traceback
from typing import Dict, Optional, Tuple

import push_store

KEY_FILE = os.path.join(os.path.dirname(__file__), "vapid_keys.json")
CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL", "mklimstra@csipacific.ca")

_keys: Optional[Tuple[str, str]] = None  # (private_pem, public_b64url)
_loop_started = False


def get_vapid_keys() -> Tuple[str, str]:
    """(private_key_pem, public_key_b64url) — env first, then key file,
    else generate and persist."""
    global _keys
    if _keys:
        return _keys
    env_priv = os.getenv("VAPID_PRIVATE_KEY")
    env_pub = os.getenv("VAPID_PUBLIC_KEY")
    if env_priv and env_pub:
        _keys = (env_priv, env_pub)
        return _keys
    if os.path.exists(KEY_FILE):
        try:
            js = json.load(open(KEY_FILE))
            _keys = (js["private_key"], js["public_key"])
            return _keys
        except Exception:
            pass
    # generate
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    v = Vapid()
    v.generate_keys()
    raw_pub = v.public_key.public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    public_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode()
    private_pem = v.private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    try:
        with open(KEY_FILE, "w") as f:
            json.dump({"private_key": private_pem, "public_key": public_b64}, f)
    except Exception as e:
        print(f"push: could not persist VAPID keys ({e}) — set VAPID_* env vars")
    _keys = (private_pem, public_b64)
    return _keys


def public_key() -> str:
    return get_vapid_keys()[1]


def send_push(subscription: Dict, title: str, body: str, url: str = "/") -> bool:
    """Send one notification. Returns False (and prunes the subscription) if
    the push service says the subscription is gone (404/410)."""
    from pywebpush import webpush, WebPushException
    priv, _ = get_vapid_keys()
    try:
        webpush(subscription_info=subscription,
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=priv,
                vapid_claims={"sub": f"mailto:{CLAIMS_EMAIL}"},
                ttl=3600)
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            endpoint = str(subscription.get("endpoint") or "")
            if endpoint:
                push_store.delete_subscription(endpoint)
                print(f"push: pruned dead subscription ({status})")
            return False
        raise


def _fmt_interval(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} day{'s' if d != 1 else ''}"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{minutes} minutes"


def _reminder_tick():
    for sub in push_store.due_subscriptions():
        try:
            ok = send_push(
                sub["subscription"],
                title="SCAT6 reminder",
                body=(f"Time to complete a SCAT6 form "
                      f"(every {_fmt_interval(int(sub['reminder_minutes']))})."),
                url="/")
            if ok:
                push_store.mark_reminded(sub["endpoint"])
        except Exception:
            traceback.print_exc()
            # transient failure — leave last_reminded_at alone so it retries
            # on the next tick, but don't spin the loop down.


def start_reminder_loop(interval_seconds: int = 60) -> None:
    """Start the background reminder thread once per process."""
    global _loop_started
    if _loop_started:
        return
    _loop_started = True

    def _loop():
        while True:
            try:
                _reminder_tick()
            except Exception:
                traceback.print_exc()
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="scat6-reminders", daemon=True)
    t.start()
    print("push: reminder loop started")
