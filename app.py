# app.py
import os
import math
import json
import requests
import pandas as pd
import dash
import traceback

from dash_auth_external import DashAuthExternal
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update, dash_table, State, callback_context
import dash_bootstrap_components as dbc

# Repo components & settings (same import style as the registration viewer)
from layout import Footer, Navbar
from settings import *  # expects AUTH_URL, TOKEN_URL, APP_URL, SITE_URL, CLIENT_ID, CLIENT_SECRET

# Reuse all API/data utilities from the working Training Dashboard
import training_dashboard as td  # uses JUV_API_KEY env var, fetches CUSTOMERS, GROUP_OPTS, etc.


# ------------------------- Auth / Server -------------------------
auth = DashAuthExternal(
    AUTH_URL,
    TOKEN_URL,
    app_url=APP_URL,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)
server = auth.server

# Serve assets folder like the repo
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


# ------------------------- Small HTML helpers (pills/dots) -------------------------
PILL_BG = "#eef2f7"

def pill(text: str, bg=PILL_BG, fg="#111", border="#e3e6eb"):
    return (
        f'<span style="display:inline-block;padding:2px 8px;'
        f'border-radius:999px;background:{bg};color:{fg};'
        f'border:1px solid {border};font-size:12px;'
        f'line-height:18px;white-space:nowrap;">{td.html.escape(text)}</span>'
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
                options=td.GROUP_OPTS,  # reuse computed groups from training_dashboard
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
            dbc.CardHeader("Comments"),
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
                        {"name":"Date","id":"Date"},
                        {"name":"By","id":"By"},
                        {"name":"Athlete","id":"Athlete"},
                        {"name":"Complaint","id":"Complaint"},
                        {"name":"Status","id":"Status"},
                        {"name":"Comment","id":"Comment"},
                    ],
                    data=[],
                    page_size=10,
                    style_header={"fontWeight":"600","backgroundColor":"#f8f9fa"},
                    style_cell={
                        "padding":"8px","fontSize":13,
                        "fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
                        "textAlign":"left"
                    },
                    style_data={
                        "whiteSpace":"normal","height":"auto","borderBottom":"1px solid #eee"
                    },
                ),
            ])
        ], className="mb-4"),

    ], fluid=True)


# ------------------------- Tab 2 (Training Dashboard) Layout -------------------------
def tab2_layout():
    # Use the same layout/callbacks you already have working
    return td.layout_body()


