"""Official data access layer for CHAOS-Reverse-Miner.

Every series is downloaded from the public FRED CSV endpoint (no API key needed):

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>

If a local copy already exists in ``data/<SERIES_ID>.csv`` it is used instead of
the network.  Nothing is ever synthesised, estimated or back-filled: a failed
download is a hard error, never a fabricated series.

Provenance
----------
* Source: Federal Reserve Bank of St. Louis (FRED)
* Access method: official public ``fredgraph.csv`` endpoint
* Raw files are cached under ``data/`` and fingerprinted in the outputs so every
  number in ``results/`` can be traced back to the exact bytes that produced it.
"""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

#: FRED series used by the mining engine.
SERIES_CATALOG: dict[str, str] = {
    "UNRATE": "Civilian Unemployment Rate (monthly, percent)",
    "PAYEMS": "All Employees, Total Nonfarm (monthly, thousands of persons)",
    "ICSA": "Initial Claims (weekly, seasonally adjusted)",
    "WCOILBRENTEU": "Crude Oil Prices: Brent - Europe (weekly, USD/barrel)",
}

#: The 12 benchmark crisis / shock anchors required by the specification.
EVENTS: list[tuple[str, str, str, int]] = [
    ("NBER_1969_PEAK", "UNRATE", "1969-12-01", 1),
    ("NBER_1973_PEAK", "UNRATE", "1973-11-01", 1),
    ("NBER_1980_PEAK", "UNRATE", "1980-01-01", 1),
    ("NBER_1981_PEAK", "UNRATE", "1981-07-01", 1),
    ("NBER_1990_PEAK", "UNRATE", "1990-07-01", 1),
    ("NBER_2001_PEAK", "UNRATE", "2001-03-01", 1),
    ("NBER_2007_PEAK", "UNRATE", "2007-12-01", 1),
    ("GFC_LEHMAN_2008", "UNRATE", "2008-09-15", 1),
    ("NBER_2020_PEAK", "UNRATE", "2020-02-01", 1),
    ("COVID_ICSA_2020", "ICSA", "2020-03-11", 1),
    ("OIL_DECLINE_2014", "WCOILBRENTEU", "2014-06-01", -1),
    ("UKRAINE_OIL_2022", "WCOILBRENTEU", "2022-02-24", 1),
]

#: PAYEMS cycle peaks share the NBER peak dates but are *contractions* of an
#: employment series, hence ``-1`` (the alarm must fire on the negative side).
PAYEMS_PEAK_DATES: list[str] = [
    "1969-12-01",
    "1973-11-01",
    "1980-01-01",
    "1981-07-01",
    "1990-07-01",
    "2001-03-01",
    "2007-12-01",
    "2020-02-01",
]

for _date in PAYEMS_PEAK_DATES:
    EVENTS.append((f"PAYEMS_PEAK_{_date[:4]}", "PAYEMS", _date, -1))


#: Pre-2005 weekly anchors added in Cycle C4 so the weekly specialist can be
#: trained cold instead of in-sample.  Dates come from the C4 directive; each
#: one was cross-checked against the raw FRED prints (ICSA rises into the
#: recession dates, Brent jumps 20.9 -> 26.4 in the week of 1990-08-02).
WEEKLY_ANCHORS: list[tuple[str, str, str, int]] = [
    ("GULF_WAR_OIL_1990", "WCOILBRENTEU", "1990-08-02", 1),
    ("RECESSION_ICSA_1973", "ICSA", "1973-11-01", 1),
    ("RECESSION_ICSA_1980", "ICSA", "1980-01-01", 1),
    ("RECESSION_ICSA_1990", "ICSA", "1990-07-01", 1),
    ("DOTCOM_ICSA_2001", "ICSA", "2001-03-01", 1),
]
EVENTS.extend(WEEKLY_ANCHORS)


