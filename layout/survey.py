from dataclasses import dataclass
from datetime import date, datetime
from dash import html, dcc, Input, Output, State
import dash_bootstrap_components as dbc


def _normalize_wellness_form(date_val, mood, fatigue, sleep_hours, notes):
    """Coerce/clean values for warehouse ingestion."""
    iso_date = None
    if date_val:
        try:
            iso_date = datetime.fromisoformat(str(date_val)).date().isoformat()
        except Exception:
            iso_date = str(date_val)

    return {
        "participant_id": "0",
        "date": iso_date,  # "YYYY-MM-DD"
        "mood": int(mood) if mood is not None else None,
        "fatigue": int(fatigue) if fatigue is not None else None,
        "sleep_hours": float(sleep_hours) if sleep_hours is not None else None,
        "notes": (notes or "").strip(),
    }


@dataclass
class WellnessSurveyForm:
    """
    Bootstrap card that renders a wellness survey with stable IDs for callbacks.
    IDs follow: {id_prefix}-date, -mood, -fatigue, -sleep_hours, -notes, -submit
    """
    id_prefix: str = "survey"

    def _id(self, suffix: str) -> str:
        return f"{self.id_prefix}-{suffix}"

    @property
    def store_id(self) -> str:
        return self._id("form-store")

    def render(self):
        return html.Div(
            [
                # Self-contained store for this survey instance
                dcc.Store(id=self.store_id, data={}),
                dbc.Card(
                    [
                        dbc.CardHeader(html.H5("Daily Wellness Survey", className="mb-0")),
                        dbc.CardBody(
                            [
                                # Date
                                dbc.Row(
                                    dbc.Col(
                                        [
                                            dbc.Label("Survey date"),
                                            dcc.DatePickerSingle(
                                                id=self._id("date"),
                                                display_format="YYYY-MM-DD",
                                                date=date.today(),
                                            ),
                                        ],
                                        md=6,
                                    ),
                                    className="mb-3",
                                ),

                                # Mood
                                dbc.Row(
                                    dbc.Col(
                                        [
                                            dbc.Label("Mood (1–5: 1=very poor, 5=very good)"),
                                            dcc.Slider(
                                                id=self._id("mood"),
                                                min=1,
                                                max=5,
                                                step=1,
                                                value=None,
                                                marks={i: str(i) for i in range(1, 6)},
                                            ),
                                        ]
                                    ),
                                    className="mb-4",
                                ),

                                # Fatigue
                                dbc.Row(
                                    dbc.Col(
                                        [
                                            dbc.Label("Fatigue (0–10: 0=none, 10=extreme)"),
                                            dcc.Slider(
                                                id=self._id("fatigue"),
                                                min=0,
                                                max=10,
                                                step=1,
                                                value=None,
                                                marks={i: str(i) for i in range(0, 11)},
                                            ),
                                        ]
                                    ),
                                    className="mb-4",
                                ),

                                # Sleep hours
                                dbc.Row(
                                    dbc.Col(
                                        [
                                            dbc.Label("Sleep hours (0–24)"),
                                            dbc.Input(
                                                id=self._id("sleep_hours"),
                                                type="number",
                                                min=0,
                                                max=24,
                                                step=0.25,
                                                value=None,
                                            ),
                                        ],
                                        md=6,
                                    ),
                                    className="mb-3",
                                ),

                                # Notes
                                dbc.Row(
                                    dbc.Col(
                                        [
                                            dbc.Label("Notes (optional, max 500 chars)"),
                                            dbc.Textarea(
                                                id=self._id("notes"),
                                                rows=4,
                                                maxLength=500,
                                                style={"resize": "vertical"},
                                            ),
                                        ]
                                    ),
                                    className="mb-3",
                                ),

                                # Submit button + placeholder for feedback
                                dbc.Row(
                                    [
                                        dbc.Col(
                                            dbc.Button(
                                                "Submit",
                                                id=self._id("submit"),
                                                n_clicks=0,
                                                color="primary",
                                            ),
                                            width="auto",
                                        ),
                                        dbc.Col(
                                            html.Div(id=self._id("feedback"), className="text-muted"),
                                        )
                                    ],
                                    className="g-2 align-items-center",
                                ),
                            ],
                            className="pt-3",
                        ),
                    ],
                    className="shadow-sm border-0",
                )
            ])

    def register_callbacks(self, app):
        """
        Registers the self-contained collector callback that keeps this instance's
        Store in sync whenever any form input changes.
        """

        @app.callback(
            Output(self.store_id, "data"),
            Input(self._id("date"), "date"),
            Input(self._id("mood"), "value"),
            Input(self._id("fatigue"), "value"),
            Input(self._id("sleep_hours"), "value"),
            Input(self._id("notes"), "value"),
        )
        def _collect(date_val, mood, fatigue, sleep_hours, notes):
            values = _normalize_wellness_form(date_val, mood, fatigue, sleep_hours, notes)

            return values
