from dash_auth_external import DashAuthExternal
import pandas as pd
from dash.exceptions import PreventUpdate
from dash import Dash, Input, Output, html, dcc, no_update, dash_table, State, callback_context
import dash_bootstrap_components as dbc
import requests
import math

from settings import *

# creating the instance of our auth class
auth = DashAuthExternal(AUTH_URL,
                        TOKEN_URL,
                        app_url=APP_URL,
                        client_id=CLIENT_ID,
                        client_secret=CLIENT_SECRET)
server = (
    auth.server
)  # retrieving the flask server which has our redirect rules assigned


# Initialize the Dash app
app = Dash(__name__,
           server=server,
           external_stylesheets=[dbc.themes.BOOTSTRAP])

def fetch_options(path, token, label_key, value_key, limit=1000):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{SITE_URL}{path}", params={"limit": limit}, headers=headers, timeout=5)
    resp.raise_for_status()
    items = resp.json()["results"]
    return [{"label": item[label_key], "value": item[value_key]} for item in items]



# Offcanvas for filters
offcanvas = dbc.Offcanvas(
    [
        dbc.Label("Role"),
        dcc.Dropdown(
            id="filter-role",
            options=[],
            multi=False,
            className="mb-3",
        ),

        dbc.Label("Campus"),
        dcc.Dropdown(
            id="filter-campus",
            options=[],
            multi=False,
            className="mb-3",
        ),

        dbc.Label("Organization"),
        dcc.Dropdown(
            id="filter-organization",
            options=[],
            multi=False,
            className="mb-3",
        ),

        dbc.Button("Apply", id="apply-filters", color="primary"),
    ],
    id="offcanvas-filters",
    title="Filters",
    is_open=False,
    placement="start",
)

# Toggle button for offcanvas
toggle_button = dbc.Button("Filters", id="open-offcanvas", n_clicks=0, color="secondary", className="mb-3")

# Data table stub for results
results_table = dash_table.DataTable(
    id="results-table",
    columns=[
        {"name": "ID", "id": "id"},
        {"name": "First Name", "id": "first_name"},
        {"name": "Last Name", "id": "last_name"},
        {"name": "Sport", "id": "sport"},
        {"name": "Email", "id": "email"},
        {"name": "Enrollment", "id": "enrollment_status"}
    ],
    page_current=0,
    page_size=20,
    page_action="none",

    style_table={"overflowX": "auto"},
    style_cell={"textAlign": "left"},
)

pagination = dbc.Pagination(
    id="pagination",
    max_value=1,
    active_page=1,
    fully_expanded=False,
    previous_next=True,
)

download_button = html.Div([
        html.Button("Download CSV", id="download-csv-btn", n_clicks=0),
        dcc.Download(id="download-csv")
    ], className="mt-3")

# App layout
app.layout = html.Div([
    dcc.Location(id="redirect-to", refresh=True),
    dcc.Interval(
        id="init-interval",
        interval=500,  # e.g., 1 second after page load
        n_intervals=0,
        max_intervals=1   # This ensures it fires only once
    ),

    dbc.Container(
    [
        toggle_button,
        offcanvas,
        results_table,
        html.Div(pagination, className="mt-2"),
        download_button,
    ],
    fluid=True,
)])

# Callback to toggle offcanvas
@app.callback(
    Output("offcanvas-filters", "is_open"),
    Input("open-offcanvas", "n_clicks"),
    State("offcanvas-filters", "is_open"),
)
def toggle_offcanvas(n_clicks, is_open):
    if n_clicks:
        return not is_open
    return is_open


