import os
import pickle
import re

import pandas as pd

import timescaledb_model as tsdb

TSDB = tsdb.TimescaleStockMarketModel
DATADIR = "/mnt/data/"


def _parse_bourso_file(filepath: str):
    filename = os.path.basename(filepath)
    match = re.match(r"comp[AB]\s+(.+)\.bz2$", filename)
    if not match:
        return None

    dt_str = match.group(1)
    try:
        dt = pd.to_datetime(dt_str)
    except Exception:
        return None

    try:
        with open(filepath, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return None

    if df.empty:
        return None

    df = df.reset_index(drop=True)
    df["last"] = pd.to_numeric(df["last"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["datetime"] = dt
    return df[["symbol", "name", "last", "volume", "datetime"]]


def _parse_euronext_csv(filepath: str) -> pd.DataFrame:
    df = pd.read_csv(filepath, sep="\t", header=0, skiprows=[1, 2, 3])
    df = df.rename(
        columns={
            "Name": "name", "ISIN": "isin", "Symbol": "symbol",
            "Market": "market", "Trading Currency": "currency",
            "Open": "open", "High": "high", "Low": "low", "Last": "last",
            "Last Date/Time": "last_datetime", "Time Zone": "timezone",
            "Volume": "volume", "Turnover": "turnover",
        }
    )
    return df


def _parse_euronext_xlsx(filepath: str) -> pd.DataFrame:
    df = pd.read_excel(filepath, skiprows=[1, 2, 3])
    df = df.rename(
        columns={
            "Name": "name", "ISIN": "isin", "Symbol": "symbol",
            "Market": "market", "Currency": "currency",
            "Open Price": "open", "High Price": "high", "low Price": "low",
            "last Price": "last", "last Trade MIC Time": "last_datetime",
            "Time Zone": "timezone", "Volume": "volume", "Turnover": "turnover",
        }
    )
    return df


def _extract_date_from_euronext_filename(filepath: str) -> pd.Timestamp:
    basename = os.path.basename(filepath)
    match = re.search(r"(\d{4}-\d{2}-\d{2})", basename)
    if match:
        return pd.to_datetime(match.group(1))
    return pd.NaT


def _get_or_create_company(db: TSDB, name: str, symbol: str, isin: str = None, market_alias: str = None) -> int:
    rows = db.raw_query("SELECT id FROM companies WHERE symbol = %s", (symbol,))
    if rows and len(rows) > 0:
        return rows[0][0]

    if isin:
        rows = db.raw_query("SELECT id FROM companies WHERE isin = %s", (isin,))
        if rows and len(rows) > 0:
            return rows[0][0]

    mid = 0
    if market_alias and market_alias in db.market_id:
        mid = db.market_id[market_alias]

    db.raw_query(
        "INSERT INTO companies (name, symbol, isin, mid) VALUES (%s, %s, %s, %s)",
        (name, symbol, isin, mid),
    )
    db.commit()

    rows = db.raw_query("SELECT id FROM companies WHERE symbol = %s", (symbol,))
    if rows and len(rows) > 0:
        return rows[0][0]
    return None


def _is_file_done(db: TSDB, filename: str) -> bool:
    rows = db.raw_query("SELECT name FROM file_done WHERE name = %s", (filename,))
    return rows is not None and len(rows) > 0


def _mark_file_done(db: TSDB, filename: str):
    try:
        db.raw_query("INSERT INTO file_done (name) VALUES (%s)", (filename,))
        db.commit()
    except Exception:
        db.commit()


def _flush_stocks(db: TSDB, df: pd.DataFrame):
    if df.empty:
        return
    out = df[["date", "cid", "value", "volume"]].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    out["value"] = out["value"].astype("float32")
    out["volume"] = out["volume"].astype("float32")
    db.df_write(out, "stocks", commit=True)


def _flush_daystocks(db: TSDB, df: pd.DataFrame):
    if df.empty:
        return
    out = df[["date", "cid", "open", "close", "high", "low", "volume", "mean", "std"]].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    for col in ["open", "close", "high", "low", "volume", "mean", "std"]:
        out[col] = out[col].astype("float32")
    out["cid"] = out["cid"].astype("int16")
    db.df_write(out, "daystocks", commit=True)


def _detect_market_alias_from_bourso_symbol(symbol: str) -> str:
    for mid, name, alias, bourso_prefix, sws, euronext in tsdb.initial_markets_data:
        if bourso_prefix and symbol.startswith(bourso_prefix):
            return alias
    return "paris"


def _detect_market_alias_from_euronext_market(market_str: str) -> str:
    if not market_str or pd.isna(market_str):
        return "paris"
    market_lower = str(market_str).lower()
    if "amsterdam" in market_lower:
        return "amsterdam"
    if "bruxel" in market_lower or "brussel" in market_lower:
        return "bruxelle"
    if "milan" in market_lower:
        return "milano"
    return "paris"


def _store_euronext_files(start: str, end: str, db: TSDB):
    start_dt = pd.to_datetime(start)
    end_dt = pd.to_datetime(end)
    euronext_dir = os.path.join(DATADIR, "euronext")

    if not os.path.isdir(euronext_dir):
        return

    all_files = sorted(
        os.path.join(euronext_dir, f)
        for f in os.listdir(euronext_dir)
        if f.endswith(".csv") or f.endswith(".xlsx")
    )
    company_cache = {}
    stocks_rows = []
    daystocks_rows = []

    for fpath in all_files:
        basename = os.path.basename(fpath)
        file_date = _extract_date_from_euronext_filename(fpath)
        if pd.isna(file_date) or file_date < start_dt or file_date >= end_dt:
            continue

        if _is_file_done(db, basename):
            continue

        try:
            if fpath.endswith(".csv"):
                df = _parse_euronext_csv(fpath)
            else:
                df = _parse_euronext_xlsx(fpath)
        except Exception:
            _mark_file_done(db, basename)
            continue

        if df.empty:
            _mark_file_done(db, basename)
            continue

        for col in ["open", "high", "low", "last", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["last"])
        df = df[df["last"] > 0]

        for _, row in df.iterrows():
            symbol = str(row.get("symbol", ""))
            name = str(row.get("name", ""))
            isin = str(row.get("isin", "")) if pd.notna(row.get("isin")) else None
            market_str = str(row.get("market", "")) if pd.notna(row.get("market")) else ""

            cache_key = isin or symbol
            if cache_key not in company_cache:
                market_alias = _detect_market_alias_from_euronext_market(market_str)
                cid = _get_or_create_company(db, name, symbol, isin=isin, market_alias=market_alias)
                company_cache[cache_key] = cid

            cid = company_cache[cache_key]
            if cid is None:
                continue

            last_val = float(row["last"])
            vol = float(row["volume"]) if pd.notna(row.get("volume")) else 0.0
            open_val = float(row["open"]) if pd.notna(row.get("open")) else last_val
            high_val = float(row["high"]) if pd.notna(row.get("high")) else last_val
            low_val = float(row["low"]) if pd.notna(row.get("low")) else last_val

            stocks_rows.append({"date": file_date, "cid": cid, "value": last_val, "volume": vol})
            daystocks_rows.append({
                "date": file_date, "cid": cid,
                "open": open_val, "close": last_val, "high": high_val, "low": low_val,
                "volume": vol, "mean": (open_val + high_val + low_val + last_val) / 4.0, "std": 0.0,
            })

        _mark_file_done(db, basename)

        if len(stocks_rows) >= 5000:
            _flush_stocks(db, pd.DataFrame(stocks_rows))
            _flush_daystocks(db, pd.DataFrame(daystocks_rows))
            stocks_rows = []
            daystocks_rows = []

    if stocks_rows:
        _flush_stocks(db, pd.DataFrame(stocks_rows))
    if daystocks_rows:
        _flush_daystocks(db, pd.DataFrame(daystocks_rows))


def _store_bourso_files(start: str, end: str, db: TSDB):
    start_dt = pd.to_datetime(start)
    end_dt = pd.to_datetime(end)
    bourso_dir = os.path.join(DATADIR, "bourso")

    if not os.path.isdir(bourso_dir):
        return

    done_key = f"bourso_all_{start}_{end}"
    if _is_file_done(db, done_key):
        return

    files_by_day = {}
    for year_dir in sorted(os.listdir(bourso_dir)):
        year_path = os.path.join(bourso_dir, year_dir)
        if not os.path.isdir(year_path):
            continue
        for f in os.listdir(year_path):
            if not f.endswith(".bz2"):
                continue
            match = re.match(r"comp[AB]\s+(\d{4}-\d{2}-\d{2})\s", f)
            if not match:
                continue
            day_str = match.group(1)
            day_dt = pd.to_datetime(day_str)
            if day_dt < start_dt or day_dt >= end_dt:
                continue
            if day_str not in files_by_day:
                files_by_day[day_str] = []
            files_by_day[day_str].append(os.path.join(year_path, f))

    total_days = len(files_by_day)

    company_cache = {}

    first_day = sorted(files_by_day.keys())[0]
    for fpath in files_by_day[first_day][:2]:
        result = _parse_bourso_file(fpath)
        if result is not None:
            for _, row in result.iterrows():
                symbol = row["symbol"]
                name = row["name"]
                if symbol not in company_cache:
                    market_alias = _detect_market_alias_from_bourso_symbol(symbol)
                    cid = _get_or_create_company(db, name, symbol, market_alias=market_alias)
                    company_cache[symbol] = cid

    days_processed = 0

    for day_str in sorted(files_by_day.keys()):
        day_files = files_by_day[day_str]
        day_dfs = []

        for fpath in day_files:
            result = _parse_bourso_file(fpath)
            if result is not None:
                day_dfs.append(result)

        if not day_dfs:
            continue

        day_df = pd.concat(day_dfs, ignore_index=True)
        day_df = day_df.dropna(subset=["last"])

        new_symbols = set(day_df["symbol"].unique()) - set(company_cache.keys())
        if new_symbols:
            for _, row in day_df[day_df["symbol"].isin(new_symbols)].drop_duplicates("symbol").iterrows():
                symbol = row["symbol"]
                if symbol not in company_cache:
                    market_alias = _detect_market_alias_from_bourso_symbol(symbol)
                    cid = _get_or_create_company(db, row["name"], symbol, market_alias=market_alias)
                    company_cache[symbol] = cid

        day_df["cid"] = day_df["symbol"].map(company_cache)
        day_df = day_df.dropna(subset=["cid"])
        day_df["cid"] = day_df["cid"].astype(int)

        stocks_df = day_df[["datetime", "cid", "last", "volume"]].rename(
            columns={"datetime": "date", "last": "value"}
        )
        _flush_stocks(db, stocks_df)

        daily = day_df.groupby("cid").agg(
            open=("last", "first"),
            close=("last", "last"),
            high=("last", "max"),
            low=("last", "min"),
            volume=("volume", "last"),
            mean=("last", "mean"),
            std=("last", "std"),
        ).reset_index()

        daily["std"] = daily["std"].fillna(0.0)
        daily["date"] = pd.to_datetime(day_str)

        _flush_daystocks(db, daily)

        days_processed += 1

    _mark_file_done(db, done_key)


def store_files(start: str, end: str, website: str, db: TSDB):
    if website == "bourso":
        _store_bourso_files(start, end, db)
    elif website == "euronext":
        _store_euronext_files(start, end, db)
    else:
        raise ValueError(f"Unknown website: {website}. Use 'bourso' or 'euronext'.")
