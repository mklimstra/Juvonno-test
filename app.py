# app.py
import os
import json
import math
import requests
import pandas as pd
from datetime import date, datetime

import dash
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

from dash import Dash, Input, Output, State, html, dcc, no_update
from dash.exceptions import PreventUpdate

# Auth + layout modules (same as the repo)
from dash_auth_external import DashAuthExternal
from layout import Footer, Navbar, Pagination, GeographyFilters
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Reuse all the data plumbing from your working dashboard
# (we deliberately import functions & constants so Tab 1 fetches the same way)
from training_dashboard import (
    _require_api_key, _get, tidy_date_str,
    fetch_customers_full, fetch_customer_complaints,
    extract_training_status, encounter_ids_for_appt, fetch_encounter,
    PASTEL_COLOR, STATUS_ORDER, GROUP_OPTS, _norm
)

# ──────────────────────────────────────────────────────────────────────
# Auth / Flask server (unchanged from your working version)
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server
server.secret_key = os.getenv("SECRET_KEY", "dev-change-me")

here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"

# ──────────────────────────────────────────────────────────────────────
# Helpers (Tab 1)
def _json_safe(obj):
    """Coerce numpy, Timestamp, dates, and sets so dcc.Store can serialize clean JSON."""
    import numpy as np
    if isinstance(obj, (np.integer, )):
        return int(obj)
    if isinstance(obj, (np.floating,  )):
        return float(obj)
    if isinstance(obj, (np.bool_,     )):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    if isinstance(obj, set):
        return list(obj)
    return str(obj) if obj is not None else ""

def _status_dot_html(text: str) -> str:
    color = PASTEL_COLOR.get(text, "#e6e6e6")
    return (
        f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
        f'background:{color};border:1px solid rgba(0,0,0,.25);margin-right:6px"></span>'
        f'{text}'
    )

def _pill_html(txt: str, bg: str = "#f1f3f5", color: str = "#111"):
    return (f'<span style="display:inline-block;padding:3px 8px;border-radius:9999px;'
            f'font-size:12px;background:{bg};color:{color};border:1px solid #e3e6eb;'
            f'margin:2px 4px 2px 0">{txt}</span>')

def _fmt_date(val) -> str:
    try:
        return pd.to_datetime(val).strftime("%Y-%m-%d")
    except Exception:
        return str(val) if val else ""

def _current_training_status_for_customer(aid_list):
    """Forward-fill latest training status from this customer's appointments."""
    status_rows = []
    for ap in aid_list:
        aid = ap.get("id")
        dt  = pd.to_datetime(tidy_date_str(ap.get("date")), errors="coerce")
        if pd.isna(dt):
            continue
        eids = encounter_ids_for_appt(aid)
        max_eid = max(eids) if eids else None
        s = extract_training_status(fetch_encounter(max_eid)) if max_eid else ""
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

def _last_appt_date(aid_list):
    dates = []
    for ap in aid_list:
        ds = tidy_date_str(ap.get("date"))
        try:
            dates.append(pd.to_datetime(ds))
        except Exception:
            pass
    return _fmt_date(max(dates)) if dates else ""

def t1_build_grid(rows):
    """
    AG Grid with pill renderers.
    """
    # JS renderers (allow_code=True below)
    pill_renderer = dag.JsCode("""
    function(params) {
        if (!params.value) return "";
        const items = Array.isArray(params.value) ? params.value : String(params.value).split(";").map(s=>s.trim()).filter(Boolean);
        return items.map(txt => {
            return `<span class="ag-theme-quartz" style="display:inline-block;padding:3px 8px;border-radius:9999px;font-size:12px;background:#f1f3f5;color:#111;border:1px solid #e3e6eb;margin:2px 4px 2px 0">${txt}</span>`;
        }).join(" ");
    }
    """)
    status_renderer = dag.JsCode("""
    function(params){
        if (!params.value) return "";
        // value is already HTML string with a dot + text
        return params.value;
    }
    """)

    col_defs = [
        {"headerName": "Athlete", "field": "Athlete", "flex": 2, "filter": True, "sortable": True},
        {"headerName": "Groups", "field": "Groups", "flex": 2, "cellRenderer": pill_renderer},
        {"headerName": "Current Status", "field": "Current Status",
         "cellRenderer": status_renderer, "flex": 2, "filter": True, "sortable": True},
        {"headerName": "Complaints", "field": "Complaints", "flex": 3, "cellRenderer": pill_renderer},
        {"headerName": "Last Appt", "field": "Last Appt", "flex": 1, "filter": True, "sortable": True},
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
            # Fix deprecation by using object form
            "rowSelection": {"mode": "single"},
            "animateRows": True,
            "pagination": True,
            "paginationPageSize": 20,
            "paginationPageSizeSelector": [10, 20, 50, 100],
            "suppressRowClickSelection": False,
            "ensureDomOrder": True,
            "domLayout": "normal",
        },
        className="ag-theme-quartz",
        style={"height": "520px", "width": "100%"},
        dangerously_allow_code=True,  # REQUIRED for JsCode renderers
    )
    return grid

