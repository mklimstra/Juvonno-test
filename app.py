import os
import requests
import sqlite3
import dash
import pandas as pd

from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update, State
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

# PKCE + login helpers
import base64, hashlib, secrets
from urllib.parse import urlencode
from flask import session, redirect

# Repo layout + settings
from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Training dashboard (Tab 2)
from training_dashboard import (
    layout_body as training_layout_body,
    register_callbacks as training_register_callbacks,
    # we still re-use complaints/status helpers for Tab 1 comments, but not customers
    fetch_customer_complaints, tidy_date_str, PASTEL_COLOR
)

from datetime import datetime

# ------------------------- Auth / Server -------------------------
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Cookie/session settings
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

# ------------------------- Minimal local DB for Tab 1 comments -------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "comments.db")

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_date TEXT,
            author_name TEXT,
            author_email TEXT,
            athlete_id INTEGER,
            athlete_label TEXT,
            complaint TEXT,
            complaint_status TEXT,
            comment TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def db2_add(comment_date, author_name, author_email, athlete_id, athlete_label, complaint, complaint_status, comment):
    conn = _db()
    conn.execute("""
        INSERT INTO comments2(comment_date, author_name, author_email, athlete_id, athlete_label,
                              complaint, complaint_status, comment, created_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now'))
    """, (comment_date, author_name, author_email, int(athlete_id) if athlete_id is not None else None,
          athlete_label or "", complaint or "", complaint_status or "", comment or ""))
    conn.commit()
    conn.close()

