import os
import json
import requests
import sqlite3
import pandas as pd
from datetime import datetime

import dash
from dash import Dash, dcc, html, Input, Output, State, no_update, dash_table, callback_context
import dash_bootstrap_components as dbc

# --- Repo-style imports (keep styling / structure identical) ---
from layout import Footer, Navbar  # (Pagination / GeographyFilters not used here)
from settings import *             # expects AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET
from dash_auth_external import DashAuthExternal

# --- Bring in your existing training dashboard (unchanged) ---
import training_dashboard as td


# ======================================================================================
# Auth / Server (exactly like repo style)
# ======================================================================================
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Serve /assets (same as repo)
here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"


# ======================================================================================
# Dash app
# ======================================================================================
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css",
    ],
    suppress_callback_exceptions=True,
)

# --------------------------------------------------------------------------------------
# Simple helpers reusing training_dashboard data
# --------------------------------------------------------------------------------------
def _athletes_for_groups(selected_groups):
    """Return rows for the table using the same group mapping td.CID_TO_GROUPS/CUSTOMERS."""
    if not selected_groups:
        return []
    wanted = {td._norm(g) for g in selected_groups}
    out = []
    for cid, cust in td.CUSTOMERS.items():
        groups = td.CID_TO_GROUPS.get(cid, [])
        if set(groups) & wanted:
            label = f"{cust.get('first_name','')} {cust.get('last_name','')} (ID {cid})".strip()
            out.append({
                "Athlete": label,
                "Groups": ", ".join(g.title() for g in groups),
                "Complaints": len(td.fetch_customer_complaints(cid)),
                "_cid": cid,  # internal id for selection
            })
    # sort by label
    out.sort(key=lambda r: r["Athlete"].lower())
    return out

def _current_status_for_cid(cid: int) -> str:
    """Same forward-fill approach as in the training dashboard summary."""
    appts = td.CID_TO_APPTS.get(cid, [])
    status_rows = []
    for ap in appts:
        aid = ap.get("id")
        ds = td.tidy_date_str(ap.get("date"))
        dt = pd.to_datetime(ds, errors="coerce")
        if pd.isna(dt):
            continue
        eids = td.encounter_ids_for_appt(aid)
        max_eid = max(eids) if eids else None
        s = td.extract_training_status(td.fetch_encounter(max_eid)) if max_eid else ""
        if s:
            status_rows.append((dt.normalize(), s))
    if not status_rows:
        return ""
    df_s = pd.DataFrame(status_rows, columns=["Date", "Status"]).sort_values("Date")
    df_s = df_s.drop_duplicates("Date", keep="last")
    full_idx = pd.date_range(start=df_s["Date"].min(), end=pd.Timestamp("today").normalize(), freq="D")
    df_full = pd.DataFrame({"Date": full_idx}).merge(df_s, on="Date", how="left").sort_values("Date")
    df_full["Status"] = df_full["Status"].ffill()
    return str(df_full.iloc[-1]["Status"]) if not df_full.empty else ""