#: Per-series conditioning: v2.0 treats growing series as relative changes and
#: keeps UNRATE (a rate that can hit zero) on plain first differences.
SERIES_PROFILES: dict[str, dict[str, str]] = {
    "UNRATE": {"frequency": "monthly", "transform": "diff"},
    "PAYEMS": {"frequency": "monthly", "transform": "log_return"},
    "ICSA": {"frequency": "weekly", "transform": "log_return"},
    "WCOILBRENTEU": {"frequency": "weekly", "transform": "log_return"},
}

#: Frequency-aware search geometry.  Window / lookback / gap / cooldown are all
#: expressed in the native steps of the series, not in one shared calendar.
FREQUENCY_PROFILES: dict[str, dict[str, object]] = {
    "monthly": {
        "windows": (18, 24, 36),
        "cascade_gap": 3,
        "cooldown": 6,
    },
    "weekly": {
        "windows": (36, 44, 52),
        "cascade_gap": 6,
        "cooldown": 12,
    },
}

#: A formula is defined once and mapped onto each series' own clock.  "fast"
#: means an 18-month baseline for monthly data and a 36-week baseline for weekly
#: data - the same *relative* horizon, not the same number of steps.
WINDOW_POLICIES: dict[str, dict[str, int]] = {
    "fast": {"monthly": 18, "weekly": 36},
    "medium": {"monthly": 24, "weekly": 44},
    "slow": {"monthly": 36, "weekly": 52},
}

#: Lookback lengths in native steps, again by policy.
LEAD_POLICIES: dict[str, dict[str, int]] = {
    "short": {"monthly": 4, "weekly": 6},
    "medium": {"monthly": 6, "weekly": 12},
    "long": {"monthly": 12, "weekly": 24},
}


def get_profiles() -> dict[str, dict[str, str]]:
    """Return a copy of the per-series conditioning profiles."""
    return {sid: dict(profile) for sid, profile in SERIES_PROFILES.items()}


def get_frequency_profiles() -> dict[str, dict[str, object]]:
    """Return a copy of the frequency-aware search geometry."""
    return {freq: dict(profile) for freq, profile in FREQUENCY_PROFILES.items()}


def get_window_policies() -> dict[str, dict[str, int]]:
    return {name: dict(mapping) for name, mapping in WINDOW_POLICIES.items()}


def get_lead_policies() -> dict[str, dict[str, int]]:
    return {name: dict(mapping) for name, mapping in LEAD_POLICIES.items()}


# --------------------------------------------------------------------------- #
# Cycle C5: liquidity / credit / volatility / financial-conditions layers
# --------------------------------------------------------------------------- #
#: Kept separate from ``SERIES_CATALOG`` so v1.0 panels and thresholds are never
#: affected by the C5 expansion.
C5_SERIES: dict[str, str] = {
    "T10Y2Y": "10-Year minus 2-Year Treasury spread (liquidity layer, daily)",
    "BAMLH0A0HYM2": "ICE BofA US High Yield OAS (credit layer, daily; FRED restricts history to 3 years)",
    "BAA10Y": "Moody's Baa minus 10-Year Treasury (credit-stress proxy, daily from 1986)",
    "VIXCLS": "CBOE VIX close (volatility/contagion layer, daily)",
    "NFCI": "Chicago Fed National Financial Conditions Index (weekly)",
}

C5_TRANSFORMS: dict[str, str] = {
    "T10Y2Y": "diff",
    "BAMLH0A0HYM2": "diff",
    "BAA10Y": "diff",
    "VIXCLS": "log_return",
    "NFCI": "diff",
    "UNRATE": "diff",
    "PAYEMS": "log_return",
    "ICSA": "log_return",
    "WCOILBRENTEU": "log_return",
}

#: Stress direction per layer: +1 means "higher = stress", -1 means "lower = stress".
C5_STRESS_DIRECTION: dict[str, int] = {
    "T10Y2Y": -1,          # curve flattening / inversion
    "BAMLH0A0HYM2": 1,     # spreads widen
    "BAA10Y": 1,           # credit spread widens
    "VIXCLS": 1,           # fear rises
    "NFCI": 1,             # conditions tighten
    "UNRATE": 1,
    "PAYEMS": -1,
    "ICSA": 1,
    "WCOILBRENTEU": 1,
}

