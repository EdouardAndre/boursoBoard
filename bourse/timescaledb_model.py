import time
import io
import csv
import psycopg2
import pandas as pd
import sqlalchemy
from loguru import logger

# Pour la table Markets, utilisé aussi dans le constructeur
# mid, nom, alias, prefix boursorama, symbol SWS
initial_markets_data = (
    (1, "New York", "nyse", "", "NYSE", ""),
    (2, "London Stock Exchange", "lse", "1u*.L", "LSE", ""),
    (3, "Bourse de Milan", "milano", "1g", "", ""),
    (4, "Mercados Espanoles", "mercados", "FF55-", "", ""),
    (5, "Amsterdam", "amsterdam", "1rA", "", "Amsterdam"),
    (6, "Paris", "paris", "1rP", "ENXTPA", "Paris"),
    (7, "Deutsche Borse", "xetra", "1z", "", ""),
    (8, "Bruxelle", "bruxelle", "FF11_", "", "Brussels"),
    (9, "Australie", "asx", "", "ASX", ""),
    (100, "International", "int", "", "", ""),  # should be last one
)


def _psql_insert_copy(table, conn, keys, data_iter):  # mehod used by df_write
    """
    Execute SQL statement inserting data
    https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.to_sql.html

    Parameters
    ----------
    table : pandas.io.sql.SQLTable
    conn : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection
    keys : list of str
        Column names
    data_iter : Iterable that iterates the values to be inserted
    """
    # gets a DBAPI connection that can provide a cursor
    dbapi_conn = conn.connection
    with dbapi_conn.cursor() as cur:
        s_buf = io.StringIO()
        writer = csv.writer(s_buf)
        writer.writerows(data_iter)
        s_buf.seek(0)

        columns = ", ".join('"{}"'.format(k) for k in keys)
        if table.schema:
            table_name = "{}.{}".format(table.schema, table.name)
        else:
            table_name = table.name

        sql = "COPY {} ({}) FROM STDIN WITH CSV".format(table_name, columns)
        cur.copy_expert(sql=sql, file=s_buf)


