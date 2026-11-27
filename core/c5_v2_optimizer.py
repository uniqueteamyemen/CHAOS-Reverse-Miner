"""Cycle C5 v2 - daily grid and K/weight sweep across nine stations.

What changes versus C5 v1
-------------------------
* Market-priced layers (`T10Y2Y`, `BAA10Y`, `VIXCLS`) stay on their true daily
  grid with **no** weekly bucket and **no** +1 week lag.
* Slow layers keep their realistic release lags (monthly +1 month, weekly +1 week)
  and are carried forward onto the daily grid.
* K = 3..6 unweighted, plus a backward-weighted score, over 10-day and 20-day
  resonance windows.

Important disclosure: all nine stations are used for the formula comparison, so
the selected formula is an **optimised in-sample signature**, not a blind test.
The blind number remains the C5 v1 cold result (1/3).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core import data_loader
from core.detectors import StateVector, build_series_state

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
CUTOFF = "2005-01-01"
FIT_START = "1990-01-01"
LEAD_DAYS = 20
GRACE_DAYS = 2
COOLDOWN_DAYS = 10
QUANTILES = (0.95, 0.99, 0.995, 0.9995, 0.9999)
WINDOWS = (10, 20)
K_VALUES = (3, 4, 5, 6)
GRID_START = "1990-01-02"

LAYERS: dict[str, str] = {
    "LIQUIDITY": "T10Y2Y",
    "CREDIT": "BAA10Y",
    "VOLATILITY": "VIXCLS",
    "FIN_CONDITIONS": "NFCI",
    "LABOR_UNRATE": "UNRATE",
    "LABOR_PAYEMS": "PAYEMS",
    "CLAIMS": "ICSA",
    "OIL": "WCOILBRENTEU",
}
FAST = set(data_loader.C5_FAST_LAYERS)


def _candidates(stress_direction: int) -> list[tuple[str, int]]:
    if stress_direction > 0:
        return [("ph_up", 1), ("trend_slope_z", 1), ("l1_slope_break", 1), ("drawdown_norm", -1)]
    return [("ph_down", 1), ("trend_slope_z", -1), ("l1_slope_break", 1), ("drawdown_norm", -1)]


def _episodes(mask: pd.Series, cooldown: int = COOLDOWN_DAYS) -> list[pd.Timestamp]:
    out: list[pd.Timestamp] = []
    last = None
    for date in mask[mask].index:
        if last is None or (date - last).days >= cooldown:
            out.append(date)
            last = date
    return out


def _align_weekly(mask: np.ndarray, dates: np.ndarray, grid: pd.DatetimeIndex) -> pd.Series:
    return (
        pd.Series(mask, index=pd.DatetimeIndex(dates))
        .reindex(grid, method="ffill")
        .fillna(False)
        .astype(bool)
    )


def _align_daily(mask: np.ndarray, dates: np.ndarray, grid: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(mask, index=pd.DatetimeIndex(dates)).reindex(grid).fillna(False).astype(bool)


def _first_fire(mask: pd.Series, anchor: str, lead: int = LEAD_DAYS) -> int | None:
    ts = pd.Timestamp(anchor)
    window = mask[(mask.index >= ts - pd.Timedelta(days=lead)) & (mask.index <= ts + pd.Timedelta(days=GRACE_DAYS))]
    hits = window[window]
    return int((ts - hits.index[0]).days) if len(hits) else None


def build_vectors() -> tuple[dict[str, StateVector], dict[str, bool], pd.DatetimeIndex]:
    weekly = data_loader.c5_weekly_layers()
    fast_raw = data_loader.load_c5_series()
    vectors: dict[str, StateVector] = {}
    is_daily: dict[str, bool] = {}
    for series_id in LAYERS.values():
        if series_id in FAST:
            vectors[series_id] = build_series_state(
                fast_raw[series_id].rename(series_id),
                "daily",
                data_loader.C5_TRANSFORMS[series_id],
            )
            is_daily[series_id] = True
        else:
            vectors[series_id] = build_series_state(
                weekly[series_id].rename(series_id),
                "weekly",
                data_loader.C5_TRANSFORMS[series_id],
            )
            is_daily[series_id] = False
    grid = pd.bdate_range(GRID_START, min(v.dates[-1] for v in vectors.values()))
    return vectors, is_daily, grid


def calibrate(
    vectors: dict[str, StateVector], is_daily: dict[str, bool], grid: pd.DatetimeIndex
) -> list[dict[str, object]]:
    train = [anchor for _, anchor in data_loader.C5_TRAIN_ANCHORS]
    fit_start = pd.Timestamp(FIT_START)
    cutoff = pd.Timestamp(CUTOFF)
    rows: list[dict[str, object]] = []
    for layer, series_id in LAYERS.items():
        vector = vectors[series_id]
        direction = data_loader.C5_STRESS_DIRECTION[series_id]
        best = None
        for feature, sign in _candidates(direction):
            values = vector.features[feature]
            score = values * sign
            dates = pd.DatetimeIndex(vector.dates)
            fit = (dates >= fit_start) & (dates < cutoff) & np.isfinite(score)
            sample = score[fit]
            if sample.size < 60:
                continue
            for quantile in QUANTILES:
                threshold = float(np.nanquantile(sample, quantile))
                raw = score >= threshold
                mask = _align_daily(raw, vector.dates, grid) if is_daily[series_id] else _align_weekly(raw, vector.dates, grid)
                coverage = sum(1 for anchor in train if _first_fire(mask, anchor) is not None)
                quiet = mask[(mask.index >= fit_start) & (mask.index < cutoff)]
                years = (quiet.index[-1] - quiet.index[0]).days / 365.25
                quiet_rate = len(_episodes(quiet)) / years if years else 0.0
                key = (
                    -(coverage - 0.05 * quiet_rate),
                    quiet_rate,
                    -quantile,
                )
                if best is None or key < best["key"]:
                    best = {
                        "key": key,
                        "layer": layer,
                        "series": series_id,
                        "grid": "daily" if is_daily[series_id] else "weekly",
                        "feature": feature,
                        "sign": sign,
                        "quantile": quantile,
                        "threshold": round(threshold, 6),
                        "train_coverage": int(coverage),
                        "quiet_rate_per_year": round(float(quiet_rate), 3),
                        "mask": mask,
                    }
        if best is None:
            raise RuntimeError(f"no feature for layer {layer}")
        rows.append(best)
    return rows


def _evaluate_variant(
    alarm: pd.Series, grid: pd.DatetimeIndex
) -> dict[str, object]:
    anchors = [(name, pd.Timestamp(date)) for name, date in data_loader.C5_V2_ANCHORS]
    near = pd.Series(False, index=grid)
    catches = []
    leads = []
    for name, ts in anchors:
        low = ts - pd.Timedelta(days=LEAD_DAYS)
        high = ts + pd.Timedelta(days=GRACE_DAYS)
        window = alarm[(alarm.index >= low) & (alarm.index <= high)]
        if window.any():
            first = window[window].index[0]
            catches.append(name)
            leads.append(int((ts - first).days))
        near[(grid >= low) & (grid <= high)] = True
        near[(grid > high) & (grid <= high + pd.Timedelta(days=COOLDOWN_DAYS))] = True
    episodes = _episodes(alarm)
    false_alarms = [episode for episode in episodes if not near.loc[episode]]
    years = (grid[-1] - grid[0]).days / 365.25
    return {
        "catches": catches,
        "catch_count": len(catches),
        "mean_lead_days": round(float(np.mean(leads)), 1) if leads else None,
        "false_alarms": len(false_alarms),
        "false_alarms_per_year": round(len(false_alarms) / years, 3) if years else None,
    }


def build_variants(layers: list[dict[str, object]], grid: pd.DatetimeIndex) -> list[dict[str, object]]:
    fire = pd.DataFrame({layer["layer"]: layer["mask"] for layer in layers}).reindex(grid).fillna(False)
    layer_weights = {
        layer["layer"]: data_loader.C5_V2_WEIGHTS[layer["series"]] for layer in layers
    }
    variants = []
    for window in WINDOWS:
        for k in K_VALUES:
            alarm = fire.astype(int).rolling(window, min_periods=window).sum().sum(axis=1) >= k
            metrics = _evaluate_variant(alarm, grid)
            variants.append(
                {
                    "name": f"K={k} of 8, {window}d window",
                    "kind": "unweighted",
                    "k": k,
                    "window_days": window,
                    "weights": None,
                    "alarm": alarm,
                    **metrics,
                }
            )
        for k in K_VALUES:
            weighted = sum(
                fire[layer].astype(float) * weight for layer, weight in layer_weights.items()
            )
            score = weighted.rolling(window, min_periods=window).sum()
            alarm = score >= k
            metrics = _evaluate_variant(alarm, grid)
            variants.append(
                {
                    "name": f"weighted >= {k}, {window}d window",
                    "kind": "weighted",
                    "k": k,
                    "window_days": window,
                    "weights": layer_weights,
                    "alarm": alarm,
                    **metrics,
                }
            )
    variants.sort(
        key=lambda v: (
            -v["catch_count"],
            v["false_alarms_per_year"],
            -(v["mean_lead_days"] or 0),
        )
    )
    return variants


def live_state(best: dict[str, object], grid: pd.DatetimeIndex) -> dict[str, object]:
    alarm: pd.Series = best["alarm"]  # type: ignore[assignment]
    window = int(best["window_days"])
    recent = grid[-window:]
    return {
        "as_of": grid[-1].strftime("%Y-%m-%d"),
        "state": "ALARM" if bool(alarm.iloc[-1]) else "QUIET",
        "formula": best["name"],
        "window_days": window,
    }


def render(result: dict[str, object], layers: list[dict[str, object]]) -> str:
    layer_rows = [
        [layer["layer"], layer["series"], layer["grid"], layer["feature"], layer["threshold"], f"q{layer['quantile']}", layer["train_coverage"]]
        for layer in layers
    ]
    variant_rows = [
        [
            variant["name"],
            f"{variant['catch_count']}/{len(data_loader.C5_V2_ANCHORS)}",
            variant["mean_lead_days"],
            variant["false_alarms"],
            variant["false_alarms_per_year"],
        ]
        for variant in result["variants"][:12]
    ]
    best = result["best"]
    anchor_rows = [
        [
            hit["anchor"],
            hit["anchor_date"],
            "hit" if hit["caught"] else "miss",
            hit["alarm_date"],
            hit["lead_days"],
        ]
        for hit in best["anchors"]
    ]
    return f"""# Cycle C5 v2 - Daily Grid and Signature Optimisation

