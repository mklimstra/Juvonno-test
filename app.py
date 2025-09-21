# app.py
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

# Optional: who-is-logged-in badge
import json

# Layout pieces from your repo
from layout import Footer, Navbar
from settings import *  # expects AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Bring in the full Training Dashboard module
# (Use its _get + _require_api_key + encounter helpers to stay consistent)
from training_dashboard import (
    layout_body as training_layout_body,
    register_callbacks as training_register_callbacks,
    _get, _require_api_key,
    extract_training_status, encounter_ids_for_appt, fetch_encounter
)

# ------------------------- DashAuthExternal / Flask server -------------------------
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Session config for hosted redirects
server.secret_key = os.getenv("SECRET_KEY", "dev-change-me")
server.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True
)

# Serve /assets like the repo
here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"


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

# ------------------------- Helpers for Tab 1 (fresh calls, same pattern as dashboard) -------------------------
def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _groups_of_customer(cust: dict):
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

def t1_fetch_customers_full():
    out, page = [], 1
    while True:
        js = _get("customers/list", include="groups", page=page, count=100, status="ACTIVE")
        rows = js.get("list", js)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    return {c["id"]: c for c in out if c.get("id")}

def t1_group_options():
    people = t1_fetch_customers_full()
    all_groups = sorted({g for cid, c in people.items() for g in _groups_of_customer(c)})
    return [{"label": g.title(), "value": g} for g in all_groups]

def t1_fetch_branch_appts(branch=1):
    rows, page = [], 1
    while True:
        js = _get(
            f"appointments/list/{branch}",
            start_date="2000-01-01",
            status="all",
            page=page,
            count=100
        )
        block = js.get("list", js)
        if not block:
            break
        rows.extend(block)
        if len(block) < 100:
            break
        page += 1
    return rows

def _tidy_date_str(raw) -> str:
    if isinstance(raw, dict):
        raw = raw.get("start", "")
    raw = raw or ""
    return raw.split("T", 1)[0] if isinstance(raw, str) else str(raw)

def _last_appt_date(appts):
    if not appts:
        return ""
    ds = []
    for ap in appts:
        try:
            ds.append(pd.to_datetime(_tidy_date_str(ap.get("date")), errors="coerce"))
        except Exception:
            pass
    ds = [d for d in ds if pd.notna(d)]
    return (max(ds).strftime("%Y-%m-%d") if ds else "")

def _latest_status_fast(appts):
    """
    Fast “latest status”: check the appointment with max date only.
    (Full forward-fill view remains on the Training Dashboard tab.)
    """
    if not appts:
        return ""
    # pick the appointment with latest date
    appts_sorted = sorted(
        appts,
        key=lambda ap: pd.to_datetime(_tidy_date_str(ap.get("date")), errors="coerce"),
        reverse=True
    )
    top = appts_sorted[0]
    aid = top.get("id")
    if not aid:
        return ""
    try:
        eids = encounter_ids_for_appt(aid)
        if not eids:
            return ""
        max_eid = max(eids)
        status = extract_training_status(fetch_encounter(max_eid))
        return status or ""
    except Exception:
        return ""


