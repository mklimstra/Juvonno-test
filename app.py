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

# Auth + layout (same as repo)
from dash_auth_external import DashAuthExternal
from layout import Footer, Navbar, Pagination, GeographyFilters
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Reuse the working Juvonno plumbing from training_dashboard.py
from training_dashboard import (
    _require_api_key, _get, tidy_date_str,
    fetch_customers_full, fetch_customer_complaints,
    extract_training_status, encounter_ids_for_appt, fetch_encounter,
    PASTEL_COLOR, STATUS_ORDER, GROUP_OPTS, _norm
)

# ──────────────────────────────────────────────────────────────────────
# Auth / Flask server (unchanged)
auth = DashAuthExternal(
    AUTH_URL, TOKEN_URL, app_url=APP_URL,
    client_id=CLIENT_ID, client_secret=CLIENT_SECRET
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
    import numpy as np
    if isinstance(obj, (np.integer, )):
        return int(obj)
    if isinstance(obj, (np.floating, )):
        return float(obj)
    if isinstance(obj, (np.bool_, )):
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

def _fmt_date(val) -> str:
    try:
        return pd.to_datetime(val).strftime("%Y-%m-%d")
    except Exception:
        return str(val) if val else ""

def _last_appt_date(appt_list):
    dates = []
    for ap in appt_list:
        ds = tidy_date_str(ap.get("date"))
        try:
            dates.append(pd.to_datetime(ds))
        except Exception:
            pass
    return _fmt_date(max(dates)) if dates else ""

def _latest_status_fast(appt_list):
    """
    Fast path for Tab 1: use ONLY the latest appointment's encounter(s) to get a current status.
    This avoids iterating over all encounters for all historical appointments.
    """
    if not appt_list:
        return ""
    # find latest by date
    best = None
    best_dt = None
    for ap in appt_list:
        ds = tidy_date_str(ap.get("date"))
        dt = pd.to_datetime(ds, errors="coerce")
        if pd.isna(dt):
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best = ap
    if not best:
        return ""
    aid = best.get("id")
    eids = encounter_ids_for_appt(aid)
    if not eids:
        return ""
    max_eid = max(eids)
    try:
        return extract_training_status(fetch_encounter(max_eid)) or ""
    except Exception:
        return ""

def t1_build_grid(rows):
    """
    AG Grid with pill renderers — return DOM nodes with innerHTML (no JsCode).
    Works with latest dash-ag-grid / AG Grid 32.x.
    """
    pill_renderer = {
        "function": """
        function(params) {
            // Accept array or semicolon-delimited string
            const items = (Array.isArray(params.value)
                ? params.value
                : String(params.value || "")
                    .split(";")
                    .map(s => s.trim())
                    .filter(Boolean));

            const html = items.map(txt => (
              `<span style="display:inline-block;padding:3px 8px;border-radius:9999px;
                 font-size:12px;background:#f1f3f5;color:#111;border:1px solid #e3e6eb;
                 margin:2px 4px 2px 0">${txt}</span>`
            )).join(" ");

            const e = document.createElement("span");
            e.innerHTML = html;
            return e;
        }
        """
    };

    status_renderer = {
        "function": """
        function(params){
            const e = document.createElement("span");
            e.innerHTML = params.value || "";
            return e;
        }
        """
    };

    col_defs = [
        {"headerName": "Athlete", "field": "Athlete", "flex": 2, "filter": True, "sortable": True},
        {"headerName": "Groups", "field": "Groups", "flex": 2, "cellRenderer": pill_renderer},
        {"headerName": "Current Status", "field": "Current Status",
         "cellRenderer": status_renderer, "flex": 2, "filter": True, "sortable": True},
        {"headerName": "Complaints", "field": "Complaints", "flex": 3, "cellRenderer": pill_renderer},
        {"headerName": "Last Appt", "field": "Last Appt", "flex": 1, "filter": True, "sortable": True},
    ]

    return dag.AgGrid(
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
            "rowSelection": {"mode": "single"},   # new style
            "animateRows": True,
            "pagination": True,
            "paginationPageSize": 20,
            "paginationPageSizeSelector": [10, 20, 50, 100],  # include 10 to stop warning
            "suppressRowClickSelection": False,
            "ensureDomOrder": True,
            "domLayout": "normal",
        },
        className="ag-theme-quartz",
        style={"height": "520px", "width": "100%"},
        dangerously_allow_code=True,  # allow our JS renderers above
    )

# ──────────────────────────────────────────────────────────────────────
# App + layout
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css",
    ],
    suppress_callback_exceptions=True,
)

navbar_right = html.Span(id="navbar-user", className="text-white-50 small", children="")

app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),
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
# Init/login & navbar badge
@app.callback(
    Output("redirect-to", "href"),
    Output("navbar-user", "children"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    try:
        token = auth.get_token()
    except Exception:
        token = None
    if not token:
        return "login", ""
    try:
        headers = {"Authorization": f"Bearer {token}"}
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
# Tab 1: fetch & render grid
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
        _require_api_key()
        if not group_values:
            return no_update, no_update, "Select at least one group.", True

        # Fresh fetch of customers (same method as training_dashboard)
        customers_by_id = fetch_customers_full()

        # Build groups per-customer (copy of training_dashboard logic)
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
        targets = {_norm(g) for g in group_values}
        filtered_cids = [cid for cid, gs in cid_to_groups.items() if targets & set(gs)]

        if not filtered_cids:
            return html.Div("No athletes found for those groups."), [], "", False

        # Fetch appointments once (branch 1) and map to customers
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

        # Build rows (FAST current status)
        rows = []
        for cid in filtered_cids:
            cust = customers_by_id.get(cid, {}) or {}
            first = cust.get("first_name", "") or ""
            last  = cust.get("last_name", "") or ""
            athlete_label = f"{first} {last}".strip() or f"ID {cid}"

            grp_display = [g.title() for g in cid_to_groups.get(cid, [])]
            my_appts    = cid_to_appts.get(cid, [])
            current_status = _latest_status_fast(my_appts)
            last_appt     = _last_appt_date(my_appts)
            complaints    = [c["Title"] for c in fetch_customer_complaints(cid) if c.get("Title")]

            rows.append({
                "Athlete": athlete_label,
                "Groups": grp_display,
                "Current Status": _status_dot_html(current_status),
                "Complaints": complaints,
                "Last Appt": last_appt,
            })

        rows.sort(key=lambda r: r["Athlete"].lower())
        grid = t1_build_grid(rows)
        rows_json = json.loads(json.dumps(rows, default=_json_safe))
        return grid, rows_json, "", False

    except Exception as e:
        return no_update, no_update, f"Error: {e}", True

# ──────────────────────────────────────────────────────────────────────
# Tab 2: register existing training dashboard callbacks
def _register_training_callbacks(app: Dash):
    from training_dashboard import register_callbacks as td_register_callbacks
    td_register_callbacks(app)

_register_training_callbacks(app)

# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=False, port=8050)