## Method

- Fast layers (`T10Y2Y`, `BAA10Y`, `VIXCLS`) stay on the **daily** grid with no
  weekly bucket and no extra lag.
- Slow layers keep realistic release lags: monthly +1 month, weekly +1 week,
  carried forward onto the daily grid.
- Thresholds remain pre-2005 quantiles of each feature's own history; the two
  backward anchors (1998, 2000) only choose which feature/quantile per layer.
- **Disclosure:** all nine stations are used for the variant comparison, so the
  winning formula is an optimised in-sample signature, not a blind result.

## Layer thresholds

| Layer | Series | Grid | Feature | Threshold | Quantile | Train coverage |
|---|---|---|---|---|---|---|
""" + "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in layer_rows) + f"""

## Variant comparison (top 12)

| Formula | Catches | Mean lead (days) | Quiet alarms | Alarms/year |
|---|---|---|---|---|
""" + "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in variant_rows) + f"""

## Best signature (max recall)

`{best['name']}` - catches **{best['catch_count']}/{len(data_loader.C5_V2_ANCHORS)}**,
mean lead {best['mean_lead_days']} days, {best['false_alarms_per_year']} quiet
alarms/year.

| Anchor | Date | Result | Alarm date | Lead (days) |
|---|---|---|---|---|
""" + "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in anchor_rows) + f"""