# --------------------------------------------------------------------------------------
# Tab 1: Athlete List & Comments (fast + simple)
# --------------------------------------------------------------------------------------
def tab1_layout():
    return dbc.Container([
        html.H3("Athlete List & Comments", className="mt-2"),

        dbc.Row([
            dbc.Col(dcc.Dropdown(id="t1-group-dd",
                                 options=td.GROUP_OPTS,
                                 multi=True,
                                 placeholder="Select patient group(s)…"), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
        ], className="g-2 mb-2"),

        dbc.Alert(id="t1-msg", is_open=False, color="danger", duration=0, className="mb-2"),

        dash_table.DataTable(
            id="t1-athlete-table",
            columns=[
                {"name": "Athlete", "id": "Athlete"},
                {"name": "Groups", "id": "Groups"},
                {"name": "Complaints", "id": "Complaints"},
            ],
            page_size=12,
            style_table={"overflowX": "auto"},
            style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
            style_cell={
                "padding": "8px",
                "fontSize": 14,
                "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                "textAlign": "left",
            },
            style_data={"borderBottom": "1px solid #eceff4"},
            row_selectable="single",
            selected_rows=[],
            data=[],
        ),

        html.Hr(),

        # Active athlete + complaint + comment editor
        dbc.Row([
            dbc.Col([
                html.Div([html.Span("Active athlete: ", className="fw-semibold"),
                          html.Span(id="t1-active-athlete-label", children="—")]),
                html.Div(id="t1-active-athlete-id", style={"display": "none"}),
            ], md=6),
            dbc.Col(dcc.Dropdown(
                id="t1-complaint-dd",
                options=[],
                placeholder="Select complaint…",
                clearable=True
            ), md=6),
        ], className="g-2 mb-2"),

        dbc.Row([
            dbc.Col(dcc.DatePickerSingle(
                id="t1-comment-date",
                display_format="YYYY-MM-DD",
                date=datetime.utcnow().strftime("%Y-%m-%d")
            ), md=3),
            dbc.Col(dcc.Textarea(
                id="t1-comment-text",
                placeholder="Write a comment about this athlete + complaint…",
                style={"width": "100%", "height": "80px"}
            ), md=7),
            dbc.Col(dbc.Button("Save", id="t1-comment-save", color="success", className="w-100"), md=2),
        ], className="g-2"),

        html.Div(id="t1-comment-hint", className="text-muted", style={"fontSize": "12px"}),

        html.Hr(),

        dash_table.DataTable(
            id="t1-comments-table",
            columns=[
                {"name": "Date", "id": "Date"},
                {"name": "Complaint", "id": "Complaint"},
                {"name": "Status", "id": "Status"},
                {"name": "Author", "id": "Author"},
                {"name": "Comment", "id": "Comment"},
            ],
            page_size=10,
            style_table={"overflowX": "auto"},
            style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa"},
            style_cell={
                "padding": "8px",
                "fontSize": 13,
                "fontFamily": "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                "textAlign": "left",
                "whiteSpace": "normal",
                "height": "auto",
            },
            style_data={"borderBottom": "1px solid #eceff4"},
            data=[],
        ),
    ], fluid=True)


# --------------------------------------------------------------------------------------
# Tab 2: Your original training dashboard (unchanged)
# --------------------------------------------------------------------------------------
tab2_layout = td.layout_body()
td.register_callbacks(app)  # keep your existing callbacks


# --------------------------------------------------------------------------------------
# Navbar (repo style) + Tabs + Footer
# --------------------------------------------------------------------------------------
app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),

    # repo boot-sequence interval
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    # refresh the user badge every 60s
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    # Navbar with right-slot user label (repo Navbar takes a list; we pass a span)
    Navbar([
        html.Span(id="navbar-user", className="text-white-50 small", children="")
    ]).render(),

    dbc.Container([
        dcc.Tabs(id="tabs", value="tab1", children=[
            dcc.Tab(label="Athlete List & Comments", value="tab1"),
            dcc.Tab(label="Training Dashboard", value="tab2"),
        ], persistence=True, persistence_type="session"),
        html.Div(id="tab-content", className="mt-3"),
    ], fluid=True),

    Footer().render(),
])


# ======================================================================================
# Comments DB for Tab 1 (separate table, same SQLite file as training_dashboard)
# ======================================================================================
T1_DB_PATH = td.DB_PATH

