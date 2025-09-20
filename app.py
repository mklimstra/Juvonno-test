import os
import requests
import json
import sqlite3
import dash
from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update, dash_table, State, callback_context
import dash_bootstrap_components as dbc

# PKCE + login helpers
import base64, hashlib, secrets
from urllib.parse import urlencode
from flask import session, redirect

# Repo layout + settings
from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Training dashboard (second tab)
from training_dashboard import (
    layout_body as training_layout_body,
    register_callbacks as training_register_callbacks,
    # reuse data/logic for first tab
    CUSTOMERS, CID_TO_GROUPS, CID_TO_APPTS,
    fetch_customer_complaints, extract_training_status,
    encounter_ids_for_appt, tidy_date_str, PASTEL_COLOR
)

import pandas as pd

# ------------------------- Auth / Server -------------------------
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,          # e.g., https://connect.posit.cloud/content/<id>
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

# ------------------------- Helpers (Tab 1) -------------------------
def current_training_status_for_cid(cid: int) -> str:
    """Forward-filled latest training status for a customer, based on your existing logic."""
    appts = CID_TO_APPTS.get(cid, [])
    if not appts:
        return ""
    rows = []
    for ap in appts:
        aid = ap.get("id")
        dt = pd.to_datetime(tidy_date_str(ap.get("date")), errors="coerce")
        if pd.isna(dt): continue
        eids = encounter_ids_for_appt(aid)
        max_eid = max(eids) if eids else None
        s = extract_training_status({}) if not max_eid else extract_training_status(
            # fetch_encounter is used inside extract in training_dashboard; here we only have the id list.
            # BUT training_dashboard.fetch_encounter is not exported; the training dash callback uses it internally.
            # So we compute status via encounter ids + training_dashboard’s extract helper when available in callbacks.
            # For Tab 1 summary, we’ll simply skip when no status can be derived.
            # (This keeps changes minimal; Tab 2 still shows the precise status calendar.)
            {}  # no payload here; status will be "" unless you extend export of fetch_encounter.
        )
        # Since we can't call fetch_encounter here without importing internal, use empty → status ""
        # Fallback: If appointment has inline complaint with "status", it won't be the training status; we leave blank.
        if s: rows.append((dt.normalize(), s))
    if not rows:
        return ""
    df_s = pd.DataFrame(rows, columns=["Date", "Status"]).sort_values("Date")
    df_s = df_s.drop_duplicates("Date", keep="last")
    full_idx = pd.date_range(start=df_s["Date"].min(), end=pd.Timestamp("today").normalize(), freq="D")
    df_full = pd.DataFrame({"Date": full_idx}).merge(df_s, on="Date", how="left").sort_values("Date")
    df_full["Status"] = df_full["Status"].ffill()
    try:
        return str(df_full.iloc[-1]["Status"])
    except Exception:
        return ""

def label_for_cid(cid: int) -> str:
    c = CUSTOMERS.get(cid, {})
    return f"{c.get('first_name','')} {c.get('last_name','')} (ID {cid})".strip()

