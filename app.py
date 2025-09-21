# app.py
import os, math, requests, json
import pandas as pd

import dash
import flask
from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update, dash_table, State, callback_context
import dash_bootstrap_components as dbc

from layout import Footer, Navbar  # same as repo
from settings import *             # expects AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Reuse the data + helpers from your training dashboard module
from training_dashboard import (
    layout_body as training_layout_body,
    register_callbacks as training_register_callbacks,
    CUSTOMERS, CID_TO_GROUPS, CID_TO_APPTS, GROUP_OPTS,
    fetch_customer_complaints, PASTEL_COLOR, STATUS_ORDER,
    db_add_comment, db_list_comments
)

# ------------------------- Auth / server (repo style) -------------------------
auth = DashAuthExternal(
    AUTH_URL, TOKEN_URL, app_url=APP_URL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET
)
server = auth.server

here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"

app = Dash(
    __name__,
    server=server,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css",
    ],
    suppress_callback_exceptions=True,
)

# ------------------------- Small HTML helpers (pills/dots) -------------------------
def pill(text: str, bg_hex: str, border="#e3e6eb", fg="#111"):
    return f"""
    <span style="
      display:inline-block;padding:2px 8px;border-radius:999px;
      background:{bg_hex};color:{fg};border:1px solid {border};
      font-size:12px; line-height:18px; white-space:nowrap;">
      {text}
    </span>
    """

def dot(hex_color: str, size=10, mr=8):
    return f'<span style="display:inline-block;width:{size}px;height:{size}px;border-radius:50%;background:{hex_color};margin-right:{mr}px;border:1px solid rgba(0,0,0,.25)"></span>'

# ------------------------- Tab 1 layout -------------------------
def tab1_layout():
    return dbc.Container([
        html.H3("Athlete Overview", className="mt-1"),

        # Group selector + Load
        dbc.Row([
            dbc.Col(dcc.Dropdown(id="t1-group-dd", options=GROUP_OPTS, multi=True,
                                 placeholder="Select patient group(s)…"), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
        ], className="g-2"),
        html.Hr(),

        # Athlete table card
        dbc.Card([
            dbc.CardHeader("Athletes", className="bg-light"),
            dbc.CardBody([
                dash_table.DataTable(
                    id="t1-athlete-table",
                    columns=[
                        {"name":"Athlete","id":"Athlete","presentation":"markdown"},
                        {"name":"Groups","id":"Groups","presentation":"markdown"},
                        {"name":"Current Status","id":"CurrentStatus","presentation":"markdown"},
                        {"name":"Complaints","id":"Complaints","presentation":"markdown"},
                        {"name":"Latest Onset","id":"LatestOnset"},
                        {"name":"Priority","id":"Priority"},
                        {"name":"Complaint Status","id":"ComplaintStatus"},
                    ],
                    data=[],
                    page_size=15,
                    filter_action="native",
                    sort_action="native",
                    markdown_options={"html": True},
                    style_table={"overflowX":"auto"},
                    style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","borderBottom":"1px solid #e9ecef"},
                    style_cell={
                        "padding":"9px","fontSize":14,
                        "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                        "textAlign":"left"
                    },
                    style_data={"borderBottom":"1px solid #eceff4"},
                    style_data_conditional=[{"if":{"row_index":"odd"},"backgroundColor":"#fbfbfd"}],
                    row_selectable="single",
                    selected_rows=[],
                )
            ])
        ], className="mb-4"),

        # Compose comment card
        dbc.Card([
            dbc.CardHeader("Add Comment", className="bg-light"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(dcc.DatePickerSingle(id="t1-comment-date", display_format="YYYY-MM-DD"), md=3),
                    dbc.Col(dcc.Dropdown(id="t1-complaint-dd", placeholder="Pick complaint (optional)…"), md=4),
                    dbc.Col(dcc.Textarea(
                        id="t1-comment-text",
                        placeholder="Write a comment about the selected athlete (auto-fills today if empty)…",
                        style={"width":"100%","height":"80px"}), md=5),
                ], className="g-2"),
                html.Div(id="t1-comment-hint", className="text-muted mt-1", style={"fontSize":"12px"}),
                dbc.Button("Save Comment", id="t1-save-comment", color="success", className="mt-2"),
            ])
        ], className="mb-4"),

        # Comment history
        dbc.Card([
            dbc.CardHeader("Comment History (selected athlete)", className="bg-light"),
            dbc.CardBody([
                dash_table.DataTable(
                    id="t1-comments-table",
                    columns=[
                        {"name":"Date","id":"Date"},
                        {"name":"By","id":"By"},
                        {"name":"Athlete","id":"Athlete"},
                        {"name":"Complaint","id":"Complaint"},
                        {"name":"Status","id":"Status","presentation":"markdown"},
                        {"name":"Comment","id":"Comment"},
                    ],
                    data=[],
                    page_size=10,
                    filter_action="native",
                    sort_action="native",
                    markdown_options={"html": True},
                    style_header={"fontWeight":"600","backgroundColor":"#f8f9fa"},
                    style_cell={"padding":"8px","fontSize":13,
                                "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                                "textAlign":"left"},
                    style_data={"whiteSpace":"normal","height":"auto","borderBottom":"1px solid #eee"},
                )
            ])
        ], className="mb-5"),

        # selected athlete store
        dcc.Store(id="t1-selected-athlete", data=None),

        dbc.Alert(id="t1-msg", is_open=False, duration=0, color="danger"),
    ], fluid=True)