C5_TRAIN_ANCHORS: list[tuple[str, str]] = [
    ("LTCM_RUSSIA_1998", "1998-08-17"),
    ("DOTCOM_CRASH_2000", "2000-03-24"),
]

C5_COLD_ANCHORS: list[tuple[str, str]] = [
    ("EURO_DEBT_US_DOWNGRADE_2011", "2011-08-05"),
    ("HY_ENERGY_CRUNCH_2015", "2015-12-11"),
    ("SVB_BANKING_STRESS_2023", "2023-03-10"),
]

#: C5 v2: nine stations used for the daily-grid formula comparison.  The first
#: two are the backward-training anchors; the rest are evaluation stations.
C5_V2_ANCHORS: list[tuple[str, str]] = [
    ("LTCM_RUSSIA_1998", "1998-08-17"),
    ("DOTCOM_CRASH_2000", "2000-03-24"),
    ("NBER_2007_PEAK", "2007-12-01"),
    ("GFC_LEHMAN_2008", "2008-09-15"),
    ("EURO_DEBT_US_DOWNGRADE_2011", "2011-08-05"),
    ("HY_ENERGY_CRUNCH_2015", "2015-12-11"),
    ("NBER_2020_PEAK", "2020-02-01"),
    ("UKRAINE_OIL_2022", "2022-02-24"),
    ("SVB_BANKING_STRESS_2023", "2023-03-10"),
]

C5_FAST_LAYERS: tuple[str, ...] = ("T10Y2Y", "BAA10Y", "VIXCLS")

#: Backward-derived weights: layers that led the 1998/2000 breaks get 1.5.
C5_V2_WEIGHTS: dict[str, float] = {
    "T10Y2Y": 1.5,
    "BAA10Y": 1.0,
    "VIXCLS": 1.5,
    "NFCI": 1.5,
    "UNRATE": 1.0,
    "PAYEMS": 1.0,
    "ICSA": 1.5,
    "WCOILBRENTEU": 1.5,
}


def load_c5_series(force_refresh: bool = False) -> dict[str, pd.Series]:
    """Download/parse the four C5 layers from FRED (official CSV endpoint)."""
    download_all(force_refresh=force_refresh)  # core cache; harmless if present
    series: dict[str, pd.Series] = {}
    for series_id in C5_SERIES:
        path = DATA_DIR / f"{series_id}.csv"
        if force_refresh or not path.exists() or path.stat().st_size == 0:
            print(f"[data_loader] downloading {series_id} ...")
            path.write_bytes(_download(series_id))
        series[series_id] = _parse(path, series_id)
    return series


def to_weekly_friday(series: pd.Series) -> pd.Series:
    """Causal weekly close: last observation inside each Mon-Fri week."""
    weekly = series.resample("W-FRI").last().dropna()
    weekly.name = series.name
    return weekly


def to_month_end(series: pd.Series) -> pd.Series:
    monthly = series.resample("ME").last().dropna()
    monthly.name = series.name
    return monthly


def c5_weekly_layers() -> dict[str, pd.Series]:
    """All eight layers on a Friday-close grid, with publication lags applied.

    Monthly series (UNRATE, PAYEMS) are shifted +1 month before alignment, so a
    Friday decision can only see the previous month's release. Weekly series are
    shifted +1 week. Daily layers keep their Friday close.
    """
    core = load_all()
    extra = load_c5_series()
    layers: dict[str, pd.Series] = {}

    for series_id in ("UNRATE", "PAYEMS"):
        monthly = to_month_end(core[series_id])
        shifted = pd.Series(monthly.to_numpy(), index=monthly.index + pd.DateOffset(months=1))
        layers[series_id] = to_weekly_friday(shifted).ffill()

    for series_id in ("ICSA", "WCOILBRENTEU"):
        weekly = to_weekly_friday(core[series_id])
        layers[series_id] = pd.Series(
            weekly.to_numpy(), index=weekly.index + pd.Timedelta(weeks=1)
        )

    for series_id, series in extra.items():
        if series_id == "NFCI":
            weekly = to_weekly_friday(series)
        else:
            weekly = to_weekly_friday(series)
        layers[series_id] = weekly

    return layers


