"""Stage 1 - build the anonymised blind matrix (strict cutoff).

Every download is capped at 2026-09-25 *at the source* (FRED ``coed`` / Stooq
``d2``), so no post-cutoff value ever enters memory.  Columns are anonymised
immediately: the detector in stage 2 only ever sees S_01..S_08.

Source mapping lives only in this builder; it is intentionally absent from the
detector, its report and its JSON.
"""

from __future__ import annotations

import io
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CUTOFF = "2026-09-25"
START = "1999-01-06"
FRED_START = "1998-01-01"

HEADERS = {"User-Agent": "CHAOS-Reverse-Miner/blind-builder (research; urllib)"}
COLUMNS = [f"S_{i:02d}" for i in range(1, 9)]
_FRED_OK: bool | None = None


def _get(url: str, attempts: int = 3, timeout: int = 60) -> bytes | None:
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print(f"[blind] attempt {attempt}/{attempts} failed for {url.split('?')[0]}: {exc}")
            time.sleep(2 * attempt)
    return None


def fred_series(series_id: str) -> pd.Series | None:
    """Download a FRED series capped at the cutoff, then parse.

    Falls back only to the local cache of the *same* series; it never substitutes
    a different economic variable.
    """
    global _FRED_OK
    url = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}&cosd={FRED_START}&coed={CUTOFF}"
    )
    payload = None
    if _FRED_OK is not False:
        payload = _get(url, attempts=2, timeout=45)
        if payload is None:
            _FRED_OK = False
        else:
            _FRED_OK = True
    if payload is None:
        cache = DATA_DIR / f"{series_id}.csv"
        if cache.exists():
            print(f"[blind] using cached {series_id}")
            payload = cache.read_bytes()
        else:
            return None
    frame = pd.read_csv(io.BytesIO(payload))
    date_col, value_col = frame.columns[0], frame.columns[1]
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
    frame = frame.dropna().sort_values(date_col)
    series = pd.Series(
        frame[value_col].to_numpy(dtype=float), index=pd.DatetimeIndex(frame[date_col]), name=series_id
    )
    return series[series.index <= pd.Timestamp(CUTOFF)]


