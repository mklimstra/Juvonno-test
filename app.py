# app.py
import os
import math
import json
import hashlib
import sqlite3
from datetime import date
import requests
import pandas as pd
import dash
import traceback

from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update, dash_table, State, callback_context
import dash_bootstrap_components as dbc

from html import escape as html_escape

# Repo components & settings
from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Training tab (unchanged logic & JUV API calls)
import training_dashboard as td


# ------------------------- Auth / Server -------------------------
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Serve assets (same as repo)
here = os.path.dirname(os.path.abspath(__file__))
assets_path = os.path.join(here, "assets")
server.static_folder = assets_path
server.static_url_path = "/assets"


# ------------------------- Small helpers (pills/dots/colors) -------------------------
PILL_BG_DEFAULT = "#eef2f7"

# soft pastel palette; stable selection via md5(text) % len(PALETTE)
PALETTE = [
    "#e7f0ff",  # blue-ish
    "#fde2cf",  # peach
    "#e6f3e6",  # green-ish
    "#f3e6f7",  # purple-ish
    "#fff3cd",  # soft yellow
    "#e0f7fa",  # cyan-ish
    "#fbe7eb",  # pink-ish
    "#e7f5ff",  # light blue
]
BORDER = "#cfd6de"

def color_for_label(text: str) -> str:
    if not text:
        return PILL_BG_DEFAULT
    h = hashlib.md5(text.encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(PALETTE)
    return PALETTE[idx]

def pill(text: str, bg=None, fg="#111", border=BORDER):
    bg = bg or PILL_BG_DEFAULT
    return (
        f'<span style="display:inline-block;padding:2px 8px;'
        f'border-radius:999px;background:{bg};color:{fg};'
        f'border:1px solid {border};font-size:12px;'
        f'line-height:18px;white-space:nowrap;">{html_escape(text)}</span>'
    )

def dot(hex_color: str, size: int = 10, mr: int = 8) -> str:
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'border-radius:50%;background:{hex_color};margin-right:{mr}px;'
        f'border:1px solid rgba(0,0,0,.25)"></span>'
    )


# ------------------------- Tab 1 (Overview) Layout -------------------------
def tab1_layout():
    return dbc.Container([
        html.H3("Overview", className="mt-2"),

        dbc.Row([
            dbc.Col(dcc.Dropdown(
                id="t1-group-dd",
                options=td.GROUP_OPTS,  # reuse groups computed in training_dashboard
                multi=True,
                placeholder="Select patient group(s)…"
            ), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
        ], className="g-2 mb-2"),

        dbc.Alert(id="t1-msg", is_open=False, color="danger"),

        # main table
        html.Div(id="t1-grid-container"),
        dcc.Store(id="t1-rows-json", data=[]),

        html.Hr(),

        # Comments section bound to the selected row in the table
        dbc.Card([
            dbc.CardHeader([
                html.Span("Comments", className="me-2"),
                html.Span(id="t1-selected-athlete-label", className="fw-semibold text-muted")
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(dcc.DatePickerSingle(id="t1-comment-date", display_format="YYYY-MM-DD"), md=3),
                    dbc.Col(dcc.Dropdown(id="t1-complaint-dd", placeholder="Pick a complaint (optional)…"), md=4),
                    dbc.Col(dcc.Textarea(
                        id="t1-comment-text",
                        placeholder="Add a note about the selected athlete…",
                        style={"width":"100%","height":"80px"}), md=5),
                ], className="g-2"),
                dbc.Row([
                    dbc.Col(dbc.Button("Save Comment", id="t1-save-comment", color="success"), width="auto"),
                    dbc.Col(html.Div(id="t1-comment-hint", className="text-muted", style={"fontSize":"12px"}))
                ], className="g-2 mt-2"),

                html.Hr(),

                dash_table.DataTable(
                    id="t1-comments-table",
                    columns=[
                        {"name":"Date","id":"Date", "editable": False},
                        {"name":"By","id":"By", "editable": False},
                        {"name":"Athlete","id":"Athlete", "editable": False},
                        {"name":"Complaint","id":"Complaint", "editable": False},
                        {"name":"Status","id":"Status", "editable": False},
                        {"name":"Comment","id":"Comment", "editable": True},  # ONLY this column editable
                        {"name":"_id","id":"_id", "hidden": True, "editable": False},
                    ],
                    data=[],
                    row_deletable=True,               # delete icon per row
                    editable=True,                    # per-column overrides above
                    page_action="none",               # we use scroll, not pagination
                    style_table={
                        "overflowX": "auto",
                        "maxHeight": "240px",         # ~5 rows visible then scroll
                        "overflowY": "auto",
                    },
                    # Make delete button cell light red
                    css=[{
                        "selector": ".dash-table-container .dash-spreadsheet-container td.dash-delete-cell",
                        "rule": "background-color: #fdecea; border-right: 1px solid #f5c6cb;"
                    }],
                    # Uniform spacing across cells/rows
                    style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","lineHeight":"22px"},
                    style_cell={"padding":"9px","fontSize":14,"lineHeight":"22px",
                                "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                                "textAlign":"left"},
                    style_data={"borderBottom":"1px solid #eceff4"},
                    style_data_conditional=[{"if": {"row_index":"odd"}, "backgroundColor":"#fbfbfd"}],
                ),

                # MOVED: comment update flag to bottom of the card
                dbc.Alert(id="t1-comment-msg", is_open=False, color="info", className="mt-3", duration=3000),
            ])
        ], className="mb-4"),

    ], fluid=True)