# ------------------------- App layout (repo style) -------------------------
app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    Navbar([ html.Span(id="navbar-user", className="text-white-50 small", children="") ]).render(),

    dbc.Container([
        dcc.Tabs(id="main-tabs", value="tab1", children=[
            dcc.Tab(label="Athletes", value="tab1", children=[ tab1_layout() ]),
            dcc.Tab(label="Training Dashboard", value="tab2", children=[ training_layout_body() ]),
        ]),
    ], fluid=True),

    Footer().render(),
])

# ------------------------- Repo-like “Who am I?” badge -------------------------
API_ME_URL = f"{SITE_URL}/api/csiauth/me/"

@app.callback(
    Output("redirect-to", "href"),
    Output("navbar-user", "children"),
    Input("init-interval", "n_intervals"),
    Input("user-refresh", "n_intervals"),
)
def who_am_i(_n_init, _n_ref):
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if not token:
        # On first load with no token, kick to auth (relative path works on Connect)
        if _n_init == 0:
            return "login", no_update
        return no_update, html.A("Sign in", href="login", className="text-white")

    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(API_ME_URL, headers=headers, timeout=5)
        r.raise_for_status()
        me = r.json() or {}
        first = (me.get("first_name") or "").strip()
        last  = (me.get("last_name") or "").strip()
        name  = f"{first} {last}".strip() or me.get("email","")
        return no_update, f"Signed in as: {name}"
    except Exception:
        # Token might be stale; show generic label but don’t break app
        return no_update, html.A("Sign in", href="login", className="text-white")

# ------------------------- Tab 1 callbacks -------------------------
@dash.callback(
    Output("t1-athlete-table", "data"),
    Output("t1-athlete-table", "selected_rows"),
    Output("t1-msg", "children"),
    Output("t1-msg", "is_open"),
    Input("t1-load", "n_clicks"),
    State("t1-group-dd", "value"),
    prevent_initial_call=True,
)
def t1_load_group(n_clicks, groups):
    if not groups:
        return [], [], "Select at least one group.", True

    targets = { (g or "").strip().lower() for g in groups }
    rows = []

    # Build per-athlete summaries similar to Tab 2 logic (but fast & lightweight)
    for cid, cust in CUSTOMERS.items():
        cust_groups = set(CID_TO_GROUPS.get(cid, []))
        if not (targets & cust_groups):
            continue

        label = f"{cust.get('first_name','')} {cust.get('last_name','')} (ID {cid})".strip()

        # Current training status (forward-filled lightweight pass)
        appts = CID_TO_APPTS.get(cid, [])
        status_rows = []
        for ap in appts:
            dt_str = ap.get("date")
            if isinstance(dt_str, dict): dt_str = dt_str.get("start","")
            dt = pd.to_datetime((dt_str or "").split("T",1)[0], errors="coerce")
            if pd.isna(dt): continue
            # NOTE: we avoid calling encounters on all rows here for speed; last-known
            # status is approximated by presence, or leave blank. You can uncomment
            # the detailed status computation if needed.
            # eids = encounter_ids_for_appt(ap.get("id"))
            # if eids: s = extract_training_status(fetch_encounter(max(eids)))
            # else: s = ""
            s = ""  # keep fast; training tab computes exact value
            if s: status_rows.append((dt.normalize(), s))

        current_status = ""
        if status_rows:
            df_s = pd.DataFrame(status_rows, columns=["Date","Status"]).sort_values("Date").drop_duplicates("Date", keep="last")
            idx = pd.date_range(start=df_s["Date"].min(), end=pd.Timestamp("today").normalize(), freq="D")
            df_full = pd.DataFrame({"Date": idx}).merge(df_s, on="Date", how="left").sort_values("Date")
            df_full["Status"] = df_full["Status"].ffill()
            current_status = str(df_full.iloc[-1]["Status"]) if not df_full.empty else ""

        # complaints (customer-level merge)
        comps = fetch_customer_complaints(cid)
        comp_titles = [c["Title"] for c in comps if c.get("Title")]
        comp_count = len(comp_titles)
        latest_onset = ""
        priority = ""
        comp_status = ""
        if comps:
            try:
                latest = sorted([c for c in comps if c.get("Onset")], key=lambda x: pd.to_datetime(x["Onset"]), reverse=True)[0]
            except Exception:
                latest = comps[0]
            latest_onset = latest.get("Onset","") or ""
            priority = latest.get("Priority","") or ""
            comp_status = latest.get("Status","") or ""

        # pills/dots
        group_pills = " ".join([pill(g.title(), "#eef2f7") for g in sorted(list(cust_groups))]) or "—"
        status_chip = (dot(PASTEL_COLOR.get(current_status, "#e6e6e6")) + (current_status or "—"))
        complaint_pill = pill(f"{comp_count} issues", "#ffe8e0") if comp_count else "—"

        rows.append({
            "Athlete": label,
            "Groups": group_pills,
            "CurrentStatus": status_chip,
            "Complaints": complaint_pill,
            "LatestOnset": latest_onset,
            "Priority": priority or "—",
            "ComplaintStatus": comp_status or "—",
            "_cid": cid,  # keep id hidden for selection
            "_complaint_titles": comp_titles,
        })

    # Sort by athlete name
    rows.sort(key=lambda r: r["Athlete"].lower())
    return rows, [], "", False