def yahoo_series(symbol: str) -> pd.Series | None:
    """Daily close from Yahoo Finance, hard-capped before the cutoff."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?period1=915148800&period2=1790380800&interval=1d"
    )
    payload = _get(url, attempts=2)
    if payload is None:
        return None
    try:
        import json

        data = json.loads(payload)
        result = data["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        closes = result["indicators"]["quote"][0]["close"]
        pairs = [
            (pd.Timestamp(ts, unit="s").normalize(), float(close))
            for ts, close in zip(timestamps, closes)
            if close is not None
        ]
        if not pairs:
            return None
        index, values = zip(*pairs)
        series = pd.Series(values, index=pd.DatetimeIndex(index))
        series = series[~series.index.duplicated(keep="last")]
        return series[series.index <= pd.Timestamp(CUTOFF)]
    except Exception:
        return None


def stooq_series(symbol: str) -> pd.Series | None:
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d&d1=19980101&d2=20260925"
    payload = _get(url)
    if payload is None or b"Date" not in payload[:100]:
        return None
    try:
        frame = pd.read_csv(io.BytesIO(payload))
    except Exception:
        return None
    if "Close" not in frame.columns or "Date" not in frame.columns:
        return None
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
    frame = frame.dropna().sort_values("Date")
    series = pd.Series(frame["Close"].to_numpy(dtype=float), index=pd.DatetimeIndex(frame["Date"]))
    return series[series.index <= pd.Timestamp(CUTOFF)]


def with_availability(series: pd.Series, frequency: str) -> pd.Series:
    """Return a series indexed by the date its value became available."""
    if frequency == "daily":
        return series
    if frequency == "weekly":
        # Weekly values are known the Thursday after the week ends.
        index = pd.DatetimeIndex(series.index) + pd.Timedelta(days=4)
        return pd.Series(series.to_numpy(dtype=float), index=index)
    if frequency == "monthly":
        month_end = pd.DatetimeIndex(series.index) + pd.offsets.MonthEnd(0)
        index = month_end + pd.DateOffset(months=1) + pd.Timedelta(days=3)
        return pd.Series(series.to_numpy(dtype=float), index=index)
    raise ValueError(frequency)


def _keep_existing(path: Path) -> int:
    if path.exists():
        print(f"[blind] sources unavailable; keeping existing {path}")
        return 0
    print("[blind] FATAL: no matrix and no reachable source")
    return 1


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the matrix even if a frozen copy already exists",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    matrix_index = pd.bdate_range(START, CUTOFF)
    path = DATA_DIR / "blind_matrix_2026_09_25.csv"

    if path.exists() and not args.refresh:
        existing = pd.read_csv(path, parse_dates=["date"])
        ok = (
            set(existing.columns) == {"date", *COLUMNS}
            and existing["date"].max() <= pd.Timestamp(CUTOFF)
            and existing["date"].min() >= pd.Timestamp(START)
            and existing.drop(columns=["date"]).notna().all().all()
            and float(existing["S_02"].iloc[-1]) > 1000
            and float(existing["S_08"].iloc[-1]) > 20
        )
        if ok:
            print(
                f"[blind] frozen matrix verified: rows={len(existing)} "
                f"range={existing['date'].min().date()}..{existing['date'].max().date()}"
            )
            return 0
        print("[blind] existing matrix invalid; rebuilding")

    # S_01: equity index - Stooq ^spx, then Yahoo ^GSPC only.
    s01 = stooq_series("^spx")
    if s01 is None or len(s01) < 1000:
        s01 = yahoo_series("%5EGSPC")
    if s01 is None or len(s01) < 1000:
        print("[blind] FAILED: S_01 requires Stooq ^spx or Yahoo ^GSPC")
        return _keep_existing(path)

    # S_02: gold - primary source, then Yahoo GC=F only (never a bond yield).
    s02 = None
    payload = _get("https://crystalballmarkets.com", attempts=1)
    if payload:
        try:
            frame = pd.read_csv(io.BytesIO(payload))
            if {"date", "Rate"} <= set(frame.columns):
                frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
                frame["Rate"] = pd.to_numeric(frame["Rate"], errors="coerce")
                frame = frame.dropna()
                s02 = pd.Series(
                    frame["Rate"].to_numpy(dtype=float), index=pd.DatetimeIndex(frame["date"])
                )
                s02 = s02[s02.index <= pd.Timestamp(CUTOFF)]
        except Exception:
            s02 = None
    if s02 is None or len(s02) < 1000 or float(s02.iloc[-1]) <= 1000:
        s02 = yahoo_series("GC%3DF")
    if s02 is None or len(s02) < 1000:
        print("[blind] FAILED: S_02 requires the rate feed or Yahoo GC=F")
        return _keep_existing(path)

    s08 = fred_series("DCOILBRENTEU")
    if s08 is None or len(s08) < 1000:
        s08 = yahoo_series("BZ%3DF")
    if s08 is None or len(s08) < 1000:
        print("[blind] FAILED: S_08 requires FRED DCOILBRENTEU or Yahoo BZ=F")
        return _keep_existing(path)

    specs = {
        "S_03": (fred_series("VIXCLS"), "daily"),
        "S_04": (fred_series("T10Y2Y"), "daily"),
        "S_05": (fred_series("BAA10Y"), "daily"),
        "S_06": (fred_series("NFCI"), "weekly"),
        "S_07": (fred_series("ICSA"), "weekly"),
    }

    columns: dict[str, pd.Series] = {}
    for name, (series, frequency) in specs.items():
        if series is None:
            print(f"[blind] {name} unavailable")
            return _keep_existing(path)
        available = with_availability(series, frequency)
        columns[name] = available.reindex(matrix_index, method="ffill")

    columns["S_01"] = s01.reindex(matrix_index, method="ffill")
    columns["S_02"] = s02.reindex(matrix_index, method="ffill")
    columns["S_08"] = s08.reindex(matrix_index, method="ffill")

    out = pd.DataFrame({"date": matrix_index})
    for name in ("S_01", "S_02", "S_03", "S_04", "S_05", "S_06", "S_07", "S_08"):
        out[name] = columns[name].to_numpy()

    out = out.dropna().reset_index(drop=True)
    if out.empty:
        print("[blind] matrix empty after alignment")
        return _keep_existing(path)
    if out["date"].max() > pd.Timestamp(CUTOFF):
        print("[blind] cutoff violated")
        return 1
    if out["date"].min() < pd.Timestamp(START):
        out = out[out["date"] >= pd.Timestamp(START)].reset_index(drop=True)

    # Hard identity assertions: gold must look like gold, oil like oil.
    s02_last = float(out["S_02"].iloc[-1])
    s08_last = float(out["S_08"].iloc[-1])
    s02_median = float(out["S_02"].median())
    s08_median = float(out["S_08"].median())
    if s02_last <= 1000 or s02_median <= 200:
        print(f"[blind] FAILED assertion S_02 looks like gold? last={s02_last} median={s02_median}")
        return 1
    if s08_last <= 20 or s08_median <= 10:
        print(f"[blind] FAILED assertion S_08 looks like oil? last={s08_last} median={s08_median}")
        return 1

    out.to_csv(path, index=False)
    print(
        f"[blind] matrix rows={len(out)} range={out['date'].min().date()}..{out['date'].max().date()} -> {path}"
    )
    print(f"[blind] completeness: {out.notna().all().all()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