# ------------------------- Tab 2 (Training Dashboard) Layout -------------------------
def tab2_layout():
    return td.layout_body()


# ------------------------- Page Layout -------------------------
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css",
    ],
    suppress_callback_exceptions=True,
)

app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),

    # one-time init for login / redirect
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    # user badge refresh (signed-in name) every 60s
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    # Navbar with right-slot for user label
    Navbar([
        html.Span(id="navbar-user", className="text-white-50 small", children="")
    ]).render(),

    dbc.Container([
        dcc.Tabs(
            id="main-tabs",
            value="tab-1",
            children=[
                dcc.Tab(label="Overview", value="tab-1"),
                dcc.Tab(label="Training Dashboard", value="tab-2"),
            ],
        ),
        html.Div(id="tabs-content", className="mt-3"),
    ], fluid=True),

    Footer().render(),
])


# ------------------------- Tab switcher -------------------------
@app.callback(
    Output("tabs-content", "children"),
    Input("main-tabs", "value"),
)
def render_tab(which):
    return tab1_layout() if which == "tab-1" else tab2_layout()


# ------------------------- Auth / Navbar user & login redirect -------------------------
@app.callback(
    Output("redirect-to", "href"),
    Input("init-interval", "n_intervals"),
    State("redirect-to", "pathname"),
)
def initial_view(n, pathname):
    """
    Redirect to /login only when no token AND we are not already on /login.
    This prevents the reload loop on the /login page.
    """
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if token:
        return no_update

    if (pathname or "").rstrip("/") == "/login":
        return no_update

    return "login"


@app.callback(
    Output("navbar-user", "children"),
    Input("user-refresh", "n_intervals"),
)
def refresh_user_badge(_n):
    """Show 'Signed in as: First Last' once token is present; otherwise show 'Sign in'."""
    try:
        token = auth.get_token()
        if not token:
            return html.A("Sign in", href="login", className="link-light")
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(f"{SITE_URL}/api/csiauth/me/", headers=headers, timeout=5)
        if resp.status_code != 200:
            return html.A("Sign in", href="login", className="link-light")
        js = resp.json()
        name = " ".join([js.get("first_name","").strip(), js.get("last_name","").strip()]).strip()
        if not name:
            name = js.get("email","")
        return f"Signed in as: {name or 'Unknown user'}"
    except Exception:
        return html.A("Sign in", href="login", className="link-light")


