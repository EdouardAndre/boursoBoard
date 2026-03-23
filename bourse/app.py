import dash
import dash_bootstrap_components as dbc
from dash import html

import timescaledb_model as tsdb
from etl import store_files

from loguru import logger

external_stylesheets = [dbc.themes.BOOTSTRAP]
app = dash.Dash(
    __name__,
    title="Bourse",
    suppress_callback_exceptions=True,
    external_stylesheets=external_stylesheets,
    assets_ignore="style.css?v=1.0",
)
db = tsdb.TimescaleStockMarketModel("bourse", "bourse", "database", "password")

# TODO: Modify this part for the front-end.
app.layout = dbc.Container([html.H1("Bouse Dashboard")])

if __name__ == "__main__":
    logger.info("Importing data into the database")
    # TODO: implement the function
    store_files("2020-01-01", "2020-02-01", "euronext", db)  # Example of usage
    logger.info("Import done")
    logger.info("Starting dashboard server")
    app.run(debug=True)
