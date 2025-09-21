import os
import json
import math
import sqlite3
from datetime import date, datetime

import requests
import pandas as pd

import dash
import dash_bootstrap_components as dbc
import dash_ag_grid as dag

from dash import Dash, Input, Output, State, html, dcc, no_update
from dash.exceptions import PreventUpdate

# Repo-auth + layout
from dash_auth_external import DashAuthExternal
from layout import Footer, Navbar, Pagination, GeographyFilters
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Reuse your working Juvonno + dashboard plumbing
from training_dashboard import (
    _require_api_key, _get, tidy_date_str,
    fetch_customers_full, list_complaints_for_appt,
    extract_training_status, encounter_ids_for_appt, fetch_encounter,
    PASTEL_COLOR, STATUS_ORDER, GROUP_OPTS, _norm,
)

# ────────────────────────────────────────────────────────────
# Auth / Flask server (same pattern as repo)
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

# ────────────────────────────────────────────────────────────
# Small helpers

def _json_safe(obj):
    import numpy as np
    if isinstance(obj, (np.integer, )):  return int(obj)
    if isinstance(obj, (np.floating,)):  return float(obj)
    if isinstance(obj, (np.bool_,   )):  return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime, date)): return obj.isoformat()
    if isinstance(obj, set): return list(obj)
    return str(obj) if obj is not None else ""

def _fmt_date(val) -> str:
    try: return pd.to_datetime(val).strftime("%Y-%m-%d")
    except Exception: return str(val) if val else ""

def _last_appt_date(appt_list):
    dates = []
    for ap in appt_list:
        ds = tidy_date_str(ap.get("date"))
        try: dates.append(pd.to_datetime(ds))
        except Exception: pass
    return _fmt_date(max(dates)) if dates else ""

def _latest_status_fast(appt_list):
    """
    Fast 'current status': use ONLY the latest appointment's encounter(s).
    Keeps Tab 1 snappy. Tab 2 still does detailed forward-fill.
    """
    if not appt_list:
        return ""
    best = None; best_dt = None
    for ap in appt_list:
        ds = tidy_date_str(ap.get("date"))
        dt = pd.to_datetime(ds, errors="coerce")
        if pd.isna(dt): continue
        if best_dt is None or dt > best_dt:
            best_dt = dt; best = ap
    if not best:
        return ""
    aid = best.get("id")
    eids = encounter_ids_for_appt(aid)
    if not eids:
        return ""
    try:
        return extract_training_status(fetch_encounter(max(eids))) or ""
    except Exception:
        return ""

def _status_html(text: str) -> str:
    color = PASTEL_COLOR.get(text, "#e6e6e6")
    return (
        f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
        f'background:{color};border:1px solid rgba(0,0,0,.25);margin-right:6px"></span>'
        f'{text}'
    )

# ────────────────────────────────────────────────────────────
# Comments persistence (extend the same DB used by training_dashboard)
# We add columns author, complaint, status if they don't exist.
from training_dashboard import DB_PATH  # uses comments.db in this folder

def _ensure_comment_columns():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(comments)")
    cols = {row[1] for row in cur.fetchall()}
    alters = []
    if "author" not in cols:
        alters.append("ALTER TABLE comments ADD COLUMN author TEXT")
    if "complaint" not in cols:
        alters.append("ALTER TABLE comments ADD COLUMN complaint TEXT")
    if "status" not in cols:
        alters.append("ALTER TABLE comments ADD COLUMN status TEXT")
    for sql in alters:
        try: cur.execute(sql)
        except Exception: pass
    conn.commit()
    conn.close()

def add_comment_with_meta(customer_id: int, customer_label: str, date_str: str,
                          comment: str, author: str, complaint: str, status: str):
    _ensure_comment_columns()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        "INSERT INTO comments(customer_id, customer_label, date, comment, created_at, author, complaint, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (int(customer_id) if customer_id is not None else None,
         customer_label or "", date_str, comment.strip(),
         datetime.utcnow().isoformat(timespec="seconds"),
         author or "", complaint or "", status or "")
    )
    conn.commit()
    conn.close()