# ------------------------- Page Layout -------------------------
app.layout = html.Div([
    # repo boot sequence
    dcc.Location(id="redirect-to", refresh=True),

    # one-time init for login / redirect
    dcc.Interval(id="init-interval", interval=500, n_intervals=0, max_intervals=1),

    # user badge refresh (signed-in name) every 60s
    dcc.Interval(id="user-refresh", interval=60_000, n_intervals=0),

    # Navbar with right-slot for user label (same component as repo)
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
    if which == "tab-1":
        return tab1_layout()
    else:
        return tab2_layout()


# ------------------------- Auth / Navbar user -------------------------
@app.callback(
    Output("redirect-to", "href"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    """Redirect to /login if we don't have a token yet (same behavior as repo)."""
    try:
        token = auth.get_token()
    except Exception:
        token = None

    if not token:
        return "login"  # relative path works on Posit Connect under /content/<id>
    return no_update


@app.callback(
    Output("navbar-user", "children"),
    Input("user-refresh", "n_intervals"),
)
def refresh_user_badge(_n):
    """Show 'Signed in as: First Last' once token is present."""
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


# ------------------------- Tab 1 Callbacks -------------------------
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

        # Normalize targets (use the same lowercasing as training_dashboard)
        targets = {td._norm(g) for g in group_values}

        # Build rows from the same data that tab 2 uses
        rows = []
        for cid, cust in td.CUSTOMERS.items():
            cust_groups = set(td.CID_TO_GROUPS.get(cid, []))
            if not (targets & cust_groups):
                continue

            name = f"{cust.get('first_name','')} {cust.get('last_name','')}".strip()
            groups_html = " ".join(
                pill(g.title(), PILL_BG) for g in sorted(cust_groups)
            ) if cust_groups else "—"

            # Current status (forward-fill from appointments)
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

            # Complaints (from merged sources)
            complaints = td.fetch_customer_complaints(cid)
            complaint_names = [c["Title"] for c in complaints if c.get("Title")]
            complaints_html = " ".join(
                pill(t, "#e7f0ff", border="#cfe0ff") for t in complaint_names
            ) if complaint_names else "—"

            rows.append({
                "Athlete": name,
                "Groups": groups_html,
                "Current Status": status_html,
                "Complaints": complaints_html,
                "DOB": cust.get("dob") or cust.get("birthdate") or "—",
                "Sex": cust.get("sex") or cust.get("gender") or "—",
                "_cid": cid,
            })

        if not rows:
            return html.Div("No athletes in those groups."), [], "", False

        # DataTable with filtering/sorting enabled; render pills/dots via dangerously_allow_html
        columns = [
            {"name":"Athlete", "id":"Athlete"},
            {"name":"Groups", "id":"Groups"},
            {"name":"Current Status", "id":"Current Status"},
            {"name":"Complaints", "id":"Complaints"},
            {"name":"DOB", "id":"DOB"},
            {"name":"Sex", "id":"Sex"},
        ]
        table = dash_table.DataTable(
            id="t1-athlete-table",
            data=rows,
            columns=columns,
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
            dangerously_allow_html=True,
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


# When a row is selected, populate the complaint dropdown and comments table
@app.callback(
    Output("t1-complaint-dd", "options"),
    Output("t1-complaint-dd", "value"),
    Output("t1-comments-table", "data"),
    Output("t1-comment-hint", "children"),
    Input("t1-athlete-table", "selected_rows"),
    State("t1-rows-json", "data"),
)
def t1_on_select(selected_rows, rows_json):
    if not rows_json:
        raise PreventUpdate
    if not selected_rows:
        return [], None, [], "Select an athlete above; comments will filter to that athlete."

    row = rows_json[selected_rows[0]]
    cid = int(row["_cid"])

    # complaint options
    complaints = td.fetch_customer_complaints(cid)
    names = [c["Title"] for c in complaints if c.get("Title")]
    opts = [{"label": n, "value": n} for n in sorted(set(names))]
    val = opts[0]["value"] if opts else None

    # comments table for this athlete (from SQLite)
    comments_raw = td.db_list_comments([cid])
    # expand to include complaint/status columns if possible (leave blank otherwise)
    expanded = []
    for c in comments_raw:
        expanded.append({
            "Date": c["Date"],
            "By": "",  # we’ll fill in on save with user name
            "Athlete": row["Athlete"],
            "Complaint": "",  # we’ll fill on save if provided
            "Status": "",     # optional to fill on save
            "Comment": c["Comment"],
        })

    return opts, val, expanded, f"Adding comment for: {row['Athlete']}"


# Save a Tab 1 comment and refresh the table
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
    if not _n:
        raise PreventUpdate
    if not rows_json or not selected_rows:
        raise PreventUpdate
    if not date_str or not (text or "").strip():
        raise PreventUpdate

    row = rows_json[selected_rows[0]]
    cid = int(row["_cid"])
    label = row["Athlete"]

    # Persist to SQLite (same DB used by tab 2)
    td.db_add_comment(cid, label, date_str, text.strip())

    # Rebuild comments display for this athlete (include author name if available)
    by_who = ""
    try:
        token = auth.get_token()
        if token:
            headers = {"Authorization": f"Bearer {token}"}
            r = requests.get(f"{SITE_URL}/api/csiauth/me/", headers=headers, timeout=5)
            if r.status_code == 200:
                js = r.json()
                by_who = " ".join([js.get("first_name","").strip(), js.get("last_name","").strip()]).strip() or js.get("email","")
    except Exception:
        pass

    comments_raw = td.db_list_comments([cid])
    expanded = []
    for c in comments_raw:
        expanded.append({
            "Date": c["Date"],
            "By": by_who if by_who else "",
            "Athlete": label,
            "Complaint": complaint or "",
            "Status": "",  # optional: derive current status if you want
            "Comment": c["Comment"],
        })

    return expanded


# ------------------------- Register Tab 2 callbacks -------------------------
td.register_callbacks(app)


# ------------------------- Main -------------------------
if __name__ == "__main__":
    # Local run; on Connect the launcher takes over
    app.run(debug=False, port=8050)
