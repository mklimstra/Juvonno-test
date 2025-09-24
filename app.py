# app.py
import os, hashlib, base64, sqlite3, traceback, functools
from datetime import date
from html import escape as html_escape

import requests
import pandas as pd
import dash
from dash_auth_external import DashAuthExternal
from dash import Dash, Input, Output, State, html, dcc, dash_table, no_update
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc

# Repo components & settings
from layout import Footer, Navbar
from settings import *  # AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET
import training_dashboard as td  # reuse groups, API access, DB path, etc.

# ───────────────────────── Auth / Server ─────────────────────────
auth = DashAuthExternal(
    AUTH_URL, TOKEN_URL,
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

# Ensure the SQLite table exists on first run (so first comment works).
try:
    td._db().close()
except Exception:
    pass

# ───────────────────────── Styles (tabs & pills) ─────────────────────────
TABS_CONTAINER_STYLE = {
    "display": "flex",
    "gap": "6px",
    "alignItems": "center",
    "borderBottom": "0",
    "marginBottom": "4px",
    "width": "100%",
}
TAB_STYLE = {
    "padding": "8px 14px",
    "border": "1px solid #e9ecef",
    "borderRadius": "8px",
    "background": "#f8f9fb",
    "color": "#495057",
    "fontWeight": "500",
    "flex": "1 1 0%",
    "textAlign": "center",
}
TAB_SELECTED_STYLE = {
    "padding": "8px 14px",
    "border": "1px solid #cfe2ff",
    "borderRadius": "8px",
    "background": "#e7f1ff",
    "color": "#084298",
    "fontWeight": "600",
    "boxShadow": "inset 0 1px 0 rgba(255,255,255,.6)",
    "flex": "1 1 0%",
    "textAlign": "center",
}

# ───────────────────────── UI helpers (pills/dots/colors) ─────────────────────────
PILL_BG_DEFAULT = "#eef2f7"
PILL_BORDER_RADIUS = "6px"  # less rounded corners
PALETTE = ["#e7f0ff", "#fde2cf", "#e6f3e6", "#f3e6f7", "#fff3cd", "#e0f7fa", "#fbe7eb", "#e7f5ff"]
BORDER = "#cfd6de"

def color_for_label(text: str) -> str:
    if not text:
        return PILL_BG_DEFAULT
    h = hashlib.md5(text.encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(PALETTE)
    return PALETTE[idx]

def pill_html(text: str, bg=None, fg="#111", border=BORDER) -> str:
    bg = bg or PILL_BG_DEFAULT
    return (
        f'<span style="display:inline-block;padding:2px 8px;'
        f'border-radius:{PILL_BORDER_RADIUS};background:{bg};color:{fg};'
        f'border:1px solid {border};font-size:12px;'
        f'line-height:18px;white-space:nowrap;">{html_escape(text)}</span>'
    )

def dot_html(hex_color: str, size: int = 10, mr: int = 8) -> str:
    return (
        f'<span style="display:inline-block;width:{size}px;height:{size}px;'
        f'border-radius:50%;background:{hex_color};margin-right:{mr}px;'
        f'border:1px solid rgba(0,0,0,.25)"></span>'
    )

def status_pill_component(text: str, kind: str = "success"):
    if kind == "success":
        style = {
            "display": "inline-block", "padding": "2px 8px", "borderRadius": PILL_BORDER_RADIUS,
            "background": "#e9f7ef", "color": "#0f5132", "border": "1px solid #badbcc",
            "fontSize": "12px", "lineHeight": "18px", "whiteSpace": "nowrap"
        }
    elif kind == "danger":
        style = {
            "display": "inline-block", "padding": "2px 8px", "borderRadius": PILL_BORDER_RADIUS,
            "background": "#fdecea", "color": "#842029", "border": "1px solid #f5c2c7",
            "fontSize": "12px", "lineHeight": "18px", "whiteSpace": "nowrap"
        }
    else:
        style = {
            "display": "inline-block", "padding": "2px 8px", "borderRadius": PILL_BORDER_RADIUS,
            "background": "#eef2f7", "color": "#111", "border": "1px solid #cfd6de",
            "fontSize": "12px", "lineHeight": "18px", "WhiteSpace": "nowrap"
        }
    return html.Span(text, style=style)

# ───────────────────────── Signed-in name helpers ─────────────────────────
def _b64url_decode(part: str) -> bytes:
    part = part + '=' * (-len(part) % 4)
    return base64.urlsafe_b64decode(part.encode("utf-8"))

def _name_from_jwt(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) < 2: return ""
        payload = _b64url_decode(parts[1]).decode("utf-8")
        js = __import__("json").loads(payload)
        first = (js.get("given_name") or js.get("first_name") or "").strip()
        last  = (js.get("family_name") or js.get("last_name") or "").strip()
        name  = (f"{first} {last}").strip() or js.get("name") or ""
        if not name:
            name = js.get("preferred_username") or js.get("email") or ""
        return name
    except Exception:
        return ""

def _get_signed_in_name() -> str:
    try:
        token = auth.get_token()
        if not token:
            return ""
        # Try Bearer
        try:
            r = requests.get(f"{SITE_URL}/api/csiauth/me/",
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                             timeout=5)
            if r.status_code == 200:
                js = r.json()
                first = (js.get("first_name") or "").strip()
                last  = (js.get("last_name") or "").strip()
                name = f"{first} {last}".strip() or js.get("email", "")
                if name: return name
        except Exception:
            pass
        # Try query param
        try:
            r2 = requests.get(f"{SITE_URL}/api/csiauth/me/", params={"access_token": token}, timeout=5)
            if r2.status_code == 200:
                js = r2.json()
                first = (js.get("first_name") or "").strip()
                last  = (js.get("last_name") or "").strip()
                name = f"{first} {last}".strip() or js.get("email", "")
                if name: return name
        except Exception:
            pass
        # JWT decode fallback
        return _name_from_jwt(token) or ""
    except Exception:
        return ""

# ───────────────────────── Status override (limited to 4 labels) ─────────────────────────
STATUS_CHOICES = [
    "Full participation without Health problems",
    "Full participation with Illness/Injury",
    "Reduced participation with Illness/Injury",
    "No Participation due to Illness/Injury",
]

# ───────────────────────── Fast path: cache current status per athlete ─────────────────────────
@functools.lru_cache(maxsize=2048)
def _current_status_for_customer(cid: int) -> str:
    """Compute forward-filled current training status for a customer id."""
    appts = td.CID_TO_APPTS.get(int(cid), [])
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
    if not status_rows:
        return ""
    df_s = pd.DataFrame(status_rows, columns=["Date", "Status"]).sort_values("Date")
    df_s = df_s.drop_duplicates("Date", keep="last")
    full_idx = pd.date_range(start=df_s["Date"].min(),
                             end=pd.Timestamp("today").normalize(), freq="D")
    df_full = pd.DataFrame({"Date": full_idx}).merge(df_s, on="Date", how="left").sort_values("Date")
    df_full["Status"] = df_full["Status"].ffill()
    return str(df_full.iloc[-1]["Status"]) if not df_full.empty else ""

# ───────────────────────── Tab 1 (Overview) ─────────────────────────
def tab1_layout():
    return dbc.Container([
        html.H3("Athlete List", className="mt-2"),

        dbc.Row([
            dbc.Col(dcc.Dropdown(
                id="t1-group-dd",
                options=td.GROUP_OPTS,
                multi=True,
                placeholder="Select athlete group(s)…"
            ), md=6),
            dbc.Col(dbc.Button("Load", id="t1-load", color="primary", className="w-100"), md=2),
        ], className="g-2 mb-2"),

        dbc.Alert(id="t1-msg", is_open=False, color="danger"),

        html.Div(id="t1-grid-container"),
        dcc.Store(id="t1-rows-json", data=[]),

        html.Hr(),

        dbc.Card([
            dbc.CardHeader([
                html.Span("Comments", className="me-2"),
                html.Span(id="t1-selected-athlete-label", className="fw-semibold text-muted")
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(dcc.Textarea(
                        id="t1-comment-text",
                        placeholder="Add a note about the selected athlete…",
                        style={"width":"100%","height":"110px"}
                    ), md=8),
                    dbc.Col([
                        dcc.DatePickerSingle(id="t1-comment-date", display_format="YYYY-MM-DD", style={"width":"100%"}),
                        dcc.Dropdown(id="t1-complaint-dd", placeholder="Pick a complaint (optional)…",
                                     style={"width":"100%","marginTop":"6px"}),

                        # Toggle + collapsible Status Override
                        dbc.Button(
                            "Show Status Override", id="t1-toggle-status", color="secondary",
                            className="w-100", style={"marginTop": "6px"}
                        ),
                        dbc.Collapse(
                            dcc.Dropdown(
                                id="t1-status-override",
                                options=[{"label": s, "value": s} for s in STATUS_CHOICES],
                                placeholder="Override status…",
                                clearable=True,
                                style={"width": "100%", "marginTop": "6px"}
                            ),
                            id="t1-status-collapse",
                            is_open=False
                        ),

                        dbc.Button("Save Comment", id="t1-save-comment", color="success",
                                   className="w-100", style={"marginTop":"6px"}),
                    ], md=4),
                ], className="g-2"),

                html.Div(id="t1-comment-status", className="mt-2"),

                html.Hr(),

                dash_table.DataTable(
                    id="t1-comments-table",
                    columns=[
                        {"name":"Date","id":"Date", "editable": False},
                        {"name":"By","id":"By", "editable": False},
                        {"name":"Athlete","id":"Athlete", "editable": False},
                        {"name":"Complaint","id":"Complaint", "editable": False},
                        {"name":"Status","id":"Status", "editable": False},
                        {"name":"Comment","id":"Comment", "editable": True},
                        {"name":"_id","id":"_id", "hidden": True, "editable": False},
                    ],
                    data=[],
                    row_deletable=True,
                    editable=False,   # column-level editable keeps only Comment editable
                    page_action="none",
                    style_table={"overflowX":"auto","maxHeight":"240px","overflowY":"auto"},
                    style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","lineHeight":"22px"},
                    style_cell={"padding":"9px","fontSize":14,"lineHeight":"22px",
                                "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                                "textAlign":"left"},
                    style_data={"borderBottom":"1px solid #eceff4"},
                    style_data_conditional=[{"if": {"row_index":"odd"}, "backgroundColor":"#fbfbfd"}],
                ),
            ])
        ], className="mb-4"),
    ], fluid=True)

# ───────────────────────── Tab 2 (Training Dashboard) ─────────────────────────
def tab2_layout():
    return td.layout_body()

# ───────────────────────── App shell ─────────────────────────
app = Dash(
    __name__,
    server=server,
    external_stylesheets=[dbc.themes.BOOTSTRAP,
                          "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.5/font/bootstrap-icons.css"],
    suppress_callback_exceptions=True,
)

app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    Navbar([html.Span(id="navbar-user", className="text-white-50 small", children="")]).render(),

    dbc.Container([
        dcc.Tabs(
            id="main-tabs",
            value="tab-1",
            children=[
                dcc.Tab(label="Athlete Status", value="tab-1",
                        style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
                dcc.Tab(label="Status History", value="tab-2",
                        style=TAB_STYLE, selected_style=TAB_SELECTED_STYLE),
            ],
            style=TABS_CONTAINER_STYLE,
            parent_style={"width": "100%"},
            mobile_breakpoint=0,
        ),
        html.Div(id="tabs-content", className="mt-3"),
    ], fluid=True),

    Footer().render(),
])

