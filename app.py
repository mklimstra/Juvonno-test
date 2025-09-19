import os
import dash
import flask
from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update
import dash_bootstrap_components as dbc

# PKCE + login helpers (unchanged)
import base64, hashlib, secrets
from urllib.parse import urlencode
from flask import session, redirect

# Keep the repo’s layout pieces and settings exactly the same
from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# ⬇️ Your training dashboard (unchanged)
from training_dashboard import layout_body as training_layout_body, register_callbacks as training_register_callbacks

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

    Navbar([]).render(),                     # EXACT same call as the repo
    dbc.Container([
        # ⬇️ Only your Training Status dashboard
        training_layout_body(),
    ], fluid=True),
    Footer().render(),                       # EXACT same call as the repo
])

# ------------------------- Initial auth check (unchanged logic, simplified outputs) -------------------------
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

# ------------------------- Training dashboard callbacks (unchanged) -------------------------
training_register_callbacks(app)

if __name__ == "__main__":
    app.run(debug=False, port=8050)