class TimescaleStockMarketModel:
    """Bourse model with TimeScaleDB persistence."""

    def __init__(
        self, database, user=None, host=None, password=None, port=None, remove_all=False
    ):
        """Create a TimescaleStockMarketModel

        database -- The name of the persistence database.
        user     -- Username to connect with to the database. Same as the
                    database name by default.
        remove_all -- REMOVE ALL DATA from the database
        """
        self.__database = database
        self.__user = user or database
        self.__host = host or "localhost"
        self.__port = port or 5432
        self.__password = password or ""
        self.__squash = False
        self.__engine = sqlalchemy.create_engine(
            f"timescaledb://{self.__user}:{self.__password}@{self.__host}:{self.__port}/{self.__database}"
        )
        # markets
        self.market_id = {
            a: i + 1 for i, a in enumerate([m[2] for m in initial_markets_data])
        }
        self.market_id2sws = {
            i + 1: w for i, w in enumerate([m[4] for m in initial_markets_data])
        }
        for i, w in self.market_id2sws.items():
            if w == "":
                self.market_id2sws[i] = None

        self.logger = logger
        self.connection = self._connect_to_database()

        self.logger.info(
            "Setup database generates an error if it exists already, it's ok"
        )
        if remove_all:
            self._purge_database()
        self._setup_database()

    def _connect_to_database(self, retry_limit=5, retry_delay=1):
        """
        With a SQL server running in a Docker, it can take time to connect if all
        services are started in the same time.
        """
        for _ in range(retry_limit):
            try:
                connection = psycopg2.connect(
                    database=self.__database,
                    user=self.__user,
                    host=self.__host,
                    password=self.__password,
                )
                return connection
            except Exception as e:
                print(f"Connection attempt failed: {e}")
                time.sleep(retry_delay)
        raise Exception("Failed to connect to database after multiple attempts")

    def _create_sequence(self, sequence_name, commit=False):
        """Create a sequence in the database."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"CREATE SEQUENCE {sequence_name};")
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error creating sequence: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _drop_sequence(self, sequence_name, commit=False):
        """Drop a sequence from the database."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"DROP SEQUENCE IF EXISTS {sequence_name};")
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error dropping sequence: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _create_table(self, table_name, columns_definition, commit=False):
        """Create a table in the database."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"CREATE TABLE {table_name} ({columns_definition});")
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error creating table: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _drop_table(self, table_name, commit=False):
        """Drop a table from the database."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE;")
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error dropping table: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _create_hypertable(self, table_name, time_column, commit=False):
        """Create a hypertable in the database."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                f"SELECT create_hypertable('{table_name}', '{time_column}');"
            )
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error creating hypertable: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _drop_hypertable(self, table_name, commit=False):
        """Drop a hypertable from the database."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"SELECT drop_hypertable('{table_name}');")
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error dropping hypertable: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _create_index(self, table_name, index_name, columns, commit=False):
        """Create an index in the database."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"CREATE INDEX {index_name} ON {table_name} ({columns});")
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error creating index: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _drop_index(self, index_name, commit=False):
        """Drop an index from the database."""
        cursor = self.connection.cursor()
        try:
            cursor.execute(f"DROP INDEX IF EXISTS {index_name};")
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error dropping index: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _insert_data(self, table_name, data, commit=False):
        """Insert data into a table in the database."""
        cursor = self.connection.cursor()
        try:
            for row in data:
                cursor.execute(f"INSERT INTO {table_name} VALUES %s;", (row,))
            if commit:
                self.connection.commit()
        except Exception as e:
            print(f"Error inserting data: {e}")
            self.connection.rollback()  # Rollback the current transaction

    def _setup_database(self):
        """Setup the database schema."""
        print("setup des tables de la base")
        try:
            if len(self.df_query("select id from markets")) == 0:
                print("Création des tables de la base")
                # Create sequences
                self._create_sequence("market_id_seq")
                self._create_sequence("company_id_seq")

                # Create tables
                # boursorama : exchange prefix for boursorama
                # sws : exchange name for Simply Wall Street
                self._create_table(
                    "markets",
                    """ id SMALLINT PRIMARY KEY DEFAULT nextval('market_id_seq'), 
                        name VARCHAR, 
                        alias VARCHAR,
                        boursorama VARCHAR,
                        sws VARCHAR,
                        euronext VARCHAR
                    """,
                )
                self._create_table(
                    "companies",
                    """ id SMALLINT PRIMARY KEY DEFAULT nextval('company_id_seq'), 
                        name VARCHAR,
                        mid SMALLINT DEFAULT 0,
                        symbol VARCHAR, 
                        isin CHAR(12),
                        boursorama VARCHAR, 
                        euronext VARCHAR, 
                        pea BOOLEAN DEFAULT FALSE, 
                        sector1 VARCHAR,
                        sector2 VARCHAR,
                        sector3 VARCHAR
                    """,
                )
                self._create_table(
                    "stocks",
                    """ date TIMESTAMPTZ, 
                        cid SMALLINT, 
                        value FLOAT4, 
                        volume FLOAT4
                    """,
                )
                self._create_table(
                    "daystocks",
                    """ date TIMESTAMPTZ, 
                        cid SMALLINT, 
                        open FLOAT4,
                        close FLOAT4, 
                        high FLOAT4, 
                        low FLOAT4, 
                        volume FLOAT4, 
                        mean FLOAT4, 
                        std FLOAT4
                    """,
                )
                self._create_table("file_done", "name VARCHAR PRIMARY KEY")
                self._create_table("tags", "name VARCHAR PRIMARY KEY, value VARCHAR")
                self._create_table("error_dates", "date TIMESTAMPTZ")

                # Create hypertables
                self._create_hypertable("stocks", "date")
                self._create_hypertable("daystocks", "date")

                # Create indexes
                self._create_index("stocks", "idx_cid_stocks", "cid, date DESC")
                self._create_index("daystocks", "idx_cid_daystocks", "cid, date DESC")

                # Insert initial market data
                self._insert_data("markets", initial_markets_data)
                self.connection.commit()
        except Exception as e:
            self.logger.error("SQL error: %s" % e)
            self.connection.rollback()

    def _purge_database(self):
        self._drop_table("markets")
        self._drop_table("companies")
        self._drop_table("stocks")
        self._drop_table("daystocks")
        self._drop_table("file_done")
        self._drop_table("tags")
        self._drop_table("error_dates")

        self._drop_sequence("market_id_seq")
        self._drop_sequence("company_id_seq")

        self._drop_index("stocks")
        self._drop_index("daystocks")
        self.commit()

    # ------------------------------ public methods --------------------------------

    def execute(self, query, args=None, cursor=None, commit=False):
        """Send a Postgres SQL command. No return"""
        if args is None:
            pretty = query
        else:
            pretty = "%s %% %r" % (query, args)
        self.logger.debug("SQL: QUERY: %s" % pretty)
        if cursor is None:
            cursor = self.connection.cursor()
        try:
            cursor.execute(query, args)
            if commit:
                self.commit()
            return cursor.fetchall()
        except Exception as e:
            self.logger.error(f"Exception with execute: {e}")
            if self.connection:
                self.connection.rollback()

    def df_write(
        self,
        df,
        table,
        args=None,
        commit=False,
        if_exists="append",
        index=False,
        index_label=None,
        chunksize=100,
        dtype=None,
        method=_psql_insert_copy,
    ):
        """Write a Pandas dataframe to the Postgres SQL database

        :param query:
        :param args: arguments for the query
        :param commit: do a commit after writing
        :param other args: see https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.to_sql.html
        """
        self.logger.debug("df_write")
        df.to_sql(
            table,
            con=self.__engine,
            if_exists=if_exists,
            index=index,
            index_label=index_label,
            chunksize=chunksize,
            dtype=dtype,
            method=method,
        )
        if commit:
            self.commit()

    # general query methods

    def raw_query(self, query, args=None, cursor=None):
        """Return a tuple from a Postgres SQL query"""
        if args is None:
            pretty = query
        else:
            pretty = "%s %% %r" % (query, args)
        self.logger.debug("SQL: QUERY: %s" % pretty)
        if cursor is None:
            cursor = self.connection.cursor()
        try:
            cursor.execute(query, args)
            query = query.strip().upper()
            if query.startswith("SELECT"):
                return cursor.fetchall()
        except Exception as e:
            self.logger.error(f"Exception with raw_query: {e}")
            if self.connection:
                self.connection.rollback()

    def df_query(
        self,
        query,
        args=None,
        index_col=None,
        coerce_float=True,
        params=None,
        parse_dates=None,
        columns=None,
        chunksize=None,
        dtype=None,
    ):
        """Returns a Pandas dataframe from a Postgres SQL query

        :param query:
        :param args: arguments for the query
        :param index_col: index column of the DataFrame
        :param other args: see https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.read_sql.html
        :return: a dataframe
        """
        if args is not None:
            query = query % args
        self.logger.debug("df_query: %s" % query)
        try:
            res = pd.read_sql(
                query,
                self.__engine,
                index_col=index_col,
                coerce_float=coerce_float,
                params=params,
                parse_dates=parse_dates,
                columns=columns,
                chunksize=chunksize,
                dtype=dtype,
            )
        except Exception as e:
            self.logger.error(e)
            res = pd.DataFrame()
        return res

    # system methods

    def commit(self):
        if not self.__squash:
            self.connection.commit()

    # getters

    # setters

    # bool queries