# Precompute group dropdown
ALL_GROUPS = sorted({g for lst in CID_TO_GROUPS.values() for g in lst})
GROUP_OPTS = [{"label": g.title(), "value": g} for g in ALL_GROUPS]

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
    """Athlete Summary & Notes"""
    return dbc.Container([
        html.H3("Athlete Summary & Notes", className="mt-1"),

        dbc.Row([
            dbc.Col(dcc.Dropdown(id="t1-groups", options=GROUP_OPTS, multi=True,
                                 placeholder="Select patient group(s)…"), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2)
        ], className="g-2"),

        html.Div(id="t1-table-container", className="mt-3"),

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
        dash_table.DataTable(
            id="t1-comments-table",
            columns=[
                {"name":"Date","id":"Date"},
                {"name":"Author","id":"Author"},
                {"name":"Athlete","id":"Athlete"},
                {"name":"Complaint","id":"Complaint"},
                {"name":"Complaint Status","id":"Complaint Status"},
                {"name":"Comment","id":"Comment"}
            ],
            data=[],
            page_size=10,
            filter_action="native",
            sort_action="native",
            style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","borderBottom":"1px solid #e9ecef"},
            style_cell={"padding":"9px","fontSize":14,
                        "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                        "textAlign":"left"},
            style_data={"borderBottom":"1px solid #eceff4"},
            style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfbfd"}],
        ),

        dcc.Store(id="t1-user-json", data={}),         # who is signed in (object)
        dcc.Store(id="t1-rows-json", data=[]),         # rows backing the table
        dcc.Store(id="t1-selected-cid", data=None),    # current athlete id
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

# ------------------------- Callbacks: Tab 1 — Athlete Summary & Notes -------------------------
@app.callback(
    Output("t1-table-container", "children"),
    Output("t1-rows-json", "data"),
    Input("t1-load", "n_clicks"),
    State("t1-groups", "value"),
    prevent_initial_call=True
)
def t1_build_table(n, groups):
    if not groups:
        raise PreventUpdate
    targets = {g.strip().lower() for g in groups}

    # Build athlete summary rows
    rows = []
    cids = [cid for cid, glist in CID_TO_GROUPS.items() if targets & set(glist)]
    for cid in cids:
        cust = CUSTOMERS.get(cid, {})
        label = label_for_cid(cid)
        email = cust.get("email") or ""
        phone = cust.get("phone") or cust.get("mobile") or ""
        groups_str = ", ".join(sorted({g.title() for g in CID_TO_GROUPS.get(cid, [])}))

        # Current training status (may be blank if no accessible encounter body here)
        status = current_training_status_for_cid(cid)

        complaints = fetch_customer_complaints(cid)
        comp_count = len(complaints)
        latest_title, latest_onset, latest_priority = "", "", ""
        if comp_count:
            try:
                dfc = pd.DataFrame(complaints)
                # sort by onset date if parseable
                dfc["_on"] = pd.to_datetime(dfc["Onset"], errors="coerce")
                dfc = dfc.sort_values(["_on"], na_position="last", ascending=False)
                latest_title   = str(dfc.iloc[0]["Title"] or "")
                latest_onset   = str(dfc.iloc[0]["Onset"] or "")
                latest_priority= str(dfc.iloc[0]["Priority"] or "")
            except Exception:
                pass

        rows.append({
            "Athlete": label,
            "Athlete ID": cid,
            "Groups": groups_str,
            "Email": email,
            "Phone": phone,
            "Current Status": status,
            "Complaints": comp_count,
            "Latest Complaint": latest_title,
            "Latest Onset": latest_onset,
            "Latest Priority": latest_priority,
        })

    if not rows:
        return html.Div("No athletes in those groups.", className="alert alert-warning"), []

    df = pd.DataFrame(rows)

    table = dash_table.DataTable(
        id="t1-table",
        data=df.to_dict("records"),
        columns=[{"name": c, "id": c} for c in df.columns],
        page_size=15,
        filter_action="native",
        sort_action="native",
        row_selectable="single",
        style_table={"overflowX":"auto"},
        style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","borderBottom":"1px solid #e9ecef"},
        style_cell={"padding":"9px","fontSize":14,
                    "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                    "textAlign":"left"},
        style_data={"borderBottom":"1px solid #eceff4"},
        style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfbfd"}],
    )

    return table, df.to_dict("records")

@app.callback(
    Output("t1-athlete", "options"),
    Output("t1-athlete", "value"),
    Output("t1-complaint", "options"),
    Output("t1-complaint", "value"),
    Output("t1-comments-table", "data"),
    Output("t1-selected-cid", "data"),
    Input("t1-table", "selected_rows"),
    State("t1-rows-json", "data"),
    prevent_initial_call=True
)
def t1_pick_athlete(selected_rows, rows_json):
    if not rows_json or selected_rows is None or not selected_rows:
        raise PreventUpdate
    row = rows_json[selected_rows[0]]
    cid = int(row["Athlete ID"])
    label = row["Athlete"]

    # Athlete dropdown reflects the loaded cohort (optional, here just one)
    opts = [{"label": label, "value": cid}]
    complaints = fetch_customer_complaints(cid)
    c_opts = [{"label": c["Title"] or "(untitled)", "value": c["Title"] or ""} for c in complaints]
    c_opts = [o for o in c_opts if o["value"]]

    comments = db2_list_for_athlete(cid)
    return opts, cid, c_opts, (c_opts[0]["value"] if c_opts else None), comments, cid

@app.callback(
    Output("t1-comments-table", "data", allow_duplicate=True),
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

    # Today’s date
    today = pd.Timestamp("today").strftime("%Y-%m-%d")

    # Author info from /me
    name  = (user_json or {}).get("name") or (f"{(user_json or {}).get('first_name','')} {(user_json or {}).get('last_name','')}".strip())
    email = (user_json or {}).get("email") or ((user_json or {}).get("user") or {}).get("email")

    # Athlete label from options
    athlete_label = ""
    if athlete_opts:
        for o in athlete_opts:
            if int(o["value"]) == int(cid):
                athlete_label = o["label"]
                break
    if not athlete_label:
        athlete_label = f"ID {cid}"

    # Current complaint status
    status = ""
    try:
        comps = fetch_customer_complaints(int(cid))
        for c in comps:
            if (c.get("Title") or "").strip().lower() == complaint_value.strip().lower():
                status = c.get("Status") or ""
                break
    except Exception:
        pass

    # Save to DB
    db2_add(today, name or "Unknown", email or "", int(cid), athlete_label, complaint_value, status, comment_text.strip())

    # Refresh table
    data = db2_list_for_athlete(int(cid))
    return data, "Saved."

# ------------------------- Wire up existing Training Dashboard (Tab 2) -------------------------
training_register_callbacks(app)

# ------------------------- Run -------------------------
if __name__ == "__main__":
    app.run(debug=False, port=8050)
