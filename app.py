import os, math, json, requests, sqlite3, traceback
from datetime import datetime

import pandas as pd
import dash
import dash_bootstrap_components as dbc
from dash import Dash, html, dcc, Input, Output, State, no_update, dash_table, callback_context

from dash_auth_external import DashAuthExternal

# ── Repo-style layout & utilities
from layout import Footer, Navbar, Pagination, GeographyFilters
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# ── Import training dashboard bits (we reuse its data + DB helpers)
import training_dashboard as td


# ======================================================================================
# Auth / Flask server (same style as the repo)
# ======================================================================================
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Static assets (repo style)
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

# ======================================================================================
# Small helpers for HTML pills / dots (single-line for HTML rendering)
# ======================================================================================
def pill(text: str, bg_hex: str, border="#e3e6eb", fg="#111"):
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f'background:{bg_hex};color:{fg};border:1px solid {border};'
        f'font-size:12px;line-height:18px;white-space:nowrap;">{text}</span>'
    )

def dot(hex_color: str, size=10, mr=8):
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'border-radius:50%;background:{hex_color};margin-right:{mr}px;'
        f'border:1px solid rgba(0,0,0,.25)"></span>'
    )

# Light tag color
PILL_BG = "#eef2f7"

# ======================================================================================
# Tab 1 (Overview) layout — group → load → customers table + comments
# Uses the SAME data already fetched by training_dashboard.py (CUSTOMERS, groups, etc.)
# ======================================================================================

def tab1_layout():
    return dbc.Container([
        html.H3("Overview", className="mt-3 mb-2"),

        # Group selector + Load
        dbc.Row([
            dbc.Col(dcc.Dropdown(
                id="t1-group-dd",
                options=td.GROUP_OPTS,   # same options as training dashboard
                multi=True,
                placeholder="Select patient group(s)…",
            ), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
        ], className="g-2 mb-2"),

        dbc.Alert(id="t1-msg", color="danger", is_open=False, duration=0),

        # Athletes table
        html.Div(id="t1-grid-container"),

        # Comment composer
        dbc.Card([
            dbc.CardHeader("Add Comment", className="bg-light"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(dbc.Input(id="t1-comment-user", placeholder="Signed in user (auto)", disabled=True), md=3),
                    dbc.Col(dcc.DatePickerSingle(id="t1-comment-date", display_format="YYYY-MM-DD"), md=3),
                    dbc.Col(dcc.Dropdown(id="t1-complaint-dd", placeholder="Select complaint…"), md=3),
                    dbc.Col(dbc.Button("Save Comment", id="t1-save-comment", color="success", className="w-100"), md=3),
                ], className="g-2 mb-2"),
                dcc.Textarea(
                    id="t1-comment-text",
                    placeholder="Write your note…",
                    style={"width":"100%","height":"80px"}
                ),
                html.Div(id="t1-comment-hint", className="text-muted mt-2", style={"fontSize":"12px"}),
            ])
        ], className="mb-3", style={"border":"1px solid #e9ecef","borderRadius":"0.5rem"}),

        # Comment history (for the selected athlete)
        dbc.Card([
            dbc.CardHeader("Comment History", className="bg-light"),
            dbc.CardBody([
                dash_table.DataTable(
                    id="t1-comments-table",
                    columns=[
                        {"name":"Date", "id":"Date"},
                        {"name":"Comment", "id":"Comment"},
                        {"name":"Athlete", "id":"Athlete"},
                    ],
                    data=[],
                    page_size=8,
                    sort_action="native",
                    filter_action="native",
                    style_header={"fontWeight":"600","backgroundColor":"#f8f9fa"},
                    style_cell={"padding":"8px","fontSize":13,
                                "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                                "textAlign":"left"},
                    style_data={"whiteSpace":"normal","height":"auto","borderBottom":"1px solid #eee"},
                )
            ])
        ], className="mb-4", style={"border":"1px solid #e9ecef","borderRadius":"0.5rem"}),

        # Stores
        dcc.Store(id="t1-rows-json", data=[]),
        dcc.Store(id="t1-selected-athlete", data=None),
    ], fluid=True)


# ======================================================================================
# Page layout: repo-style Navbar + Footer with Tabs
# ======================================================================================

app.layout = html.Div([
    # mirrors repo flow (and lets us set navbar “signed in” later)
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    # refresh who’s signed in every 60s
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    Navbar([
        html.Span(id="navbar-user", className="text-white-50 small", children="")
    ]).render(),

    dbc.Container([
        dcc.Tabs(id="main-tabs", value="tab1", children=[
            dcc.Tab(label="Overview", value="tab1"),
            dcc.Tab(label="Training Dashboard", value="tab2"),
        ], persistence=True, persistence_type="session", parent_className="mb-3"),

        html.Div(id="tabs-content")
    ], fluid=True),

    Footer().render(),
])