def _t1_db_conn():
    conn = sqlite3.connect(T1_DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS t1_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            customer_label TEXT,
            complaint TEXT,
            status TEXT,
            author_name TEXT,
            author_email TEXT,
            date TEXT,
            comment TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

def t1_db_add_comment(customer_id: int, customer_label: str, date_str: str, comment: str,
                      complaint: str, status: str, author_name: str = "", author_email: str = ""):
    conn = _t1_db_conn()
    conn.execute(
        """INSERT INTO t1_comments(customer_id, customer_label, complaint, status,
                                   author_name, author_email, date, comment, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (int(customer_id) if customer_id is not None else None,
         customer_label or "", complaint or "", status or "",
         author_name or "", author_email or "",
         date_str or datetime.utcnow().strftime("%Y-%m-%d"),
         comment or "",
         datetime.utcnow().isoformat(timespec="seconds"))
    )
    conn.commit()
    conn.close()

def t1_db_list_comments(customer_id: int | None) -> list[dict]:
    conn = _t1_db_conn()
    cur = conn.cursor()
    if customer_id:
        cur.execute("""
            SELECT date, customer_label, complaint, status, author_name, author_email, comment
            FROM t1_comments
            WHERE customer_id = ?
            ORDER BY date ASC, id ASC
        """, (int(customer_id),))
    else:
        cur.execute("""
            SELECT date, customer_label, complaint, status, author_name, author_email, comment
            FROM t1_comments
            ORDER BY date ASC, id ASC
        """)
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "Date": r[0],
            "Complaint": r[2],
            "Status": r[3],
            "Author": (r[4] or "") if not r[5] else f"{r[4]} ({r[5]})",
            "Comment": r[6],
        })
    return out


# ======================================================================================
# Callbacks — repo flow + tabs
# ======================================================================================

# 1) Repo-like initial view: if no token, send user to /login; else stay put
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
        return "login"   # relative keeps Connect path intact
    return no_update


# 2) Navbar user badge (same styling, “Signed in as …”)
@app.callback(
    Output("navbar-user", "children"),
    Input("user-refresh", "n_intervals")
)
def show_user_badge(_):
    try:
        token = auth.get_token()
    except Exception:
        token = None
    if not token:
        return "Sign in"

    try:
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(f"{SITE_URL}/api/csiauth/me/", headers=headers, timeout=6)
        if resp.status_code != 200:
            return "Signed in"
        data = resp.json()
        first = data.get("first_name") or ""
        last  = data.get("last_name") or ""
        email = data.get("email") or ""
        name  = f"{first} {last}".strip() or email or "Signed in"
        return f"Signed in as {name}"
    except Exception:
        return "Signed in"


# 3) Tab switcher
@app.callback(
    Output("tab-content", "children"),
    Input("tabs", "value")
)
def render_tab(tabval):
    if tabval == "tab1":
        return tab1_layout()
    return tab2_layout


# 4) Load athletes on Tab 1
@app.callback(
    Output("t1-athlete-table", "data"),
    Output("t1-athlete-table", "selected_rows"),
    Output("t1-msg", "children"),
    Output("t1-msg", "is_open"),
    Input("t1-load", "n_clicks"),
    State("t1-group-dd", "value"),
    prevent_initial_call=True
)
def t1_load_athletes(n, groups):
    if not groups:
        return [], [], "Select at least one group.", True
    if os.getenv("JUV_API_KEY") is None:
        return [], [], "Missing JUV_API_KEY (set it in Posit → Variables).", True
    try:
        rows = _athletes_for_groups(groups)
        if not rows:
            return [], [], "No athletes found for those groups.", True
        return rows, [], "", False
    except Exception as e:
        return [], [], f"Error loading athletes: {e}", True


# 5) Select an athlete row → set active athlete, load complaints + comments
@app.callback(
    Output("t1-active-athlete-label", "children"),
    Output("t1-active-athlete-id", "children"),
    Output("t1-complaint-dd", "options"),
    Output("t1-comments-table", "data"),
    Output("t1-comment-hint", "children"),
    Input("t1-athlete-table", "selected_rows"),
    State("t1-athlete-table", "data"),
    prevent_initial_call=True
)
def t1_pick_athlete(selected_rows, table_data):
    if not selected_rows or not table_data:
        return "—", "", [], [], "Select an athlete above; comments will filter to that athlete."
    row = table_data[selected_rows[0]]
    label = row.get("Athlete", "—")
    cid = int(row.get("_cid")) if row.get("_cid") is not None else None
    if not cid:
        return "—", "", [], [], "Select an athlete above; comments will filter to that athlete."

    # Complaint options from Juvonno
    try:
        comps = td.fetch_customer_complaints(cid)
        comp_opts = [{"label": c["Title"], "value": c["Title"]} for c in comps if c.get("Title")]
    except Exception:
        comp_opts = []

    comments = t1_db_list_comments(cid)
    return label, str(cid), comp_opts, comments, f"Adding comments for: {label}"


# 6) Save a comment → refresh table
@app.callback(
    Output("t1-comments-table", "data", allow_duplicate=True),
    Input("t1-comment-save", "n_clicks"),
    State("t1-active-athlete-id", "children"),
    State("t1-active-athlete-label", "children"),
    State("t1-complaint-dd", "value"),
    State("t1-comment-date", "date"),
    State("t1-comment-text", "value"),
    prevent_initial_call=True
)
def t1_save_comment(n, cid_str, label, complaint, date_str, text):
    if not n or not cid_str or not text or not text.strip():
        raise dash.exceptions.PreventUpdate
    try:
        cid = int(cid_str)
    except Exception:
        raise dash.exceptions.PreventUpdate

    # compute current status right now (fast)
    status = _current_status_for_cid(cid)

    # Optional: if you want author from auth/me:
    try:
        token = auth.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(f"{SITE_URL}/api/csiauth/me/", headers=headers, timeout=5)
        if r.status_code == 200:
            me = r.json()
            author_name = f"{(me.get('first_name') or '').strip()} {(me.get('last_name') or '').strip()}".strip()
            author_email = me.get('email') or ""
        else:
            author_name = ""
            author_email = ""
    except Exception:
        author_name = ""
        author_email = ""

    t1_db_add_comment(
        customer_id=cid,
        customer_label=label or f"ID {cid}",
        date_str=date_str or datetime.utcnow().strftime("%Y-%m-%d"),
        comment=text.strip(),
        complaint=complaint or "",
        status=status or "",
        author_name=author_name,
        author_email=author_email
    )

    return t1_db_list_comments(cid)


if __name__ == "__main__":
    app.run(debug=False, port=8050)