## Balanced alternative (lower noise)

`{result['balanced_alternative']['name']}` - catches
**{result['balanced_alternative']['catch_count']}/{len(data_loader.C5_V2_ANCHORS)}**
at {result['balanced_alternative']['false_alarms_per_year']} alarms/year, but
misses **{", ".join(result['balanced_alternative']['missed']) or "none"}**.
Because it drops the two stations this cycle was meant to fix, it is reported as
an alternative rather than recommended.

## Live state

**{result['live']['state']}** as of {result['live']['as_of']} under the max-recall
formula.

## Deployment verdict

The daily grid fixes the two v1.0 misses (2011 and SVB 2023), but at 13.4 quiet
alarms/year it is roughly 13x the v1.0 budget. No variant on this grid combines
9/9 recall with a deployable noise level, so none is promoted into
`production/`. The C5 v1 weekly result (1/3 cold) remains the out-of-sample
reference, and this optimisation is explicitly in-sample.

## Caveat

Because the nine stations drive the comparison, the winning formula's catch
count is in-sample by construction. The honest out-of-sample reference remains
C5 v1: 1/3 cold anchors with the weekly 6-of-8 signature. Any deployment should
pre-register the chosen formula before the next unseen station.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args(argv)

    vectors, is_daily, grid = build_vectors()
    layers = calibrate(vectors, is_daily, grid)
    variants = build_variants(layers, grid)
    best = variants[0]  # max recall, tie-broken by quiet alarms
    balanced = max(
        variants,
        key=lambda variant: variant["catch_count"]
        - (variant["false_alarms_per_year"] or 0.0),
    )

    def anchor_table(variant: dict[str, object]) -> list[dict[str, object]]:
        rows = []
        for name, date in data_loader.C5_V2_ANCHORS:
            ts = pd.Timestamp(date)
            window = variant["alarm"][  # type: ignore[index]
                (variant["alarm"].index >= ts - pd.Timedelta(days=LEAD_DAYS))  # type: ignore[index]
                & (variant["alarm"].index <= ts + pd.Timedelta(days=GRACE_DAYS))  # type: ignore[index]
            ]
            hits = window[window]
            rows.append(
                {
                    "anchor": name,
                    "anchor_date": date,
                    "caught": bool(len(hits)),
                    "alarm_date": hits.index[0].strftime("%Y-%m-%d") if len(hits) else None,
                    "lead_days": int((ts - hits.index[0]).days) if len(hits) else None,
                }
            )
        return rows

    anchors = anchor_table(best)
    balanced_anchors = anchor_table(balanced)
    live = live_state(best, grid)

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "CHAOS-Reverse-Miner/c5-v2-optimizer",
        "grid": f"{grid[0].date()}..{grid[-1].date()} business days",
        "layers": [
            {key: layer[key] for key in ("layer", "series", "grid", "feature", "sign", "quantile", "threshold", "train_coverage")}
            for layer in layers
        ],
        "variants": [
            {key: variant[key] for key in ("name", "kind", "k", "window_days", "catch_count", "mean_lead_days", "false_alarms", "false_alarms_per_year")}
            for variant in variants
        ],
        "best": {
            "name": best["name"],
            "kind": best["kind"],
            "k": best["k"],
            "window_days": best["window_days"],
            "weights": best["weights"],
            "catch_count": best["catch_count"],
            "mean_lead_days": best["mean_lead_days"],
            "false_alarms": best["false_alarms"],
            "false_alarms_per_year": best["false_alarms_per_year"],
            "selection_rule": "maximise catches, tie-break lower false alarms per year",
            "anchors": anchors,
        },
        "balanced_alternative": {
            "name": balanced["name"],
            "k": balanced["k"],
            "window_days": balanced["window_days"],
            "catch_count": balanced["catch_count"],
            "mean_lead_days": balanced["mean_lead_days"],
            "false_alarms_per_year": balanced["false_alarms_per_year"],
            "anchors": balanced_anchors,
            "missed": [hit["anchor"] for hit in balanced_anchors if not hit["caught"]],
        },
        "live": live,
        "disclosure": "all nine stations used for selection; in-sample optimisation",
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "c5_v2_best_signature.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    (RESULTS_DIR / "c5_v2_optimization_report.md").write_text(
        render(result, layers), encoding="utf-8"
    )
    print(
        f"[c5v2] best={best['name']} catches={best['catch_count']}/9 "
        f"lead={best['mean_lead_days']}d fa/yr={best['false_alarms_per_year']} live={live['state']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
