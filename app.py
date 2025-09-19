import os
import requests
import dash
import flask
from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update
import dash_bootstrap_components as dbc

# PKCE + login helpers
import base64, hashlib, secrets
from urllib.parse import urlencode
from flask import session, redirect

# Repo layout + settings
from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Training dashboard
from training_dashboard import layout_body as training_layout_body, register_callbacks as training_register_callbacks


# ------------------------- DashAuthExternal / Flask server -------------------------
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,          # e.g., https://connect.posit.cloud/content/<id>
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Cookie/session settings:
# - Use secure cookies only when APP_URL is HTTPS (local dev stays HTTP-safe)
is_https = APP_URL.lower().startswith("https://") if APP_URL else False
server.secret_key = os.getenv("SECRET_KEY", "dev-change-me")
server.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=is_https
)

# Serve /assets like the repo
here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"


# ------------------------- PKCE login route -------------------------
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

@server.route("/login", methods=["GET"])
def login():
    """
    Prepare PKCE + state in session, then hand off to the OAuth provider.
    Redirect URI must match what you registered and what DashAuthExternal expects.
    """
    app_base = APP_URL.rstrip("/") if APP_URL else ""
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


# ------------------------- Dash app -------------------------
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

# ------------------------- Layout -------------------------
app.layout = html.Div([
    # mirrors repo’s boot sequence
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    # refresh the user badge every 60s
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    # Navbar with a right-side slot for the signed-in user label
    Navbar([
        html.Span(id="navbar-user", className="text-white-50 small", children="")
    ]).render(),

    # Single page: Training Status dashboard
    dbc.Container([
        training_layout_body(),
    ], fluid=True),

    Footer().render(),
])


# ------------------------- Callbacks -------------------------
# Silent init: if no token, bounce to /login; else do nothing
@app.callback(
    Output("redirect-to", "href"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if not token:
        return "login"   # relative path keeps Connect’s /content/<id> prefix intact
    return no_update


# Populate/refresh "who is logged in" (top-right of navbar)
@app.callback(
    Output("navbar-user", "children"),
    Input("init-interval", "n_intervals"),
    Input("user-refresh", "n_intervals"),
    State("navbar-user", "children"),
    prevent_initial_call=False
)
def show_current_user(_n1, _n2, current_children):
    """
    Rules:
    - If we have a token: show "Signed in as …".
    - If we don't have a token: show "Sign in" *only* if there's no current label.
      (Prevents flicker when the token becomes available a split-second later.)
    - If /me fails temporarily: keep the previous label instead of flipping to "Sign in".
    """
    try:
        token = auth.get_token()
    except Exception:
        token = None

    # Helper for the link
    sign_in_link = html.A("Sign in", href="login", className="link-light text-decoration-none")

    if not token:
        # If no token yet (e.g., right at load) but we already had a label, keep it.
        return current_children if current_children else sign_in_link

    # We have a token → fetch identity
    try:
        r = requests.get(
            f"{SITE_URL}/api/csiauth/me/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=6
        )
        if r.status_code != 200:
            # Don't downgrade to "Sign in" if we had a label already; prompt reauth otherwise.
            return current_children if current_children else html.A("Re-authenticate", href="login",
                                                                    className="link-light text-decoration-none")

        me = r.json() or {}
        name  = me.get("name") or me.get("display_name") or me.get("full_name") \
                or f"{me.get('first_name','')} {me.get('last_name','')}".strip()
        email = me.get("email") or (me.get("user") or {}).get("email")
        label = f"Signed in as {name} ({email})" if (name and email) else f"Signed in as {name or email or 'user'}"
        return label

    except Exception:
        # Network blip: keep whatever was showing rather than flashing "Sign in"
        return current_children if current_children else sign_in_link



# Wire up the training dashboard’s internal callbacks
training_register_callbacks(app)


if __name__ == "__main__":
    # Locally you can run on 8050; on Connect, the launcher takes over.
    app.run(debug=False, port=8050)