# ------------------------- Tabs -------------------------
def render_tab1():
    return dbc.Container([
        # Controls
        dbc.Row([
            dbc.Col(dcc.Dropdown(
                id="t1-group-dd",
                options=[],              # populated dynamically
                multi=True,
                placeholder="Select patient group(s)…",
            ), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
        ], className="g-2 mb-2"),

        dbc.Alert(id="t1-msg", is_open=False, duration=0, color="danger"),

        # Table container + a store of raw rows for selection use
        html.Div(id="t1-grid-container"),
        dcc.Store(id="t1-rows-json", data=[]),

        html.Div(className="mt-2 text-muted small",
                 children="Tip: Click a row to jump that athlete into the Training Dashboard tab."),
    ], fluid=True)


def render_tabs():
    return dbc.Container([
        dcc.Tabs(
            id="tabs",
            value="tab1",
            children=[
                dcc.Tab(label="Athletes", value="tab1"),
                dcc.Tab(label="Training Dashboard", value="tab2"),
            ]
        ),
        html.Div(id="tab-content", className="mt-3")
    ], fluid=True)


# ------------------------- Layout -------------------------
app.layout = html.Div([
    # Mirrors repo boot sequence
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    # Optional: refresh the user badge every 60s
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    # Navbar with a right-side slot for the signed-in user label
    Navbar([
        html.Span(id="navbar-user", className="text-white-50 small", children="")
    ]).render(),

    # Tabs
    render_tabs(),

    Footer().render(),
])


# ------------------------- Tab content router -------------------------
@app.callback(
    Output("tab-content", "children"),
    Input("tabs", "value"),
)
def render_tab(which):
    if which == "tab1":
        return render_tab1()
    else:
        # training_layout_body() comes from training_dashboard.py
        return training_layout_body()


# ------------------------- Init: auth + group options + user badge -------------------------
@app.callback(
    Output("redirect-to", "href"),
    Output("t1-group-dd", "options"),
    Input("init-interval", "n_intervals"),
    prevent_initial_call=True
)
def initial_view(_n):
    """
    On first tick:
      - verify we have a token (if your app expects the SSO workflow)
      - populate Tab 1's group dropdown from Juvonno
    """
    # If you require auth token for other protected endpoints, try to fetch it:
    try:
        _ = auth.get_token()
    except Exception:
        # If you don't require auth here, ignore; we still can call Juvonno with API key
        pass

    # Juvonno needs the API key; make sure it's there
    try:
        _require_api_key()
    except Exception as e:
        # Stay on page; show no redirect, and leave dropdown empty
        print("Missing JUV_API_KEY:", e)
        return no_update, []

    # Populate groups for Tab 1
    try:
        group_opts = t1_group_options()
    except Exception as e:
        print("Failed to fetch groups:", e)
        group_opts = []

    return no_update, group_opts


# Optional: “who am I?” badge (top right)
@app.callback(
    Output("navbar-user", "children"),
    Input("user-refresh", "n_intervals"),
    prevent_initial_call=True
)
def refresh_user_badge(_n):
    token = None
    try:
        token = auth.get_token()
    except Exception:
        pass
    if not token:
        return ""  # unauthenticated flow; hide text

    try:
        headers = {"Authorization": f"Bearer {token}"}
        me = requests.get(f"{SITE_URL}/api/csiauth/me/", headers=headers, timeout=5)
        me.raise_for_status()
        data = me.json()
        first = data.get("first_name") or ""
        last  = data.get("last_name") or ""
        email = data.get("email") or ""
        label = (f"{first} {last}".strip() or email or "Signed in").strip()
        return f"Signed in as: {label}"
    except Exception:
        return ""


# ------------------------- Tab 1 callbacks -------------------------
@app.callback(
    Output("t1-group-dd", "options"),
    Input("tabs", "value"),
    prevent_initial_call=True
)
def t1_refresh_groups_on_tab_switch(tab_value):
    if tab_value != "tab1":
        raise PreventUpdate
    try:
        _require_api_key()
        return t1_group_options()
    except Exception:
        raise PreventUpdate


@app.callback(
    Output("t1-grid-container", "children"),
    Output("t1-rows-json", "data"),
    Output("t1-msg", "children"),
    Output("t1-msg", "is_open"),
    Input("t1-load", "n_clicks"),
    State("t1-group-dd", "value"),
    prevent_initial_call=True
)
def t1_fetch(n_clicks, group_values):
    try:
        _require_api_key()
        if not group_values:
            return no_update, no_update, "Select at least one group.", True

        customers_by_id = t1_fetch_customers_full()
        targets = {_norm(g) for g in (group_values or [])}
        filtered_cids = [
            cid for cid, c in customers_by_id.items()
            if targets & set(_groups_of_customer(c))
        ]

        if not filtered_cids:
            return html.Div("No athletes found for those groups."), [], "", False

        appts = t1_fetch_branch_appts(1)
        cid_to_appts = {}
        for ap in appts:
            cust = ap.get("customer", {})
            if isinstance(cust, dict) and cust.get("id"):
                cid_to_appts.setdefault(int(cust["id"]), []).append(ap)

        rows = []
        for cid in filtered_cids:
            cust = customers_by_id.get(cid, {}) or {}
            first = cust.get("first_name", "") or ""
            last  = cust.get("last_name", "") or ""
            athlete_label = f"{first} {last}".strip() or f"ID {cid}"
            grp_display = ", ".join(g.title() for g in _groups_of_customer(cust))
            my_appts    = cid_to_appts.get(int(cid), [])

            current_status = _latest_status_fast(my_appts) or "—"
            last_appt     = _last_appt_date(my_appts)

            rows.append({
                "Athlete": athlete_label,
                "Groups": grp_display,
                "Current Status": current_status,
                "Last Appt": last_appt,
                "_cid": int(cid),
            })

        rows.sort(key=lambda r: r["Athlete"].lower())
        # Build a simple DataTable for speed/reliability
        table = dash_table.DataTable(
            id="t1-table",
            data=rows,
            columns=[
                {"name":"Athlete","id":"Athlete"},
                {"name":"Groups","id":"Groups"},
                {"name":"Current Status","id":"Current Status"},
                {"name":"Last Appt","id":"Last Appt"},
            ],
            sort_action="native",
            filter_action="native",
            page_size=20,
            row_selectable="single",
            style_table={"overflowX":"auto"},
            style_header={"fontWeight":"600","backgroundColor":"#f8f9fa"},
            style_cell={
                "padding":"8px",
                "fontSize":14,
                "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                "textAlign":"left"
            },
            style_data={"borderBottom":"1px solid #eceff4"},
            style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfbfd"}],
        )

        return table, rows, "", False

    except Exception as e:
        return no_update, no_update, f"Error: {e}", True


# When a row is selected on Tab 1 → set Tab 2's athlete dropdown (cust-select)
@app.callback(
    Output("cust-select", "value"),
    Input("t1-table", "selected_rows"),
    State("t1-rows-json", "data"),
    prevent_initial_call=True
)
def t1_select_row_to_dashboard(selected_rows, rows_json):
    if not selected_rows:
        raise PreventUpdate
    idx = selected_rows[0]
    try:
        cid = rows_json[idx]["_cid"]
    except Exception:
        raise PreventUpdate
    return int(cid)


# ------------------------- Plug in the Training Dashboard callbacks (Tab 2) -------------------------
training_register_callbacks(app)


# ------------------------- Run -------------------------
if __name__ == "__main__":
    # Locally you can run on 8050; on Connect, the launcher takes over.
    app.run(debug=False, port=8050)