@app.callback(
    Output("redirect-to", "href"),
    Output("filter-role", "options"),
    Output("filter-campus", "options"),
    Output("filter-organization", "options"),
    Input("init-interval", "n_intervals")
)
def initial_view(n):
    """
    On timeout, load filters
    """
    try:
        token = auth.get_token()
    except Exception as e:

        ## HERE, HOW DO I FORWARD A REDIRECT TO APP_URL???

        print(e)

        auth_url = f"{AUTH_URL}?response_type=code&client_id={CLIENT_ID}&redirect_uri={APP_URL}"

        print(f"Forward to {APP_URL}")

        return APP_URL, no_update, no_update, no_update

    # campus: label=name, value=id
    campus_options = fetch_options("/api/registration/campus/", token,"name", "id")

    # sport org: label=name, value=id
    org_options = fetch_options("/api/registration/organization/", token, "name", "id")

    # role: label=verbose_name, value=id
    role_options = fetch_options("/api/registration/role/", token, "verbose_name", "id")

    return no_update, role_options, campus_options, org_options

def restructure_profile(profile):
    record = {
        'id': profile['id'],
     'first_name': profile['person']['first_name'] if profile['person'] else None,
     'last_name': profile['person']['last_name'] if profile['person'] else None,
     'email': profile['person']['email'] if profile['person'] else None,
     'sport': profile['sport']['name'] if profile['sport'] else None,
     'enrollment_status': profile['current_enrollment']['enrollment_status'] if profile['current_enrollment'] else None
     }

    return record

def fetch_all_profiles(filters):
    try:
        token = auth.get_token()
    except Exception as e:
        raise PreventUpdate

    headers = {"Authorization": f"Bearer {token}"}
    url    = f"{SITE_URL}/api/registration/profile/"
    params = {**filters, "limit": 100, "offset": 0}  # choose a reasonable chunk size
    all_records = []

    while url:
        r = requests.get(url, headers=headers, params=params)
        r.raise_for_status()
        payload = r.json()
        all_records.extend(payload["results"])

        # Move to the next page
        url = payload.get("next")
        # Once we switch to using `next`, we no longer need `params`
        params = None

    return all_records


@app.callback(
    Output("download-csv", "data"),
    Input("download-csv-btn", "n_clicks"),
    State("filter-role",         "value"),
    State("filter-campus",       "value"),
    State("filter-organization", "value"),
    prevent_initial_call=True,
)
def download_csv(n_clicks, role, campus, org):
    # build the common filter params
    filters = {}
    if role:   filters["role_id"]      = role
    if campus: filters["campus_id"]    = campus
    if org:    filters["sport_org_id"] = org

    records = [restructure_profile(p) for p in fetch_all_profiles(filters)]
    df = pd.DataFrame(records)
    return dcc.send_data_frame(df.to_csv, "filtered_profiles.csv", index=False)


@app.callback(
    Output("results-table", "data"),
    Output("pagination", "max_value"),
    Output("pagination", "active_page"),
    Input("apply-filters", "n_clicks"),
    Input("pagination", "active_page"),
    Input("results-table",      "page_size"),
    State("filter-role", "value"),
    State("filter-campus", "value"),
    State("filter-organization", "value"),
)
def apply_filters(n_clicks, active_page, page_size, role_id, campus_id, org_id):
    try:
        token = auth.get_token()
    except Exception as e:
        raise PreventUpdate
    if not n_clicks:
        raise PreventUpdate

    ctx = callback_context
    if not ctx.triggered:
        raise PreventUpdate

    trigger_id = ctx.triggered[0]['prop_id'].split('.')[0]
    # Reset to first page on applying filters
    page = 1 if trigger_id == "apply-filters" else (active_page or 1)

    headers = {"Authorization": f"Bearer {token}"}
    # build only the params the user set
    params = {}
    params["role_id"] = role_id if role_id else None
    params["campus_id"] = campus_id if campus_id else None
    params["sport_org_id"] = org_id if org_id else None

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    # fetch the filtered profiles list
    resp = requests.get(f"{SITE_URL}/api/registration/profile/", headers=headers, params=params, timeout=5)
    resp.raise_for_status()
    payload = resp.json()

    total = payload.get("count", 0)
    total_pages = math.ceil(total / page_size) if total else 1

    # Return raw JSON as a string
    people_res = [restructure_profile(p) for p in payload.get("results", [])]

    return people_res, total_pages, page

if __name__ == "__main__":
    app.run(debug=True, port=8050)