# ───────────────────────── Tab switcher ─────────────────────────
@app.callback(Output("tabs-content", "children"), Input("main-tabs", "value"))
def render_tab(which):
    return tab1_layout() if which == "tab-1" else tab2_layout()

# ───────────────────────── Login redirect & navbar user ─────────────────────────
@app.callback(
    Output("redirect-to", "href"),
    Input("init-interval", "n_intervals"),
    State("redirect-to", "pathname"),
)
def initial_view(n, pathname):
    try:
        token = auth.get_token()
    except Exception:
        token = None
    if token:
        return no_update
    if (pathname or "").rstrip("/") == "/login":
        return no_update
    return "login"

@app.callback(Output("navbar-user", "children"), Input("user-refresh", "n_intervals"))
def refresh_user_badge(_n):
    try:
        name = _get_signed_in_name()
        return f"Signed in as: {name}" if name else html.A("Sign in", href="login", className="link-light")
    except Exception:
        return html.A("Sign in", href="login", className="link-light")

# ───────────────────────── Tab 1: Load customers ─────────────────────────
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

            groups_html = " ".join(
                pill_html(g.title(), color_for_label(g)) for g in sorted(cust_groups)
            ) if cust_groups else "—"

            # Current training status (now cached)
            current_status = _current_status_for_customer(int(cid))
            status_color = td.PASTEL_COLOR.get(current_status, "#e6e6e6")
            status_html = f"{dot_html(status_color)}{html_escape(current_status) if current_status else '—'}" if current_status else "—"

            # Complaints (cached in td)
            complaints = td.fetch_customer_complaints(cid)
            complaint_names = [c["Title"] for c in complaints if c.get("Title")]
            complaints_html = " ".join(
                pill_html(t, color_for_label(t), border=BORDER) for t in complaint_names
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

        table = dash_table.DataTable(
            id="t1-athlete-table",
            data=rows,
            columns=columns,
            markdown_options={"html": True},
            filter_action="native",
            filter_options={"case": "insensitive"},
            style_filter={
                "backgroundColor": "#fafcff",
                "borderBottom": "1px solid #e6ebf1",
                "borderTop": "1px solid #e6ebf1",
                "fontStyle": "italic",
            },
            sort_action="native",
            page_action="none",
            style_table={"overflowX":"auto", "maxHeight":"240px", "overflowY":"auto"},
            style_header={"fontWeight":"600","backgroundColor":"#f8f9fa","lineHeight":"22px"},
            style_cell={"padding":"9px","fontSize":14,"lineHeight":"22px",
                        "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                        "textAlign":"left"},
            style_data={"borderBottom":"1px solid #eceff4"},
            style_data_conditional=[{"if": {"row_index":"odd"}, "backgroundColor":"#fbfbfd"}],
            row_selectable="single",
            selected_rows=[0],  # preselect first row so comment UI populates immediately
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

# ───────────────────────── Tab 1: Toggle status override visibility ─────────────────────────
@app.callback(
    Output("t1-status-collapse", "is_open"),
    Input("t1-toggle-status", "n_clicks"),
    State("t1-status-collapse", "is_open"),
    prevent_initial_call=True
)
def toggle_status_override(n, is_open):
    return not is_open

# ───────────────────────── Tab 1: On select athlete ─────────────────────────
@app.callback(
    Output("t1-complaint-dd", "options"),
    Output("t1-complaint-dd", "value"),
    Output("t1-comments-table", "data"),
    Output("t1-selected-athlete-label", "children"),
    Output("t1-comment-date", "date"),
    Output("t1-status-override", "value"),
    Input("t1-athlete-table", "selected_rows"),
    State("t1-rows-json", "data"),
)
def t1_on_select(selected_rows, rows_json):
    if not rows_json:
        raise PreventUpdate

    today = date.today().strftime("%Y-%m-%d")

    if not selected_rows:
        return [], None, [], "", today, None

    row = rows_json[selected_rows[0]]
    cid = int(row["_cid"])
    label = row["_athlete_label"]

    complaints = td.fetch_customer_complaints(cid)
    names = [c["Title"] for c in complaints if c.get("Title")]
    opts = [{"label": n, "value": n} for n in sorted(set(names))]
    val = opts[0]["value"] if opts else None

    comments = _db_list_comments_with_ids([cid])
    expanded = [_expand_comment_record(rec, label, cid) for rec in comments]

    return opts, val, expanded, f" — {label}", today, None

# ───────────────────────── Tab 1: Save comment ─────────────────────────
@app.callback(
    Output("t1-comments-table", "data", allow_duplicate=True),
    Output("t1-comment-text", "value", allow_duplicate=True),
    Output("t1-comment-status", "children", allow_duplicate=True),
    State("t1-athlete-table", "selected_rows"),
    State("t1-rows-json", "data"),
    State("t1-complaint-dd", "value"),
    State("t1-comment-date", "date"),
    State("t1-comment-text", "value"),
    State("t1-status-override", "value"),
    State("t1-comments-table", "data"),
    Input("t1-save-comment", "n_clicks"),
    prevent_initial_call=True,
)
def t1_save_comment(selected_rows, rows_json, complaint, date_str, text, status_override, table_data, _n):
    if not _n or not rows_json or not selected_rows or not date_str or not (text or "").strip():
        raise PreventUpdate

    row = rows_json[selected_rows[0]]
    cid = int(row["_cid"])
    label = row["_athlete_label"]
    author = _get_signed_in_name()

    # Choose status to save/display
    status_to_use = status_override or _current_status_for_customer(cid)

    new_id = _db_add_comment_returning(
        cid, label, date_str, text.strip(),
        complaint=(complaint or ""),
        author=(author or ""),
        status_override=(status_override or "")
    )

    new_row = {
        "_id": new_id,
        "Date": date_str,
        "By": author or "",
        "Athlete": label,
        "Complaint": complaint or "",
        "Status": status_to_use or "",
        "Comment": text.strip(),
    }

    current = table_data or []
    updated = current + [new_row]

    return updated, "", status_pill_component("Comment saved.", "success")

# ───────────────────────── Tab 1: Persist edits/deletes ─────────────────────────
@app.callback(
    Output("t1-comment-status", "children", allow_duplicate=True),
    Input("t1-comments-table", "data_timestamp"),
    State("t1-comments-table", "data"),
    State("t1-comments-table", "data_previous"),
    prevent_initial_call=True,
)
def t1_persist_comment_mutations(_ts, data, data_prev):
    try:
        if data_prev is None:
            raise PreventUpdate

        prev_by_id = {r["_id"]: r for r in data_prev if r.get("_id") is not None}
        now_by_id  = {r["_id"]: r for r in data     if r.get("_id") is not None}

        deleted_ids = [cid for cid in prev_by_id.keys() if cid not in now_by_id]
        for i in deleted_ids:
            _db_delete_comment(i)

        any_edit = False
        for cid, now in now_by_id.items():
            before = prev_by_id.get(cid)
            if not before:
                continue
            # Only Comment is editable; check and persist if changed
            if (before.get("Comment") or "") != (now.get("Comment") or ""):
                _db_update_comment_text(cid, now.get("Comment") or "")
                any_edit = True

        if deleted_ids and any_edit:
            return status_pill_component("Comments updated & deleted.", "success")
        elif deleted_ids:
            return status_pill_component("Comment deleted.", "success")
        elif any_edit:
            return status_pill_component("Comment updated.", "success")
        else:
            raise PreventUpdate
    except Exception as e:
        return status_pill_component(f"Comment persistence error: {e}", "danger")

# ───────────────────────── SQLite helpers (reuse td.DB_PATH) ─────────────────────────
def _db_connect():
    conn = sqlite3.connect(td.DB_PATH, check_same_thread=False)
    # Light migration: add new columns if they don't exist
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(comments)")
        cols = [row[1] for row in cur.fetchall()]
        to_add = []
        if "author" not in cols:
            to_add.append(("author", "TEXT"))
        if "complaint" not in cols:
            to_add.append(("complaint", "TEXT"))
        if "status_override" not in cols:
            to_add.append(("status_override", "TEXT"))
        for name, sqltype in to_add:
            cur.execute(f"ALTER TABLE comments ADD COLUMN {name} {sqltype}")
        conn.commit()
    except Exception:
        # Best effort; ignore if ALTER not supported in some path
        pass
    return conn

def _db_add_comment_returning(customer_id: int, customer_label: str, date_str: str, comment: str,
                              complaint: str = "", author: str = "", status_override: str = "") -> int:
    conn = _db_connect(); cur = conn.cursor()
    # Insert with new optional columns (NULL if not added)
    cur.execute(
        """INSERT INTO comments(customer_id, customer_label, date, comment, complaint, author, status_override, created_at)
           VALUES (?,?,?,?,?,?,?,datetime('now'))""",
        (int(customer_id), customer_label or "", date_str, comment, complaint or None, author or None, status_override or None)
    )
    new_id = cur.lastrowid
    conn.commit(); conn.close()
    return int(new_id)

def _db_list_comments_with_ids(customer_ids):
    conn = _db_connect(); cur = conn.cursor()
    # Decide at runtime which columns exist
    cur.execute("PRAGMA table_info(comments)")
    cols = [row[1] for row in cur.fetchall()]
    has_author = "author" in cols
    has_complaint = "complaint" in cols
    has_status_override = "status_override" in cols

    select_cols = ["id", "date", "comment", "customer_label", "customer_id", "created_at"]
    if has_author: select_cols.append("author")
    if has_complaint: select_cols.append("complaint")
    if has_status_override: select_cols.append("status_override")
    sel = ", ".join(select_cols)

    if customer_ids:
        vals = [int(x) for x in customer_ids]
        q = ",".join("?" for _ in vals)
        cur.execute(f"""
          SELECT {sel}
          FROM comments
          WHERE customer_id IN ({q})
          ORDER BY date ASC, id ASC
        """, vals)
    else:
        cur.execute(f"SELECT {sel} FROM comments ORDER BY date ASC, id ASC")
    rows = cur.fetchall(); conn.close()

    out = []
    for r in rows:
        base = {
            "_id": r[0],
            "Date": r[1],
            "Comment": r[2],
            "Athlete": r[3],
            "_cid": r[4],
            "_created_at": r[5],
        }
        idx = 6
        author = r[idx] if has_author else ""
        idx += 1 if has_author else 0
        complaint = r[idx] if has_complaint else ""
        idx += 1 if has_complaint else 0
        status_override = r[idx] if has_status_override else ""
        # stash extended fields
        base["_author"] = author or ""
        base["_complaint"] = complaint or ""
        base["_status_override"] = status_override or ""
        out.append(base)
    return out

def _db_delete_comment(comment_id: int):
    conn = _db_connect(); cur = conn.cursor()
    cur.execute("DELETE FROM comments WHERE id = ?", (int(comment_id),))
    conn.commit(); conn.close()

def _db_update_comment_text(comment_id: int, new_text: str):
    conn = _db_connect(); cur = conn.cursor()
    cur.execute("UPDATE comments SET comment = ? WHERE id = ?", (new_text, int(comment_id)))
    conn.commit(); conn.close()

def _expand_comment_record(rec, athlete_label, cid: int):
    # Choose status to display for existing row: use saved override if present, else current status
    status = rec.get("_status_override") or _current_status_for_customer(int(cid))
    return {
        "_id": rec["_id"],
        "Date": rec["Date"],
        "By": rec.get("_author", "") or "",
        "Athlete": athlete_label,
        "Complaint": rec.get("_complaint", "") or "",
        "Status": status or "",
        "Comment": rec["Comment"],
    }

# ───────────────────────── Training tab callbacks ─────────────────────────
td.register_callbacks(app)

# ───────────────────────── Main ─────────────────────────
if __name__ == "__main__":
    app.run(debug=False, port=8050)