# ──────────────────────────────────────────────────────────────────────
# App + layout (same shell structure as your repo)
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css",
    ],
    suppress_callback_exceptions=True,
)

# Navbar user slot (same pattern you added recently)
navbar_right = html.Span(id="navbar-user", className="text-white-50 small", children="")

app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    # Poll for user label (optional; can be longer)
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    Navbar([navbar_right]).render(),

    dbc.Container([
        dcc.Tabs(id="tabs", value="tab1", children=[
            dcc.Tab(label="Athletes (by Group)", value="tab1"),
            dcc.Tab(label="Training Dashboard",  value="tab2"),
        ], className="mb-3"),

        html.Div(id="tab-content")
    ], fluid=True),

    Footer().render(),
])

# ──────────────────────────────────────────────────────────────────────
# Tab content builders
def render_tab1():
    return dbc.Container([
        dbc.Row([
            dbc.Col(dcc.Dropdown(id="t1-group-dd", options=GROUP_OPTS, multi=True,
                                 placeholder="Select patient group(s) …"), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
        ], className="g-2 mb-3"),

        dbc.Alert(id="t1-msg", is_open=False, color="danger", duration=0),

        html.Div(id="t1-grid-container"),
        dcc.Store(id="t1-rows-json")
    ], fluid=True)

def render_tab2():
    # Defer importing layout builder to avoid circulars
    from training_dashboard import layout_body as training_layout_body
    return training_layout_body()

@app.callback(Output("tab-content", "children"), Input("tabs", "value"))
def tabs_router(val):
    if val == "tab1":
        return render_tab1()
    elif val == "tab2":
        return render_tab2()
    return html.Div()

# ──────────────────────────────────────────────────────────────────────
# Init/login & user label (same as earlier)
@app.callback(
    Output("redirect-to", "href"),
    Output("navbar-user", "children"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    # Check OAuth token
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if not token:
        # kick to login if needed
        return "login", ""

    # Populate user label
    try:
        headers = {"Authorization": f"Bearer {token}"}
        # canonical me endpoint
        url = f"{SITE_URL.rstrip('/')}/api/csiauth/me/"
        resp = requests.get(url, headers=headers, timeout=6)
        resp.raise_for_status()
        me = resp.json() or {}
        first = me.get("first_name") or ""
        last  = me.get("last_name")  or ""
        email = me.get("email") or ""
        label = (first + " " + last).strip() or email
        return no_update, f"Signed in as: {label}"
    except Exception:
        return no_update, ""

# Optional refresh (if you want the label to update without reload)
@app.callback(Output("navbar-user", "children", allow_duplicate=True),
              Input("user-refresh", "n_intervals"), prevent_initial_call=True)
def refresh_user_label(_):
    try:
        token = auth.get_token()
        if not token:
            return no_update
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{SITE_URL.rstrip('/')}/api/csiauth/me/"
        resp = requests.get(url, headers=headers, timeout=6)
        resp.raise_for_status()
        me = resp.json() or {}
        first = me.get("first_name") or ""
        last  = me.get("last_name")  or ""
        email = me.get("email") or ""
        label = (first + " " + last).strip() or email
        return f"Signed in as: {label}"
    except Exception:
        return no_update

# ──────────────────────────────────────────────────────────────────────
# Tab 1: fetch & render grid (independent of Tab 2)
@app.callback(
    Output("t1-grid-container", "children"),
    Output("t1-rows-json", "data"),
    Output("t1-msg", "children"),
    Output("t1-msg", "is_open"),
    Input("t1-load", "n_clicks"),
    State("t1-group-dd", "value"),
    prevent_initial_call=True
)
def t1_fetch_and_render(n_clicks, group_values):
    try:
        _require_api_key()  # fail fast if missing
        if not group_values:
            return no_update, no_update, "Select at least one group.", True

        # 1) Fetch customers (same way as training_dashboard.py, but fresh call here)
        customers_by_id = fetch_customers_full()

        # Build groups map
        def groups_of(cust: dict):
            src = cust.get("groups") if "groups" in cust else cust.get("group")
            names = []
            if isinstance(src, list):
                for it in src:
                    if isinstance(it, str): names.append(_norm(it))
                    elif isinstance(it, dict) and it.get("name"): names.append(_norm(it["name"]))
            elif isinstance(src, dict) and src.get("name"):
                names.append(_norm(src["name"]))
            elif isinstance(src, str):
                names.append(_norm(src))
            return names

        cid_to_groups = {cid: groups_of(c) for cid, c in customers_by_id.items()}

        # Filter customers to selected groups
        targets = {_norm(g) for g in group_values}
        filtered_cids = [
            cid for cid, gs in cid_to_groups.items()
            if targets & set(gs)
        ]

        if not filtered_cids:
            return html.Div("No athletes found for those groups."), [], "", False

        # 2) Build rows: need appointments, status, complaints
        #    For perf, we’ll fetch appointments once (branch=1) and map them per-customer.
        #    Using the same endpoint as your TD code.
        rows = []
        # Slurp branch-1 appts (paged)
        def fetch_branch_appts(branch=1):
            rows_, page = [], 1
            while True:
                js = _get(f"appointments/list/{branch}",
                          start_date="2000-01-01", status="all", page=page, count=100)
                block = js.get("list", js)
                if not block: break
                rows_.extend(block)
                if len(block) < 100: break
                page += 1
            return rows_

        appts = fetch_branch_appts(1)
        cid_to_appts = {}
        for ap in appts:
            cust = ap.get("customer", {})
            if isinstance(cust, dict) and cust.get("id"):
                cid_to_appts.setdefault(cust["id"], []).append(ap)

        for cid in filtered_cids:
            cust = customers_by_id.get(cid, {}) or {}
            first = cust.get("first_name", "") or ""
            last  = cust.get("last_name", "") or ""
            athlete_label = f"{first} {last}".strip() or f"ID {cid}"

            # groups (pretty case for pills)
            grp_display = [g.title() for g in cid_to_groups.get(cid, [])]

            # status & last appt from this customer's appointments
            my_appts = cid_to_appts.get(cid, [])
            current_status = _current_training_status_for_customer(my_appts)
            last_appt = _last_appt_date(my_appts)

            # complaints (from customer-level + appt-level merge; same helper as TD)
            complaints = [c["Title"] for c in fetch_customer_complaints(cid) if c.get("Title")]

            rows.append({
                "Athlete": athlete_label,
                "Groups": grp_display,                 # array → pill renderer
                "Current Status": _status_dot_html(current_status),
                "Complaints": complaints,              # array → pill renderer
                "Last Appt": last_appt,
            })

        # Sort by athlete for stability
        rows.sort(key=lambda r: r["Athlete"].lower())

        grid = t1_build_grid(rows)
        rows_json = json.loads(json.dumps(rows, default=_json_safe))
        return grid, rows_json, "", False

    except Exception as e:
        # Show message but don't crash the app
        return no_update, no_update, f"Error: {e}", True

# ──────────────────────────────────────────────────────────────────────
# Tab 2: wire up the existing training dashboard callbacks
def _register_training_callbacks(app: Dash):
    from training_dashboard import register_callbacks as td_register_callbacks
    td_register_callbacks(app)

_register_training_callbacks(app)

# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Locally run on 8050; on Connect, the launcher takes over.
    app.run(debug=False, port=8050)