def _download(series_id: str, retries: int = 4, timeout: int = 90) -> bytes:
    url = FRED_CSV_URL.format(series_id=series_id)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "CHAOS-Reverse-Miner/1.0 (research; python-urllib)",
                    "Accept": "text/csv,*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            if not payload or b"observation_date" not in payload[:200]:
                raise RuntimeError(
                    f"Unexpected payload for {series_id} (first bytes: {payload[:80]!r})"
                )
            return payload
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError) as exc:
            last_error = exc
            print(f"[data_loader] {series_id}: attempt {attempt}/{retries} failed: {exc}")
    raise RuntimeError(
        f"Could not download FRED series {series_id!r} from {url}. "
        f"Last error: {last_error}. Refusing to fabricate data."
    )


def download_all(force_refresh: bool = False) -> dict[str, Path]:
    """Ensure every catalogued series exists on disk. Returns series_id -> path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for series_id in SERIES_CATALOG:
        path = DATA_DIR / f"{series_id}.csv"
        if force_refresh or not path.exists() or path.stat().st_size == 0:
            print(f"[data_loader] downloading {series_id} ...")
            path.write_bytes(_download(series_id))
        paths[series_id] = path
    return paths


def _parse(path: Path, series_id: str) -> pd.Series:
    frame = pd.read_csv(path)
    if frame.shape[1] < 2:
        raise RuntimeError(f"{path} does not look like a FRED CSV (columns={list(frame.columns)})")
    date_col = frame.columns[0]
    value_col = series_id if series_id in frame.columns else frame.columns[1]
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
    frame = frame.dropna(subset=[date_col, value_col]).sort_values(date_col)
    series = pd.Series(
        frame[value_col].to_numpy(dtype=float),
        index=pd.DatetimeIndex(frame[date_col]),
        name=series_id,
    )
    if series.empty:
        raise RuntimeError(f"{path} parsed to an empty series")
    return series


def load_series(series_id: str, force_refresh: bool = False) -> pd.Series:
    """Load one series from the local cache, downloading it first if needed."""
    if series_id not in SERIES_CATALOG:
        raise KeyError(f"{series_id!r} is not part of the catalogue {list(SERIES_CATALOG)}")
    download_all(force_refresh=force_refresh)
    return _parse(DATA_DIR / f"{series_id}.csv", series_id)


def load_all(force_refresh: bool = False) -> dict[str, pd.Series]:
    download_all(force_refresh=force_refresh)
    return {
        series_id: _parse(DATA_DIR / f"{series_id}.csv", series_id)
        for series_id in SERIES_CATALOG
    }


def get_events() -> list[tuple[str, str, str, int]]:
    """Return a copy of the anchor-event registry."""
    return list(EVENTS)


def fingerprint(paths: dict[str, Path] | None = None) -> dict[str, dict[str, object]]:
    """SHA-256 + row count + date span for every raw file (audit trail)."""
    if paths is None:
        paths = {sid: DATA_DIR / f"{sid}.csv" for sid in SERIES_CATALOG}
    report: dict[str, dict[str, object]] = {}
    for series_id, path in paths.items():
        if not path.exists():
            report[series_id] = {"sha256": None, "rows": 0, "start": None, "end": None}
            continue
        payload = path.read_bytes()
        series = _parse(path, series_id)
        report[series_id] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "rows": int(len(series)),
            "start": str(series.index.min().date()),
            "end": str(series.index.max().date()),
        }
    return report


if __name__ == "__main__":
    import json

    frames = load_all()
    for sid, series in frames.items():
        print(
            f"{sid:<14} {len(series):>5} rows  "
            f"{series.index.min().date()} .. {series.index.max().date()}"
        )
    print(f"\n{len(EVENTS)} anchor events registered.")
    print(json.dumps(fingerprint(), indent=2))