@dash.callback(
    Output("t1-selected-athlete", "data"),
    Output("t1-complaint-dd", "options"),
    Output("t1-comment-hint", "children"),
    Input("t1-athlete-table", "selected_rows"),
    State("t1-athlete-table", "data"),
)
def t1_select_athlete(selected_rows, table_data):
    if not table_data or not selected_rows:
        return None, [], "Select an athlete in the table above to add a comment."
    idx = selected_rows[0]
    rec = table_data[idx]
    cid = rec.get("_cid")
    label = rec.get("Athlete","")
    # complaint list for dropdown (optional)
    comp_titles = rec.get("_complaint_titles") or []
    opts = [{"label": t, "value": t} for t in sorted(set(comp_titles))]
    hint = f"Commenting on: {label}"
    return {"id": cid, "label": label}, opts, hint

@dash.callback(
    Output("t1-comments-table", "data"),
    Input("t1-selected-athlete", "data"),
)
def t1_refresh_comments(sel):
    if not sel or not sel.get("id"):
        return []
    cid = int(sel["id"])
    # map to columns needed on Tab 1
    rows = db_list_comments([cid])
    # Add simple “By” (we don’t store user in DB, so display placeholder or use auth info if desired)
    out = [{
        "Date": r["Date"],
        "By": "",  # if you want, fetch /api/csiauth/me/ in save step and persist
        "Athlete": r["Athlete"],
        "Complaint": "",   # stored free-form below if you extend DB schema
        "Status": "",      # likewise (or compute current status on-the-fly)
        "Comment": r["Comment"]
    } for r in rows]
    return out

@dash.callback(
    Output("t1-comments-table", "data", allow_duplicate=True),
    State("t1-selected-athlete", "data"),
    State("t1-comment-date", "date"),
    State("t1-comment-text", "value"),
    State("t1-complaint-dd", "value"),
    Input("t1-save-comment", "n_clicks"),
    prevent_initial_call=True,
)
def t1_save_comment(sel, date_str, text, complaint_value, _n):
    if not sel or not sel.get("id"): return no_update
    if not (text or "").strip(): return no_update
    if not date_str:
        date_str = pd.Timestamp("today").strftime("%Y-%m-%d")

    cid   = int(sel["id"])
    label = sel.get("label", f"ID {cid}")
    # Persist comment; if you want to store complaint/status/author, extend the table schema here
    db_add_comment(cid, label, date_str, text.strip())

    # Refresh table
    rows = db_list_comments([cid])
    out = [{
        "Date": r["Date"], "By": "", "Athlete": r["Athlete"],
        "Complaint": complaint_value or "", "Status": "",
        "Comment": r["Comment"]
    } for r in rows]
    return out

# ------------------------- Training tab callbacks -------------------------
training_register_callbacks(app)

# ------------------------- Run -------------------------
if __name__ == "__main__":
    app.run(debug=False, port=8050)