# ======================================================================================
# Callbacks — shared navbar “who am I?” (repo style)
# ======================================================================================

@app.callback(
    Output("redirect-to", "href"),
    Output("navbar-user", "children"),
    Input("init-interval", "n_intervals"),
    Input("user-refresh", "n_intervals"),
)
def init_and_user_label(_n1, _n2):
    """On load and every minute, fetch /api/csiauth/me/ with the bearer token to show the user."""
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if not token:
        # If not authenticated yet, push to /login (relative keeps Connect prefix)
        return "login", "Sign in"

    # Authenticated: fetch user
    try:
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(f"{SITE_URL}/api/csiauth/me/", headers=headers, timeout=6)
        if resp.ok:
            data = resp.json()
            fn = data.get("first_name") or ""
            ln = data.get("last_name") or ""
            lbl = f"Signed in as: {fn} {ln}".strip()
        else:
            lbl = "Signed in"
    except Exception:
        lbl = "Signed in"

    return no_update, lbl


# ======================================================================================
# Tabs content loader
# ======================================================================================
@app.callback(
    Output("tabs-content", "children"),
    Input("main-tabs", "value")
)
def render_tab(tab):
    if tab == "tab1":
        return tab1_layout()
    else:
        # Tab 2: use the training dashboard’s layout
        return td.layout_body()


# ======================================================================================
# Tab 1 — callbacks
# ======================================================================================

