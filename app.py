import os
import math
import requests
import pandas as pd
import dash
import flask

from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update, dash_table, State, callback_context
import dash_bootstrap_components as dbc

# PKCE + login helpers
import base64, hashlib, secrets
from urllib.parse import urlencode
from flask import session, request, redirect

from layout import Footer, Navbar, Pagination, GeographyFilters
from settings import *  # expects AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET
from utils import fetch_options, fetch_profiles, restructure_profile


# ------------------------- DashAuthExternal / Flask server -------------------------
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,          # MUST equal your public Connect base, e.g. https://connect.posit.cloud/content/<id>
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET  # PLAINTEXT secret, not a pbkdf2 hash
)
server = auth.server

# Stable session so PKCE survives the round-trip on Posit Connect
server.secret_key = os.getenv("SECRET_KEY", "dev-change-me")
server.config.update(
    SESSION_COOKIE_SAMESITE="Lax",  # ok for top-level OAuth redirects
    SESSION_COOKIE_SECURE=True      # Connect is HTTPS
)

# Serve assets
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
    Prepare PKCE + state in *your* session, then hand off to apps.csipacific.ca.
    Redirect URI MUST match what you registered in the OAuth app, and must match
    what DashAuthExternal will use during the token exchange.
    """
    # Use configured APP_URL so it exactly matches the provider-registered value.
    # (APP_URL should be set to your public Connect base, e.g. https://connect.posit.cloud/content/<id>)
    app_base = APP_URL.rstrip("/")
    redirect_uri = f"{app_base}/redirect"   # DashAuthExternal default callback path

    # --- PKCE + state ---
    verifier  = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state     = _b64url(secrets.token_bytes(16))

    session["cv"] = verifier
    session["st"] = state
    session["redirect_uri"] = redirect_uri  # helpful for the library

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
)

geo_filters = GeographyFilters(app, auth, id="placename")

# Offcanvas for filters
offcanvas = dbc.Offcanvas(
    [
        dbc.Label("Format"),
        dcc.Dropdown(
            id="data-format-select",
            options=[
                {"label": "Profile", "value": "profile"},
                {"label": "Contact", "value": "contact"},
                {"label": "Social",  "value": "social"},
            ],
            value="profile",
            multi=False,
            className="mb-3",
        ),

        dbc.Label("Role"),
        dcc.Dropdown(id="filter-role", options=[], multi=False, className="mb-3"),

        dbc.Label("Campus"),
        dcc.Dropdown(id="filter-campus", options=[], multi=False, className="mb-3"),

        dbc.Label("Organization"),
        dcc.Dropdown(id="filter-organization", options=[], multi=False, className="mb-3"),

        dbc.Container([
            html.H6("Town/City"),
            dbc.Label("Scope"),
            dcc.Dropdown(
                id="filter-placename-scope",
                options=[
                    {"label": "Birthplace", "value": "birthplace"},
                    {"label": "Residence",  "value": "residence"},
                    {"label": "Either",     "value": "both"}
                ],
                value="birthplace",
                multi=False,
                className="mb-3",
            ),
            geo_filters.layout,
        ], className="background-light border mb-3"),

        dbc.Button("Apply/Retrieve", id="apply-filters", color="primary"),
    ],
    id="offcanvas-filters",
    title="Filters",
    is_open=False,
    placement="end",
)

# Toggle button
toggle_button = dbc.Button(
    html.I(className="bi bi-filter me-1"),
    id="open-offcanvas",
    n_clicks=0,
    color="secondary",
    className="",
)

# Results table
results_table = dash_table.DataTable(
    id="results-table",
    columns=[
        {"name": "ID", "id": "id"},
        {"name": "First Name", "id": "first_name"},
        {"name": "Last Name", "id": "last_name"},
        {"name": "Sport", "id": "sport"},
        {"name": "Email", "id": "email"},
        {"name": "Enrollment", "id": "enrollment_status"}
    ],
    page_current=0,
    page_size=20,
    page_action="none",
    style_table={"overflowX": "auto"},
    style_cell={"textAlign": "left"},
)

download_button = html.Div([
    html.Button("Download CSV", id="download-csv-btn", className="btn btn-secondary", n_clicks=0),
    dcc.Download(id="download-csv")
], className="mt-3")

app.layout = html.Div([
    dcc.Store(id='table-rows-store', data=0),
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    Navbar([]).render(),
    dbc.Container([
        offcanvas,
        dbc.Container(
            dbc.Row(
                [dbc.Col(html.H2("Registration Search", className="mb-3")),
                 dbc.Col(toggle_button, width="auto")],
                justify="between",
                align="center",
            ),
        ),

        html.Div(
            "Use the filters to search the Registration Dataset",
            id="no-data-msg",
            className="alert alert-info d-block",
        ),

        dbc.Container(
            [
                results_table,
                html.Div(Pagination().render(), className="mt-2"),
                download_button,
            ],
            id="table-display-container",
            fluid=True,
            className="d-none",
        )
    ]),
    Footer().render(),
])

# 2) Bring in your dashboard content & callbacks
from training_dashboard import layout_body, register_callbacks

# 3) Dash app — the repo’s assets/ CSS will autoload if you copied that folder
external_stylesheets = [dbc.themes.FLATLY]
app = dash.Dash(__name__, external_stylesheets=external_stylesheets, suppress_callback_exceptions=True)
server = app.server

# If you later add OAuth like the Registration Viewer, handle redirects here.
@app.callback(Output("page-content", "children"), Input("url", "href"), prevent_initial_call=False)
def router(_href):
    return layout_body()

# Register all dashboard callbacks
register_callbacks(app)
# ------------------------- Callbacks -------------------------
# Toggle filters panel
@app.callback(
    Output("offcanvas-filters", "is_open"),
    Input("open-offcanvas", "n_clicks"),
    State("offcanvas-filters", "is_open"),
)
def toggle_offcanvas(n_clicks, is_open):
    if n_clicks:
        return not is_open
    return is_open


# Silent init: if no token, bounce to /login; else populate dropdowns
@app.callback(
    Output("redirect-to", "href"),
    Output("filter-role", "options"),
    Output("filter-campus", "options"),
    Output("filter-organization", "options"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    """
    On first tick:
      - If no token, navigate to 'login' (relative URL → works under /content/<id>)
      - If token exists, populate the dropdowns
    """
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if not token:
        # relative path keeps Connect's prefix (/content/<id>) intact
        return "login", no_update, no_update, no_update

    # campus: label=name, value=id
    campus_options = fetch_options("/api/registration/campus/", token, "name", "id")
    # sport org: label=name, value=id
    org_options    = fetch_options("/api/registration/organization/", token, "name", "id")
    # role: label=verbose_name, value=id
    role_options   = fetch_options("/api/registration/role/", token, "verbose_name", "id")

    return no_update, role_options, campus_options, org_options


# Download CSV of all results for current filters
@app.callback(
    Output("download-csv", "data"),
    Input("download-csv-btn", "n_clicks"),
    State("filter-role", "value"),
    State("filter-campus", "value"),
    State("filter-organization", "value"),
    State("filter-placename-scope", "value"),
    State(f"{geo_filters.id}-province", "value"),
    State(f"{geo_filters.id}-location", "value"),
    State(f"{geo_filters.id}-placename", "value"),
    State("data-format-select", "value"),
    prevent_initial_call=True,
)
def download_csv(n_clicks, role, campus, org, placename_scope, province, location, placename, fmt):
    try:
        token = auth.get_token()
        if not token:
            raise PreventUpdate

        filters = {}
        if role:   filters["role_id"] = role
        if campus: filters["campus_id"] = campus
        if org:    filters["sport_org_id"] = org
        if placename_scope: filters["geography_scope"] = placename_scope
        if province: filters["province"] = province
        if location: filters["location"] = location
        if placename: filters["placename"] = placename

        records = [restructure_profile(p, fmt) for p in fetch_profiles(token, filters)]
        df = pd.DataFrame(records)
        return dcc.send_data_frame(df.to_csv, "filtered_profiles.csv", index=False)
    except Exception as e:
        print("download_csv error:", e)
        raise PreventUpdate


# Apply filters + pagination
@app.callback(
    Output("results-table", "data"),
    Output("results-table", "columns"),
    Output("pagination", "max_value"),
    Output("pagination", "active_page"),
    Output("table-rows-store", "data"),
    Input("apply-filters", "n_clicks"),
    Input("pagination", "active_page"),
    Input("results-table", "page_size"),
    State("filter-role", "value"),
    State("filter-campus", "value"),
    State("filter-organization", "value"),
    State("filter-placename-scope", "value"),
    State(f"{geo_filters.id}-province", "value"),
    State(f"{geo_filters.id}-location", "value"),
    State(f"{geo_filters.id}-placename", "value"),
    State("data-format-select", "value"),
)
def apply_filters(n_clicks, active_page, page_size, role_id, campus_id, org_id,
                  placename_scope, province, location, placename, fmt):
    try:
        token = auth.get_token()
    except Exception:
        raise PreventUpdate
    if not token:
        raise PreventUpdate

    ctx = callback_context
    if not ctx.triggered:
        raise PreventUpdate

    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0]
    page = 1 if trigger_id == "apply-filters" else (active_page or 1)

    headers = {"Authorization": f"Bearer {token}"}

    params = {}
    if role_id:   params["role_id"] = role_id
    if campus_id: params["campus_id"] = campus_id
    if org_id:    params["sport_org_id"] = org_id
    if placename_scope: params["geography_scope"] = placename_scope
    if province:  params["province"] = province
    if location:  params["location"] = location
    if placename: params["placename"] = placename

    params["limit"]  = page_size
    params["offset"] = (page - 1) * page_size

    base = globals().get("SITE_URL", "https://apps.csipacific.ca")
    try:
        resp = requests.get(f"{base}/api/registration/profile/", headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print("apply_filters fetch error:", e)
        raise PreventUpdate

    total = payload.get("count", 0) or 0
    total_pages = math.ceil(total / page_size) if total else 1

    rows = [restructure_profile(p, fmt) for p in payload.get("results", [])]
    df = pd.DataFrame(rows)
    columns = [{"name": col, "id": col} for col in df.columns] if not df.empty else [
        {"name": "id", "id": "id"},
    ]

    return df.to_dict("records"), columns, total_pages, page, len(rows)


# Show/hide “no data” banner
@app.callback(
    Output("no-data-msg", "className"),
    Output("no-data-msg", "children"),
    Output("table-display-container", "className"),
    Input("table-rows-store", "data"),
)
def row_callback(rows):
    if not rows:
        return "alert alert-warning d-block", "No Records Found. Adjust your filters and try again.", "d-none"
    else:
        return "d-none", "", ""


if __name__ == "__main__":
    # Locally you can run on 8050; on Connect, the launcher takes over.
    app.run(debug=False, port=8050)