def db2_list_for_athlete(athlete_id: int):
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        SELECT comment_date, author_name, author_email, athlete_label, complaint, complaint_status, comment
        FROM comments2
        WHERE athlete_id = ?
        ORDER BY comment_date DESC, id DESC
    """, (int(athlete_id),))
    rows = cur.fetchall()
    conn.close()
    return [{
        "Date": r[0], "Author": f"{r[1]} ({r[2]})" if r[2] else r[1],
        "Athlete": r[3], "Complaint": r[4], "Complaint Status": r[5], "Comment": r[6]
    } for r in rows]

# ------------------------- Tab 1 Juvonno customer fetch (NEW, direct API) -------------------------
JUV_API_KEY = os.getenv("JUV_API_KEY")  # MUST be set in Posit Connect
JUV_BASE = "https://csipacific.juvonno.com/api"
JUV_HEADERS = {"accept": "application/json"}

def _juv_get(path: str, **params):
    """Direct GET to Juvonno, always passing api_key from env."""
    if not JUV_API_KEY:
        raise RuntimeError("JUV_API_KEY is not set in the environment.")
    params = dict(params or {})
    params.setdefault("api_key", JUV_API_KEY)
    url = f"{JUV_BASE}/{path.lstrip('/')}"
    r = requests.get(url, params=params, headers=JUV_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()

def t1_fetch_customers_and_groups():
    """
    Fetch all ACTIVE customers with groups using /customers/list, paging 100 at a time.
    Returns: (customers_by_id: dict, cid_to_groups: dict of lowercase group names)
    """
    out = []
    page = 1
    while True:
        js = _juv_get("customers/list", include="groups", page=page, count=100, status="ACTIVE")
        rows = js.get("list", js)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 100:
            break
        page += 1

    customers = {c["id"]: c for c in out if isinstance(c, dict) and c.get("id")}
    def _norm(s): return (s or "").strip().lower()
    def groups_of(cust):
        src = cust.get("groups") if "groups" in cust else cust.get("group")
        names = []
        if isinstance(src, list):
            for it in src:
                if isinstance(it, str):
                    names.append(_norm(it))
                elif isinstance(it, dict) and it.get("name"):
                    names.append(_norm(it["name"]))
        elif isinstance(src, dict) and src.get("name"):
            names.append(_norm(src["name"]))
        elif isinstance(src, str):
            names.append(_norm(src))
        return names

    cid_to_groups = {cid: groups_of(c) for cid, c in customers.items()}
    return customers, cid_to_groups

# ------------------------- Dash app -------------------------
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css",
    ],
    suppress_callback_exceptions=True,
    title="CSI Apps — Athlete Tools",
)

# ------------------------- Layout -------------------------
def tab1_layout():
    """Athlete Summary & Notes (AG Grid version)"""
    return dbc.Container([
        html.H3("Athlete Summary & Notes", className="mt-1"),

        dbc.Row([
            dbc.Col(dcc.Dropdown(id="t1-groups", options=[], multi=True,   # options filled at runtime now
                                 placeholder="Select patient group(s)…"), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
            dbc.Col(dcc.Input(id="t1-quick-filter", placeholder="Search table…", type="text",
                              className="form-control"), md=4)
        ], className="g-2"),

        html.Div(id="t1-grid-container", className="mt-3"),

        dbc.Row([
            dbc.Col([
                html.Label("Selected Athlete"),
                dcc.Dropdown(id="t1-athlete", options=[], value=None, placeholder="Choose an athlete…")
            ], md=4),
            dbc.Col([
                html.Label("Complaint"),
                dcc.Dropdown(id="t1-complaint", options=[], value=None, placeholder="Pick a complaint…")
            ], md=4),
            dbc.Col([
                html.Label("Comment"),
                dcc.Textarea(id="t1-comment", style={"width":"100%","height":"84px"},
                             placeholder="Add a note about this athlete/complaint…")
            ], md=4),
        ], className="g-2 mt-3"),

        dbc.Row([
            dbc.Col(dbc.Button("Save Comment", id="t1-save", color="success"), width="auto"),
            dbc.Col(html.Div(id="t1-save-msg", className="text-muted ms-2"), width="auto"),
        ], className="mt-2"),

        html.Hr(),
        html.H5("Comment History"),
        dag.AgGrid(
            id="t1-comments-grid",
            columnDefs=[
                {"headerName": "Date", "field": "Date", "filter": True, "sortable": True},
                {"headerName": "Author", "field": "Author", "filter": True, "sortable": True},
                {"headerName": "Athlete", "field": "Athlete", "filter": True, "sortable": True},
                {"headerName": "Complaint", "field": "Complaint", "filter": True, "sortable": True},
                {"headerName": "Complaint Status", "field": "Complaint Status", "filter": True, "sortable": True},
                {"headerName": "Comment", "field": "Comment", "filter": True, "wrapText": True, "autoHeight": True},
            ],
            rowData=[],
            defaultColDef={
                "resizable": True,
                "filter": True,
                "sortable": True,
                "floatingFilter": True,
            },
            dashGridOptions={"rowSelection": "single", "animateRows": True, "pagination": True, "paginationPageSize": 10},
            className="ag-theme-quartz",
            style={"height": "360px", "width": "100%"},
        ),

        # stores for Tab 1
        dcc.Store(id="t1-user-json", data={}),         # who is signed in (object)
        dcc.Store(id="t1-rows-json", data=[]),         # source rows backing the grid
        dcc.Store(id="t1-selected-cid", data=None),    # current athlete id
        dcc.Store(id="t1-customers-json", data={}),    # fresh customers from Juvonno
        dcc.Store(id="t1-groups-map", data={}),        # cid -> groups (lowercase)
    ], fluid=True)

app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    Navbar([html.Span(id="navbar-user", className="text-white-50 small", children="")]).render(),

    dbc.Container([
        dbc.Tabs([
            dbc.Tab(tab1_layout(), label="Athlete Summary & Notes", tab_id="tab1"),
            dbc.Tab(training_layout_body(), label="Training Status", tab_id="tab2"),
        ], id="tabs", active_tab="tab1", persistence=True, persistence_type="session")
    ], fluid=True),

    Footer().render(),
])

# ------------------------- Callbacks: global auth and navbar -------------------------
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
        return "login"
    return no_update

@app.callback(
    Output("navbar-user", "children"),
    Output("t1-user-json", "data"),
    Input("init-interval", "n_intervals"),
    Input("user-refresh", "n_intervals"),
    State("navbar-user", "children"),
    prevent_initial_call=False
)
def show_current_user(_n1, _n2, current_children):
    """Render who is signed in and stash the raw user info for Tab 1 comments."""
    try:
        token = auth.get_token()
    except Exception:
        token = None

    sign_in_link = html.A("Sign in", href="login", className="link-light text-decoration-none")

    if not token:
        return (current_children if current_children else sign_in_link), dash.no_update

    try:
        r = requests.get(f"{SITE_URL}/api/csiauth/me/", headers={"Authorization": f"Bearer {token}"}, timeout=6)
        if r.status_code != 200:
            return (current_children if current_children else html.A("Re-authenticate", href="login",
                                                                     className="link-light text-decoration-none")), \
                   dash.no_update
        me = r.json() or {}
        name  = (me.get("name") or me.get("display_name") or me.get("full_name") or
                 f"{me.get('first_name','')} {me.get('last_name','')}".strip())
        email = me.get("email") or (me.get("user") or {}).get("email")
        label = f"Signed in as {name} ({email})" if (name and email) else f"Signed in as {name or email or 'user'}"
        return label, me
    except Exception:
        return (current_children if current_children else sign_in_link), dash.no_update

# ------------------------- Tab 1 bootstrap: fetch groups & customers once user opens the tab -------------------------
@app.callback(
    Output("t1-groups", "options"),
    Output("t1-customers-json", "data"),
    Output("t1-groups-map", "data"),
    Input("tabs", "active_tab"),
    prevent_initial_call=False
)
def t1_init(active_tab):
    if active_tab != "tab1":
        raise PreventUpdate
    # fetch customers & groups fresh
    try:
        customers, cid_to_groups = t1_fetch_customers_and_groups()
    except Exception as e:
        # show a disabled dropdown with the error info in placeholder
        return [], {}, {}
    # build group options from fresh data
    all_groups = sorted({g for lst in cid_to_groups.values() for g in (lst or [])})
    group_opts = [{"label": g.title(), "value": g} for g in all_groups]
    return group_opts, customers, cid_to_groups

# ------------------------- Callbacks: Tab 1 — Athlete Summary & Notes (AG Grid) -------------------------

# Rebuild the grid when clicking Load OR when group selection changes
@app.callback(
    Output("t1-grid-container", "children"),
    Output("t1-rows-json", "data"),
    Input("t1-load", "n_clicks"),
    Input("t1-groups", "value"),
    State("t1-customers-json", "data"),
    State("t1-groups-map", "data"),
    prevent_initial_call=True
)
def t1_build_grid(_n_clicks, groups, customers_json, cid_to_groups_json):
    # sanity: must have fresh customers/groups from API
    if not customers_json or not cid_to_groups_json:
        return html.Div("Unable to fetch athletes. Check JUV_API_KEY and network.", className="alert alert-danger"), []

    if not groups:
        return html.Div("Select at least one patient group.", className="alert alert-info"), []

    # Normalize groups for matching
    targets = { (g or "").strip().lower() for g in (groups if isinstance(groups, list) else [groups]) }

    # Resolve matching athletes using the *fresh* cid→groups mapping
    cids = [int(cid) for cid, glist in cid_to_groups_json.items() if targets & set(glist or [])]

    if not cids:
        return html.Div("No athletes found for the selected group(s).", className="alert alert-warning"), []

    rows = []
    for cid in cids:
        cust = customers_json.get(str(cid)) or customers_json.get(int(cid)) or {}
        label = f"{cust.get('first_name','')} {cust.get('last_name','')} (ID {cid})".strip()
        groups_str = ", ".join(sorted({(g or "").title() for g in (cid_to_groups_json.get(str(cid)) or cid_to_groups_json.get(cid) or [])}))

        # Complaints summary for styling and latest columns
        complaints = fetch_customer_complaints(cid)
        comp_count = len(complaints)
        latest_title, latest_onset, latest_priority = "", "", ""
        if comp_count:
            try:
                dfc = pd.DataFrame(complaints)
                dfc["_on"] = pd.to_datetime(dfc["Onset"], errors="coerce")
                dfc = dfc.sort_values(["_on"], na_position="last", ascending=False)
                latest_title    = str(dfc.iloc[0]["Title"] or "")
                latest_onset    = str(dfc.iloc[0]["Onset"] or "")
                latest_priority = str(dfc.iloc[0]["Priority"] or "")
            except Exception:
                pass

        # Current Status: leave blank here (Tab 2 shows authoritative status)
        status = ""
        status_color = PASTEL_COLOR.get(status, "#e6e6e6")

        rows.append({
            "CID": cid,  # hidden technical column for selection
            "Athlete": label,
            "Groups": groups_str,
            "Current Status": status,
            "StatusColor": status_color,
            "Complaints": comp_count,
            "Latest Complaint": latest_title,
            "Latest Onset": latest_onset,
            "Latest Priority": latest_priority,
        })

    # Grid with pill renderers
    col_defs = [
        {"headerName": "Athlete", "field": "Athlete", "pinned": "left", "filter": True, "sortable": True,
         "checkboxSelection": True, "headerCheckboxSelection": False},
        {
            "headerName": "Groups", "field": "Groups", "filter": True, "sortable": True, "autoHeight": True, "wrapText": True,
            "cellRenderer": dag.JsCode("""
                function(params) {
                  if (!params.value) return '';
                  const items = params.value.split(',').map(s => s.trim()).filter(Boolean);
                  return items.map(g => `<span style="
                      display:inline-block; margin:2px 4px 2px 0; padding:2px 8px;
                      border-radius:999px; background:#f1f3f5; border:1px solid #e1e5ea;
                      font-size:12px;">${g}</span>`).join(' ');
                }
            """)
        },
        {
            "headerName": "Current Status", "field": "Current Status", "filter": True, "sortable": True,
            "cellRenderer": dag.JsCode("""
                function(params) {
                  const text = params.value || '';
                  const color = (params.data && params.data.StatusColor) ? params.data.StatusColor : '#e6e6e6';
                  const dot = `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;
                                 background:${color};border:1px solid rgba(0,0,0,.18);margin-right:6px;"></span>`;
                  const pill = `<span style="display:inline-block; padding:2px 8px; border-radius:999px;
                                   background:${color}; border:1px solid rgba(0,0,0,.10); font-size:12px;">${text || '—'}</span>`;
                  return dot + pill;
                }
            """)
        },
        {
            "headerName": "Complaints", "field": "Complaints", "filter": "agNumberColumnFilter", "sortable": True,
            "width": 130,
            "cellRenderer": dag.JsCode("""
                function(params) {
                  const v = Number(params.value || 0);
                  let bg = '#eef2ff'; // light indigo
                  if (v >= 3) bg = '#ffe8cc'; // light orange
                  else if (v === 0) bg = '#f1f3f5'; // light grey
                  return `<span style="display:inline-block; padding:2px 8px; border-radius:999px;
                           background:${bg}; border:1px solid #e1e5ea; font-weight:600;">${v}</span>`;
                }
            """)
        },
        {"headerName": "Latest Complaint", "field": "Latest Complaint", "filter": True, "sortable": True, "flex": 1},
        {"headerName": "Latest Onset", "field": "Latest Onset", "filter": True, "sortable": True, "width": 140},
        {"headerName": "Latest Priority", "field": "Latest Priority", "filter": True, "sortable": True, "width": 160},
        {"headerName": "CID", "field": "CID", "hide": True},  # hidden tech column
    ]

    grid = dag.AgGrid(
        id="t1-grid",
        columnDefs=col_defs,
        rowData=rows,
        defaultColDef={
            "resizable": True,
            "filter": True,
            "sortable": True,
            "floatingFilter": True,
        },
        dashGridOptions={
            "rowSelection": "single",
            "animateRows": True,
            "pagination": True,
            "paginationPageSize": 15,
            "suppressRowClickSelection": False,
            "ensureDomOrder": True,
            "domLayout": "normal"
        },
        className="ag-theme-quartz",
        style={"height": "520px", "width": "100%"},
    )

    return grid, rows

# Quick filter for AG Grid
@app.callback(
    Output("t1-grid", "quickFilterText"),
    Input("t1-quick-filter", "value"),
    prevent_initial_call=True
)
def t1_quick_filter(val):
    return val or ""

# Selecting an athlete from the grid populates the athlete dropdown, complaint options, and loads comments
@app.callback(
    Output("t1-athlete", "options"),
    Output("t1-athlete", "value"),
    Output("t1-complaint", "options"),
    Output("t1-complaint", "value"),
    Output("t1-comments-grid", "rowData"),
    Output("t1-selected-cid", "data"),
    Input("t1-grid", "selectedRows"),
    prevent_initial_call=True
)
def t1_pick_athlete(selected_rows):
    if not selected_rows:
        raise PreventUpdate
    row = selected_rows[0]
    cid = int(row.get("CID"))
    label = row.get("Athlete")

    opts = [{"label": label, "value": cid}]
    complaints = fetch_customer_complaints(cid)
    c_opts = [{"label": c["Title"] or "(untitled)", "value": c["Title"] or ""} for c in complaints if (c.get("Title") or "").strip()]
    comments = db2_list_for_athlete(cid)

    return opts, cid, c_opts, (c_opts[0]["value"] if c_opts else None), comments, cid

# Save comment with today's date, current user, athlete, complaint, and current complaint status
@app.callback(
    Output("t1-comments-grid", "rowData", allow_duplicate=True),
    Output("t1-save-msg", "children"),
    State("t1-selected-cid", "data"),
    State("t1-athlete", "options"),
    State("t1-complaint", "value"),
    State("t1-comment", "value"),
    State("t1-user-json", "data"),
    Input("t1-save", "n_clicks"),
    prevent_initial_call=True
)
def t1_save_comment(cid, athlete_opts, complaint_value, comment_text, user_json, n_clicks):
    if not n_clicks:
        raise PreventUpdate
    if not cid or not complaint_value or not (comment_text or "").strip():
        return dash.no_update, "Please choose an athlete, a complaint, and enter a comment."

    today = datetime.utcnow().strftime("%Y-%m-%d")

    name  = (user_json or {}).get("name") or (f"{(user_json or {}).get('first_name','')} {(user_json or {}).get('last_name','')}".strip())
    email = (user_json or {}).get("email") or ((user_json or {}).get("user") or {}).get("email")

    athlete_label = ""
    if athlete_opts:
        for o in athlete_opts:
            if int(o["value"]) == int(cid):
                athlete_label = o["label"]; break
    if not athlete_label:
        athlete_label = f"ID {cid}"

    # Current complaint status from merged complaint sources
    status = ""
    try:
        comps = fetch_customer_complaints(int(cid))
        for c in comps:
            if (c.get("Title") or "").strip().lower() == (complaint_value or "").strip().lower():
                status = c.get("Status") or ""
                break
    except Exception:
        pass

    db2_add(today, name or "Unknown", email or "", int(cid), athlete_label, complaint_value, status, (comment_text or "").strip())
    data = db2_list_for_athlete(int(cid))
    return data, "Saved."

# ------------------------- Wire up existing Training Dashboard (Tab 2) -------------------------
training_register_callbacks(app)

# ------------------------- Run -------------------------
if __name__ == "__main__":
    app.run(debug=False, port=8050)
