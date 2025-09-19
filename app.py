import os
import dash
import flask
from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update
import dash_bootstrap_components as dbc
import requests  # <-- minimal addition: used to call /me

# PKCE + login helpers (unchanged)
import base64, hashlib, secrets
from urllib.parse import urlencode
from flask import session, redirect

# Keep the repo’s layout pieces and settings exactly the same
from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# ⬇️ Your training dashboard (unchanged)
from training_dashboard import layout_body as training_layout_body, register_callbacks as training_register_callbacks

# ---- NEW: me endpoint for current user (minimal) ----
API_ME_URL = f"{SITE_URL}/api/csiauth/me/"

# ------------------------- DashAuthExternal / Flask server (unchanged) -------------------------
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Stable session for PKCE
server.secret_key = os.getenv("SECRET_KEY", "dev-change-me")
server.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True
)

# Serve assets (unchanged)
here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"

# ------------------------- PKCE login route (unchanged) -------------------------
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

@server.route("/login", methods=["GET"])
def login():
    app_base = APP_URL.rstrip("/")
    redirect_uri = f"{app_base}/redirect"   # DashAuthExternal default callback path

    verifier  = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state     = _b64url(secrets.token_bytes(16))

    session["cv"] = verifier
    session["st"] = state
    session["redirect_uri"] = redirect_uri

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return redirect(f"{AUTH_URL}?{urlencode(params)}")

# ------------------------- Dash app (unchanged) -------------------------
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css",
    ],
    suppress_callback_exceptions=True,
    title="CSI Apps — Training Status",
)

# ------------------------- Layout (Training Status only) -------------------------
app.layout = html.Div([
    # mirrors repo’s boot sequence
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    # MINIMAL CHANGE: pass a right-side slot to Navbar with an element we can update
    Navbar([
        html.Span(
            id="navbar-user",
            className="text-white-50 small",  # matches dark navbar style
            children=""                       # will be populated after auth
        )
    ]).render(),

    dbc.Container([
        # Only your Training Status dashboard
        training_layout_body(),
    ], fluid=True),

    Footer().render(),
])

# ------------------------- Initial auth check (unchanged logic) -------------------------
@app.callback(
    Output("redirect-to", "href"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    """
    On first tick:
      - If no token, navigate to 'login' (relative path keeps Connect prefix)
      - If token exists, do nothing
    """
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if not token:
        return "login"   # go start the OAuth flow
    return no_update

# ------------------------- NEW: populate "who is logged in" (minimal) -------------------------
@app.callback(
    Output("navbar-user", "children"),
    Input("init-interval", "n_intervals"),
    prevent_initial_call=False
)
def show_current_user(_n):
    """
    After the initial interval (or at load), fetch /api/csiauth/me/ with the bearer token
    and show a compact identity label in the top-right of the navbar.
    """
    try:
        token = auth.get_token()
        if not token:
            return ""  # not signed in yet
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(API_ME_URL, headers=headers, timeout=6)
        if r.status_code != 200:
            return ""
        me = r.json() or {}
        # try common fields; adjust keys if your /me differs
        name  = me.get("name") or me.get("display_name") or me.get("full_name") \
                or f"{me.get('first_name','')} {me.get('last_name','')}".strip()
        email = me.get("email") or (me.get("user") or {}).get("email")
        if name and email:
            return f"Signed in as {name} ({email})"
        if name:
            return f"Signed in as {name}"
        if email:
            return f"Signed in as {email}"
        return "Signed in"
    except Exception:
        return ""  # keep UI clean if anything fails

# ------------------------- Training dashboard callbacks (unchanged) -------------------------
training_register_callbacks(app)

if __name__ == "__main__":
    app.run(debug=False, port=8050)
