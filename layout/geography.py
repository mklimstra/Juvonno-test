from dash import html, callback, Input, Output, dcc, State
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc
from utils import fetch_options

class GeographyFilters:
    def __init__(self, app, auth, id="placename"):
        self.app = app
        self.auth = auth
        self.id = id
        self.layout = self._render()
        self._register_callbacks()

    def _render(self):
        return dbc.Container([

            dbc.Label("Province"),
            dcc.Dropdown(
                id=f"{self.id}-province",
                options=[],
                multi=False,
                className="mb-3",
            ),

            dbc.Label("Location"),
            dcc.Dropdown(
                id=f"{self.id}-location",
                options=[],
                multi=False,
                className="mb-3",
            ),

            dbc.Label("Town/City"),
            dcc.Dropdown(
                id=f"{self.id}-placename",
                options=[],
                multi=False,
                className="mb-3",
            ),

        ])

    def _register_callbacks(self):
        @self.app.callback(
            Output(f"{self.id}-province", "options"),
            Input("init-interval", "n_intervals")
        )
        def initial_values(n_intervals):
            try:
                token = self.auth.get_token()

                province_options = fetch_options("/api/registration/geography/provinces/",
                                                 token, "name", "id")


                return province_options
            except Exception as e:
                raise PreventUpdate

        # @self.app.callback(
        #     Output(f"{self.id}-location", "options"),
        #     Input(f"{self.id}-province", "value")
        # )
        # def load_locations(province):
        #     if not province:
        #         raise PreventUpdate
        #
        #     try:
        #         token = self.auth.get_token()
        #
        #         params = {
        #             'province_territory':province
        #         }
        #         location_options = fetch_options(f"/api/registration/geography/locations/",
        #                                          params=params, token=token, label_key="name", value_key="id")
        #
        #         return location_options
        #     except Exception as e:
        #         print(e)
        #         raise PreventUpdate

        @self.app.callback(
            Output(f"{self.id}-location", "options"),
            Output(f"{self.id}-placename", "options"),
            Input(f"{self.id}-province", "value"),
            Input(f"{self.id}-location", "value")
        )
        def load_places(province, location):
            try:
                token = self.auth.get_token()

                params = {}

                print("CITIES")

                if location: params['location']= location
                if province: params['province_territory']= province

                if province:
                    location_options = fetch_options(f"/api/registration/geography/locations/",
                                                     params={'province_territory':province}, token=token, label_key="name", value_key="id")
                else:
                    location_options = []

                if location:
                    city_options = fetch_options(f"/api/registration/geography/",
                                                 params=params, token=token, label_key="name",
                                                 value_key="id",
                                                 limit=5000)
                else:
                    city_options = []


                return location_options, city_options
            except Exception as e:
                print(e)
                raise PreventUpdate