def list_comments_for_customer(customer_id: int):
    _ensure_comment_columns()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
        SELECT date, author, customer_label, complaint, status, comment
        FROM comments
        WHERE customer_id = ?
        ORDER BY date ASC, id ASC
    """, (int(customer_id),))
    rows = cur.fetchall()
    conn.close()
    return [
        {"Date": r[0], "Author": r[1], "Athlete": r[2], "Complaint": r[3], "Status": r[4], "Comment": r[5]}
        for r in rows
    ]

# ────────────────────────────────────────────────────────────
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

    # store current user JSON for comments author
    dcc.Store(id="me-json", data=None),

    Footer().render(),
])

# ────────────────────────────────────────────────────────────
# Tab content builders

def render_tab1():
    return dbc.Container([
        dbc.Row([
            dbc.Col(dcc.Dropdown(id="t1-group-dd", options=GROUP_OPTS, multi=True,
                                 placeholder="Select patient group(s) …"), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
        ], className="g-2 mb-3"),

        dbc.Alert(id="t1-msg", is_open=False, color="danger", duration=0),

        # Grid
        html.Div(id="t1-grid-container", className="mb-3"),
        dcc.Store(id="t1-rows-json"),

        # Comments panel (Tab 1)
        dbc.Card([
            dbc.CardHeader("Comments", className="bg-light"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(dcc.Dropdown(id="t1-athlete-dd", placeholder="Choose athlete …"), md=4),
                    dbc.Col(dcc.Dropdown(id="t1-complaint-dd", placeholder="Choose complaint …"), md=4),
                    dbc.Col(dcc.DatePickerSingle(id="t1-date", display_format="YYYY-MM-DD"), md=2),
                ], className="g-2 mb-2"),
                dbc.Row([
                    dbc.Col(dcc.Textarea(
                        id="t1-comment-text",
                        placeholder="Add a note for this athlete + complaint …",
                        style={"width":"100%","height":"80px"}
                    ), md=10),
                    dbc.Col(dbc.Button("Save Comment", id="t1-save", color="success", className="w-100"), md=2),
                ], className="g-2"),
                html.Div(id="t1-comment-hint", className="text-muted mt-1", style={"fontSize":"12px"}),
                html.Hr(),
                dag.AgGrid(
                    id="t1-comments-grid",
                    columnDefs=[
                        {"headerName":"Date","field":"Date","flex":1},
                        {"headerName":"Author","field":"Author","flex":1},
                        {"headerName":"Athlete","field":"Athlete","flex":2},
                        {"headerName":"Complaint","field":"Complaint","flex":2},
                        {"headerName":"Status","field":"Status","flex":2},
                        {"headerName":"Comment","field":"Comment","flex":4},
                    ],
                    rowData=[],
                    defaultColDef={"resizable":True,"filter":True,"sortable":True,"floatingFilter":True},
                    dashGridOptions={
                        "pagination": True, "paginationPageSize": 10,
                        "paginationPageSizeSelector": [10, 20, 50, 100],
                        "animateRows": True
                    },
                    className="ag-theme-quartz",
                    style={"height":"340px","width":"100%"}
                ),
            ])
        ], className="mb-4", style={"border":"1px solid #e9ecef","borderRadius":"8px"}),
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

# ────────────────────────────────────────────────────────────
# Init/login & navbar badge + cache user in Store

@app.callback(
    Output("redirect-to", "href"),
    Output("navbar-user", "children"),
    Output("me-json", "data"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if not token:
        return "login", "", None

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
        return no_update, f"Signed in as: {label}", me
    except Exception:
        return no_update, "", None

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

# ────────────────────────────────────────────────────────────
# Tab 1: fetch & render athletes grid (fast) + wire Comments

def _pill_renderer():
    # returns a renderer dict accepted by dash-ag-grid that builds DOM and sets innerHTML
    return {
        "function": """
        function(params) {
            const arr = Array.isArray(params.value)
              ? params.value
              : String(params.value || "")
                 .split(";")
                 .map(s => s.trim())
                 .filter(Boolean);
            const html = arr.map(t =>
              `<span style="display:inline-block;padding:3px 8px;border-radius:9999px;
                font-size:12px;background:#f1f3f5;color:#111;border:1px solid #e3e6eb;
                margin:2px 6px 2px 0">${t}</span>`
            ).join(" ");
            const e = document.createElement("span");
            e.innerHTML = html;
            return e;
        }
        """
    }

def _html_renderer():
    return {
        "function": """
        function(params){
            const e = document.createElement("span");
            e.innerHTML = params.value || "";
            return e;
        }
        """
    }

def _build_grid(rows):
    col_defs = [
        {"headerName":"Athlete","field":"Athlete","flex":2, "filter":True, "sortable":True},
        {"headerName":"Groups","field":"Groups","flex":2, "cellRenderer": _pill_renderer()},
        {"headerName":"Current Status","field":"Current Status","flex":2,
         "cellRenderer": _html_renderer(), "filter":True, "sortable":True},
        {"headerName":"Complaints","field":"Complaints","flex":3, "cellRenderer": _pill_renderer()},
        {"headerName":"Last Appt","field":"Last Appt","flex":1, "filter":True, "sortable":True},
        # hidden id for selection mapping
        {"headerName":"_cid","field":"_cid","hide":True},
    ]
    return dag.AgGrid(
        id="t1-grid",
        columnDefs=col_defs,
        rowData=rows,
        defaultColDef={"resizable":True,"filter":True,"sortable":True,"floatingFilter":True},
        dashGridOptions={
            "rowSelection": {"mode": "single"},
            "animateRows": True,
            "pagination": True,
            "paginationPageSize": 20,
            "paginationPageSizeSelector": [10, 20, 50, 100],
            "suppressRowClickSelection": False,
            "ensureDomOrder": True,
        },
        className="ag-theme-quartz",
        style={"height":"520px","width":"100%"},
        dangerously_allow_code=True,
    )

@dag.patch()
def _pill_renderer():
    pass

@dag.patch()
def _html_renderer():
    pass

@app.callback(
    Output("t1-grid-container", "children"),
    Output("t1-rows-json", "data"),
    Output("t1-athlete-dd", "options"),
    Output("t1-athlete-dd", "value"),
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
            return no_update, no_update, [], None, "Select at least one group.", True

        # Fresh fetch of customers (same approach as training_dashboard)
        customers_by_id = fetch_customers_full()

        # groups per customer
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
            return html.Div("No athletes found for those groups."), [], [], None, "", False

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

        # Build rows (FAST) + complaints-from-appointments (fast)
        rows = []
        dd_opts = []
        for cid in filtered_cids:
            cust = customers_by_id.get(cid, {}) or {}
            first = cust.get("first_name", "") or ""
            last  = cust.get("last_name", "") or ""
            athlete_label = f"{first} {last}".strip() or f"ID {cid}"

            grp_display = [g.title() for g in cid_to_groups.get(cid, [])]
            my_appts    = cid_to_appts.get(cid, [])

            # Status & last appt
            current_status = _latest_status_fast(my_appts)
            last_appt     = _last_appt_date(my_appts)

            # Complaints from appointments only (fast)
            comp_names = set()
            for ap in my_appts:
                aid = ap.get("id")
                # structured complaints
                try:
                    for rec in list_complaints_for_appt(aid):
                        nm = None
                        for k in ("name","title","problem","injury","body_part","complaint"):
                            v = rec.get(k)
                            if isinstance(v, str) and v.strip():
                                nm = v.strip(); break
                        if nm: comp_names.add(nm)
                except Exception:
                    pass
                # inline complaint
                comp_inline = ap.get("complaint")
                if isinstance(comp_inline, dict):
                    for k in ("name","title","problem","injury","body_part","complaint"):
                        v = comp_inline.get(k)
                        if isinstance(v, str) and v.strip():
                            comp_names.add(v.strip()); break

            comp_list = sorted(comp_names)

            rows.append({
                "Athlete": athlete_label,
                "Groups": grp_display,
                "Current Status": _status_html(current_status),
                "Complaints": comp_list,
                "Last Appt": last_appt,
                "_cid": int(cid),
            })
            dd_opts.append({"label": athlete_label, "value": int(cid)})

        rows.sort(key=lambda r: r["Athlete"].lower())
        dd_opts.sort(key=lambda o: o["label"].lower())

        grid = _build_grid(rows)
        rows_json = json.loads(json.dumps(rows, default=_json_safe))

        # Default athlete = first in list
        default_cid = dd_opts[0]["value"] if dd_opts else None

        return grid, rows_json, dd_opts, default_cid, "", False

    except Exception as e:
        return no_update, no_update, [], None, f"Error: {e}", True

# When athlete changes → populate complaints dd + set date default = today
@app.callback(
    Output("t1-complaint-dd", "options"),
    Output("t1-complaint-dd", "value"),
    Output("t1-date", "date"),
    Output("t1-comment-hint", "children"),
    Input("t1-athlete-dd", "value"),
    State("t1-rows-json", "data"),
)
def t1_set_complaints(selected_cid, rows_json):
    if not selected_cid or not rows_json:
        return [], None, _fmt_date(date.today()), "Select an athlete to add/view comments."
    # Find that athlete's complaint list from our cached rows_json
    try:
        comp_list = []
        for r in rows_json:
            if int(r.get("_cid")) == int(selected_cid):
                comp_list = r.get("Complaints") or []
                break
        opts = [{"label": c, "value": c} for c in comp_list] or []
        default_val = opts[0]["value"] if opts else None
        return opts, default_val, _fmt_date(date.today()), "Fill out the comment and click Save."
    except Exception:
        return [], None, _fmt_date(date.today()), "Fill out the comment and click Save."

# Save comment (Tab 1) with author + status → DB; refresh table
@app.callback(
    Output("t1-comments-grid", "rowData"),
    State("t1-athlete-dd", "value"),
    State("t1-rows-json", "data"),
    State("t1-complaint-dd", "value"),
    State("t1-date", "date"),
    State("t1-comment-text", "value"),
    State("me-json", "data"),
    Input("t1-save", "n_clicks"),
    prevent_initial_call=True
)
def t1_save_comment(selected_cid, rows_json, complaint, date_str, text, me_json, _n):
    if not selected_cid or not date_str or not (text or "").strip():
        raise PreventUpdate

    # get label and status from cached rows
    label = f"ID {selected_cid}"
    status = ""
    if rows_json:
        for r in rows_json:
            if int(r.get("_cid")) == int(selected_cid):
                label = r.get("Athlete") or label
                # strip HTML from Current Status to get raw text for DB column
                raw = r.get("Current Status") or ""
                status = (
                    str(raw).split("</span>", 1)[-1].strip()
                    if "</span>" in str(raw) else str(raw)
                )
                break

    author = ""
    if me_json:
        first = me_json.get("first_name") or ""
        last  = me_json.get("last_name")  or ""
        email = me_json.get("email") or ""
        author = (first + " " + last).strip() or email

    add_comment_with_meta(
        customer_id=int(selected_cid),
        customer_label=label,
        date_str=_fmt_date(date_str),
        comment=text.strip(),
        author=author,
        complaint=complaint or "",
        status=status or "",
    )

    # Refresh table
    return list_comments_for_customer(int(selected_cid))

# Also refresh comments grid when athlete changes (without saving)
@app.callback(
    Output("t1-comments-grid", "rowData", allow_duplicate=True),
    Input("t1-athlete-dd", "value"),
    prevent_initial_call=True
)
def t1_refresh_comments_on_select(selected_cid):
    if not selected_cid:
        return []
    return list_comments_for_customer(int(selected_cid))

# ────────────────────────────────────────────────────────────
# Tab 2: register existing training dashboard callbacks
def _register_training_callbacks(app: Dash):
    from training_dashboard import register_callbacks as td_register_callbacks
    td_register_callbacks(app)

_register_training_callbacks(app)

# ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=False, port=8050)