# Populate the Overview table after “Load”
@app.callback(
    Output("t1-grid-container", "children"),
    Output("t1-rows-json", "data"),
    Output("t1-msg", "children"),
    Output("t1-msg", "is_open"),
    Input("t1-load", "n_clicks"),
    State("t1-group-dd", "value"),
    prevent_initial_call=True
)
def t1_load_customers(n_clicks, group_values):
    try:
        if not group_values:
            return no_update, no_update, "Select at least one group.", True

        targets = {td._norm(g) for g in group_values}

        # Build rows from the SAME data structs used by training_dashboard
        rows = []
        for cid, cust in td.CUSTOMERS.items():
            cust_groups = set(td.CID_TO_GROUPS.get(cid, []))
            if not (targets & cust_groups):
                continue

            name = f"{cust.get('first_name','')} {cust.get('last_name','')}".strip()
            groups_html = " ".join(pill(g.title(), PILL_BG) for g in sorted(cust_groups)) if cust_groups else "—"

            # Current status (forward fill, reusing td logic/structs)
            appts = td.CID_TO_APPTS.get(cid, [])
            status_rows = []
            for ap in appts:
                aid = ap.get("id")
                date_str = td.tidy_date_str(ap.get("date"))
                dt = pd.to_datetime(date_str, errors="coerce")
                if pd.isna(dt):
                    continue
                eids = td.encounter_ids_for_appt(aid)
                max_eid = max(eids) if eids else None
                s = td.extract_training_status(td.fetch_encounter(max_eid)) if max_eid else ""
                if s:
                    status_rows.append((dt.normalize(), s))

            current_status = ""
            if status_rows:
                df_s = pd.DataFrame(status_rows, columns=["Date", "Status"]).sort_values("Date")
                df_s = df_s.drop_duplicates("Date", keep="last")
                full_idx = pd.date_range(start=df_s["Date"].min(),
                                         end=pd.Timestamp("today").normalize(), freq="D")
                df_full = pd.DataFrame({"Date": full_idx}).merge(df_s, on="Date", how="left").sort_values("Date")
                df_full["Status"] = df_full["Status"].ffill()
                current_status = str(df_full.iloc[-1]["Status"]) if not df_full.empty else ""

            status_color = td.PASTEL_COLOR.get(current_status, "#e6e6e6")
            status_html = f"{dot(status_color)}{current_status or '—'}" if current_status else "—"

            # Complaints (customer-level merge)
            complaints = td.fetch_customer_complaints(cid)
            complaint_names = [c["Title"] for c in complaints if c.get("Title")]
            complaints_html = " ".join(pill(t, "#e7f0ff", border="#cfe0ff") for t in complaint_names) if complaint_names else "—"

            rows.append({
                "Athlete": name,
                "Groups": groups_html,
                "Current Status": status_html,
                "Complaints": complaints_html,
                "DOB": cust.get("dob") or cust.get("birthdate") or "—",
                "Sex": cust.get("sex") or cust.get("gender") or "—",
                "_cid": cid,  # hidden id
            })

        if not rows:
            return html.Div("No athletes in those groups."), [], "", False

        df = pd.DataFrame(rows)

        table = dash_table.DataTable(
            id="t1-athlete-table",
            data=df.to_dict("records"),
            columns=[
                {"name":"Athlete", "id":"Athlete"},
                {"name":"Groups", "id":"Groups", "presentation":"markdown"},
                {"name":"Current Status", "id":"Current Status", "presentation":"markdown"},
                {"name":"Complaints", "id":"Complaints", "presentation":"markdown"},
                {"name":"DOB", "id":"DOB"},
                {"name":"Sex", "id":"Sex"},
            ],
            # Enable built-in filtering/sorting
            filter_action="native",
            sort_action="native",
            page_size=15,
            style_table={"overflowX":"auto"},
            style_header={"fontWeight":"600","backgroundColor":"#f8f9fa"},
            style_cell={"padding":"9px","fontSize":14,
                        "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                        "textAlign":"left"},
            style_data={"borderBottom":"1px solid #eceff4"},
            style_data_conditional=[{"if": {"row_index":"odd"}, "backgroundColor":"#fbfbfd"}],
            # CRITICAL: render our <span> HTML
            dangerously_allow_html=True,
            # selection
            row_selectable="single",
            selected_rows=[],
        )

        return table, df.to_dict("records"), "", False

    except Exception as e:
        print("t1_load_customers error:", e)
        traceback.print_exc()
        return no_update, no_update, "Error loading athletes.", True


# Capture selected athlete (from table selection)
@app.callback(
    Output("t1-selected-athlete", "data"),
    Input("t1-athlete-table", "derived_virtual_selected_rows"),
    State("t1-rows-json", "data"),
    prevent_initial_call=True,
)
def t1_select_athlete(selected_rows, all_rows):
    if not selected_rows:
        return no_update
    idx = selected_rows[0]
    try:
        cid = all_rows[idx]["_cid"]
        return int(cid)
    except Exception:
        return no_update


# When athlete changes → populate complaints dropdown + hint + user field + today date
@app.callback(
    Output("t1-complaint-dd", "options"),
    Output("t1-complaint-dd", "value"),
    Output("t1-comment-hint", "children"),
    Output("t1-comment-user", "value"),
    Output("t1-comment-date", "date"),
    Input("t1-selected-athlete", "data"),
    Input("user-refresh", "n_intervals"),  # also refresh who is signed in text
    prevent_initial_call=True,
)
def t1_update_comment_controls(cid, _n_user):
    if not cid:
        return [], None, "Select an athlete in the table above.", "", no_update

    # Complaints for this athlete
    complaints = td.fetch_customer_complaints(int(cid))
    opts = [{"label": c["Title"], "value": c["Title"]} for c in complaints if c.get("Title")]
    value = opts[0]["value"] if opts else None

    # Who am I?
    try:
        token = auth.get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = requests.get(f"{SITE_URL}/api/csiauth/me/", headers=headers, timeout=6)
        if resp.ok:
            me = resp.json()
            user_label = f"{me.get('first_name','')} {me.get('last_name','')}".strip()
        else:
            user_label = ""
    except Exception:
        user_label = ""

    today = pd.Timestamp("today").strftime("%Y-%m-%d")
    return opts, value, f"Adding comment for athlete ID {cid}.", user_label, today


# Save comment → DB + refresh history
@app.callback(
    Output("t1-comments-table", "data"),
    State("t1-selected-athlete", "data"),
    State("t1-rows-json", "data"),
    State("t1-comment-date", "date"),
    State("t1-comment-text", "value"),
    State("t1-complaint-dd", "value"),
    State("t1-comment-user", "value"),
    Input("t1-save-comment", "n_clicks"),
    prevent_initial_call=True,
)
def t1_save_comment(cid, all_rows, date_str, text, complaint, user_label, _n):
    if not cid or not date_str or not (text or "").strip():
        # nothing to do
        if cid:
            return td.db_list_comments([int(cid)])
        return []

    # build a richer comment string with complaint & author
    text_clean = text.strip()
    parts = []
    if complaint:
        parts.append(f"[Complaint: {complaint}]")
    if user_label:
        parts.append(f"[By: {user_label}]")
    parts.append(text_clean)
    final_comment = " ".join(parts)

    # Find label for the athlete
    label = None
    for r in (all_rows or []):
        if int(r.get("_cid")) == int(cid):
            label = r.get("Athlete")
            break
    if not label:
        # fallback from training dashboard customer dictionary
        c = td.CUSTOMERS.get(int(cid), {})
        label = f"{c.get('first_name','')} {c.get('last_name','')} (ID {cid})".strip()

    td.db_add_comment(int(cid), label, date_str, final_comment)
    return td.db_list_comments([int(cid)])


# When athlete changes → refresh the history table
@app.callback(
    Output("t1-comments-table", "data", allow_duplicate=True),
    Input("t1-selected-athlete", "data"),
    prevent_initial_call=True
)
def t1_refresh_history_on_select(cid):
    if not cid:
        return []
    return td.db_list_comments([int(cid)])


# ======================================================================================
# Register Tab 2 callbacks from training_dashboard
# ======================================================================================
td.register_callbacks(app)


# ======================================================================================
# Run
# ======================================================================================
if __name__ == "__main__":
    app.run(debug=False, port=8050)