# ------------------------- Tab 1: Load Customers -------------------------
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

        rows = []
        for cid, cust in td.CUSTOMERS.items():
            cust_groups = set(td.CID_TO_GROUPS.get(cid, []))
            if not (targets & cust_groups):
                continue

            first = (cust.get("first_name") or "").strip()
            last  = (cust.get("last_name") or "").strip()

            # colorful pills per group
            groups_html = " ".join(
                pill(g.title(), color_for_label(g)) for g in sorted(cust_groups)
            ) if cust_groups else "—"

            # Current training status (forward-fill)
            appts = td.CID_TO_APPTS.get(cid, [])
            status_rows = []
            for ap in appts:
                aid = ap.get("id")
                date_str = td.tidy_date_str(ap.get("date"))
                dt = pd.to_datetime(date_str, errors="coerce")
                if pd.isna(dt): continue
                eids = td.encounter_ids_for_appt(aid)
                max_eid = max(eids) if eids else None
                s = td.extract_training_status(td.fetch_encounter(max_eid)) if max_eid else ""
                if s: status_rows.append((dt.normalize(), s))

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
            status_html = f"{dot(status_color)}{html_escape(current_status) if current_status else '—'}" if current_status else "—"

            # Complaints pills
            complaints = td.fetch_customer_complaints(cid)
            complaint_names = [c["Title"] for c in complaints if c.get("Title")]
            complaints_html = " ".join(
                pill(t, color_for_label(t), border=BORDER) for t in complaint_names
            ) if complaint_names else "—"

            rows.append({
                "First Name": first,
                "Last Name":  last,
                "Groups": groups_html,
                "Current Status": status_html,
                "Complaints": complaints_html,
                "DOB": cust.get("dob") or cust.get("birthdate") or "—",
                "Sex": cust.get("sex") or cust.get("gender") or "—",
                "_cid": cid,
                "_athlete_label": f"{first} {last}".strip(),
            })

        if not rows:
            return html.Div("No athletes in those groups."), [], "", False

        columns = [
            {"name":"First Name", "id":"First Name"},
            {"name":"Last Name",  "id":"Last Name"},
            {"name":"Groups", "id":"Groups", "presentation":"markdown"},
            {"name":"Current Status", "id":"Current Status", "presentation":"markdown"},
            {"name":"Complaints", "id":"Complaints", "presentation":"markdown"},
            {"name":"DOB", "id":"DOB"},
            {"name":"Sex", "id":"Sex"},
        ]

        # ~5 visible rows with scroll, consistent spacing
        table = dash_table.DataTable(
            id="t1-athlete-table",
            data=rows,
            columns=columns,
            markdown_options={"html": True},
            # Filtering: case-insensitive + styled filter row
            filter_action="native",
            filter_options={"case": "insensitive"},
            style_filter={
                "backgroundColor": "#fafcff",
                "borderBottom": "1px solid #e6ebf1",
                "borderTop": "1px solid #e6ebf1",
                "fontStyle": "italic",
            },
            # Sorting
            sort_action="native",
            # Scroll container to ~5 rows
            page_action="none",
            style_table={"overflowX":"auto", "maxHeight":"240px", "overflowY":"auto"},
            style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","lineHeight":"22px"},
            style_cell={"padding":"9px","fontSize":14,"lineHeight":"22px",
                        "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                        "textAlign":"left"},
            style_data={"borderBottom":"1px solid #eceff4"},
            style_data_conditional=[{"if": {"row_index":"odd"}, "backgroundColor":"#fbfbfd"}],
            row_selectable="single",
            selected_rows=[],
        )

        return table, rows, "", False

    except Exception as e:
        tb = traceback.format_exc()
        msg = html.Div([
            html.Div("Error loading athletes:"),
            html.Pre(str(e)),
            html.Details([html.Summary("Traceback"), html.Pre(tb)], open=False)
        ])
        return no_update, no_update, msg, True


# ------------------------- Tab 1: On select athlete -------------------------
@app.callback(
    Output("t1-complaint-dd", "options"),
    Output("t1-complaint-dd", "value"),
    Output("t1-comments-table", "data"),
    Output("t1-comment-hint", "children"),
    Output("t1-selected-athlete-label", "children"),
    Output("t1-comment-date", "date"),
    Input("t1-athlete-table", "selected_rows"),
    State("t1-rows-json", "data"),
)
def t1_on_select(selected_rows, rows_json):
    if not rows_json:
        raise PreventUpdate
    if not selected_rows:
        today = date.today().strftime("%Y-%m-%d")
        return [], None, [], "Select an athlete above; comments will filter to that athlete.", "", today

    row = rows_json[selected_rows[0]]
    cid = int(row["_cid"])
    label = row["_athlete_label"]

    # complaint options for dropdown
    complaints = td.fetch_customer_complaints(cid)
    names = [c["Title"] for c in complaints if c.get("Title")]
    opts = [{"label": n, "value": n} for n in sorted(set(names))]
    val = opts[0]["value"] if opts else None

    # existing comments for this athlete (with ids)
    comments = _db_list_comments_with_ids([cid])
    expanded = [_expand_comment_record(rec, label) for rec in comments]

    today = date.today().strftime("%Y-%m-%d")

    return opts, val, expanded, f"Adding comment for: {label}", f" — {label}", today


# ------------------------- Tab 1: Save comment -------------------------
@app.callback(
    Output("t1-comments-table", "data", allow_duplicate=True),
    State("t1-athlete-table", "selected_rows"),
    State("t1-rows-json", "data"),
    State("t1-complaint-dd", "value"),
    State("t1-comment-date", "date"),
    State("t1-comment-text", "value"),
    Input("t1-save-comment", "n_clicks"),
    prevent_initial_call=True,
)
def t1_save_comment(selected_rows, rows_json, complaint, date_str, text, _n):
    if not _n or not rows_json or not selected_rows or not date_str or not (text or "").strip():
        raise PreventUpdate

    row = rows_json[selected_rows[0]]
    cid = int(row["_cid"])
    label = row["_athlete_label"]

    # Persist to SQLite (same DB used by training tab)
    td.db_add_comment(cid, label, date_str, text.strip())

    # User display (By:)
    by_who = _get_signed_in_name()

    comments = _db_list_comments_with_ids([cid])
    expanded = [_expand_comment_record(rec, label, override_by=by_who, override_complaint=complaint) for rec in comments]
    return expanded


# ------------------------- Tab 1: Persist edits/deletes -------------------------
@app.callback(
    Output("t1-comment-msg", "is_open", allow_duplicate=True),
    Output("t1-comment-msg", "children", allow_duplicate=True),
    Input("t1-comments-table", "data_timestamp"),
    State("t1-comments-table", "data"),
    State("t1-comments-table", "data_previous"),
    prevent_initial_call=True,
)
def t1_persist_comment_mutations(_ts, data, data_prev):
    """
    Detect row deletes or inline edits and update SQLite accordingly.
    """
    try:
        if data_prev is None:
            raise PreventUpdate

        prev_by_id = {r["_id"]: r for r in data_prev if r.get("_id") is not None}
        now_by_id  = {r["_id"]: r for r in data     if r.get("_id") is not None}

        # Deletes: present before, missing now
        deleted_ids = [cid for cid in prev_by_id.keys() if cid not in now_by_id]
        for i in deleted_ids:
            _db_delete_comment(i)

        # Edits: present in both, comment text changed
        any_edit = False
        for cid, now in now_by_id.items():
            before = prev_by_id.get(cid)
            if not before:
                continue
            if (before.get("Comment") or "") != (now.get("Comment") or ""):
                _db_update_comment_text(cid, now.get("Comment") or "")
                any_edit = True

        if deleted_ids or any_edit:
            return True, "Comments updated."
        else:
            raise PreventUpdate
    except Exception as e:
        return True, f"Comment persistence error: {e}"


# =========================
# SQLite helpers (reuse td.DB_PATH)
# =========================
def _db_connect():
    return sqlite3.connect(td.DB_PATH, check_same_thread=False)

def _db_list_comments_with_ids(customer_ids):
    conn = _db_connect(); cur = conn.cursor()
    if customer_ids:
        vals = [int(x) for x in customer_ids]
        q = ",".join("?" for _ in vals)
        cur.execute(f"""
          SELECT id, date, comment, customer_label, customer_id, created_at
          FROM comments
          WHERE customer_id IN ({q})
          ORDER BY date ASC, id ASC
        """, vals)
    else:
        cur.execute("SELECT id, date, comment, customer_label, customer_id, created_at FROM comments ORDER BY date ASC, id ASC")
    rows = cur.fetchall(); conn.close()
    # return dicts
    return [{
        "_id": r[0],
        "Date": r[1],
        "Comment": r[2],
        "Athlete": r[3],
        "_cid": r[4],
        "_created_at": r[5],
    } for r in rows]

def _db_delete_comment(comment_id: int):
    conn = _db_connect(); cur = conn.cursor()
    cur.execute("DELETE FROM comments WHERE id = ?", (int(comment_id),))
    conn.commit(); conn.close()

def _db_update_comment_text(comment_id: int, new_text: str):
    conn = _db_connect(); cur = conn.cursor()
    cur.execute("UPDATE comments SET comment = ? WHERE id = ?", (new_text, int(comment_id)))
    conn.commit(); conn.close()

def _expand_comment_record(rec, athlete_label, override_by=None, override_complaint=None):
    """
    Map raw DB row -> table row. We keep _id hidden for persistence.
    """
    return {
        "_id": rec["_id"],
        "Date": rec["Date"],
        "By": override_by or "",            # we don’t store author; show current user on add
        "Athlete": athlete_label,
        "Complaint": override_complaint or "",
        "Status": "",                       # wire up if/when a status column is stored
        "Comment": rec["Comment"],
    }

def _get_signed_in_name():
    """Get 'First Last' (fallback email) for the current user via /api/csiauth/me/."""
    try:
        token = auth.get_token()
        if not token:
            return ""
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(f"{SITE_URL}/api/csiauth/me/", headers=headers, timeout=5)
        if r.status_code != 200:
            return ""
        js = r.json()
        name = " ".join([js.get("first_name","").strip(), js.get("last_name","").strip()]).strip()
        if not name:
            name = js.get("email","")
        return name or ""
    except Exception:
        return ""


# ------------------------- Register Training tab callbacks -------------------------
td.register_callbacks(app)


# ------------------------- Main -------------------------
if __name__ == "__main__":
    app.run(debug=False, port=8050)
