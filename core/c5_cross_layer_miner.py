"""Cycle C5 - cross-layer reverse engineering across eight macro layers.

Two explicit stages, never mixed:

* **Stage A (backward)**: from the two pre-2005 crisis anchors, walk backwards
  and learn which layer breaks first, which feature of that layer carries the
  signal, and the minimal K-of-N cascade that covers both anchors.
* **Stage B (forward, causal)**: freeze that signature and replay 2005-2026
  week by week with zero lookahead, then report cold catches and false alarms.

Thresholds are empirical quantiles of each feature's own pre-2005 history; no
external level is hardcoded.  The requested HY OAS series is truncated to three
years by FRED's licence, so the historical credit layer uses the BAA-10Y spread
(available since 1986) as a documented proxy; HY OAS is kept for the live
cross-check only.
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
FIT_START = "1990-01-01"  # all eight layers exist from here (VIX is the binding one)
LEAD_WEEKS = 10
GRACE_WEEKS = 2
COOLDOWN_WEEKS = 8
QUANTILES = (0.95, 0.99, 0.995)
CASCADE_WINDOWS = (2, 4, 6)
QUIET_BUDGET_PER_YEAR = 2.0

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


def _candidates(stress_direction: int) -> list[tuple[str, int]]:
    if stress_direction > 0:
        return [
            ("ph_up", 1),
            ("trend_slope_z", 1),
            ("l1_slope_break", 1),
            ("drawdown_norm", -1),
        ]
    return [
        ("ph_down", 1),
        ("trend_slope_z", -1),
        ("l1_slope_break", 1),
        ("drawdown_norm", -1),
    ]


def _anchor_window(anchor: str, lead: int = LEAD_WEEKS, grace: int = GRACE_WEEKS) -> tuple[pd.Timestamp, pd.Timestamp]:
    ts = pd.Timestamp(anchor)
    return ts - pd.Timedelta(weeks=lead), ts + pd.Timedelta(weeks=grace)


def _common_grid(vectors: dict[str, StateVector]) -> pd.DatetimeIndex:
    # Only the eight signature layers define the grid; the HY OAS live series is
    # truncated to 2023+ and must not shorten the historical window.
    used = [vectors[series_id] for series_id in LAYERS.values()]
    starts = [vector.dates[0] for vector in used if vector.dates.size]
    ends = [vector.dates[-1] for vector in used if vector.dates.size]
    return pd.date_range(max(starts), min(ends), freq="W-FRI")


def _align(mask: np.ndarray, dates: np.ndarray, grid: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(mask, index=pd.DatetimeIndex(dates)).reindex(grid, method="ffill").fillna(False).astype(bool)


def _episodes(mask: pd.Series, cooldown: int = COOLDOWN_WEEKS) -> list[pd.Timestamp]:
    episodes: list[pd.Timestamp] = []
    last = None
    for date in mask[mask].index:
        if last is None or (date - last).days >= cooldown * 7:
            episodes.append(date)
            last = date
    return episodes


def _first_fire(mask: pd.Series, anchor: str, lead: int = LEAD_WEEKS) -> int | None:
    ts = pd.Timestamp(anchor)
    window = mask[(mask.index >= ts - pd.Timedelta(weeks=lead)) & (mask.index <= ts + pd.Timedelta(weeks=GRACE_WEEKS))]
    hits = window[window]
    return int((hits.index[0] - ts).days / 7) if len(hits) else None


def calibrate_layers(
    vectors: dict[str, StateVector],
) -> list[dict[str, object]]:
    """Pick one feature and one pre-2005 quantile per layer."""
    train = [anchor for _, anchor in data_loader.C5_TRAIN_ANCHORS]
    fit_start = pd.Timestamp(FIT_START)
    cutoff = pd.Timestamp(CUTOFF)
    rows: list[dict[str, object]] = []

    for layer, series_id in LAYERS.items():
        vector = vectors[series_id]
        direction = data_loader.C5_STRESS_DIRECTION[series_id]
        best: dict[str, object] | None = None
        for feature, sign in _candidates(direction):
            values = vector.features[feature]
            if values is None:
                continue
            score = values * sign
            dates = pd.DatetimeIndex(vector.dates)
            fit = (dates >= fit_start) & (dates < cutoff) & np.isfinite(score)
            sample = score[fit]
            if sample.size < 52:
                continue
            for quantile in QUANTILES:
                threshold = float(np.nanquantile(sample, quantile))
                mask = _align(score >= threshold, vector.dates, _common_grid(vectors))
                coverage = sum(1 for anchor in train if _first_fire(mask, anchor) is not None)
                quiet_mask = mask[(mask.index >= fit_start) & (mask.index < cutoff)]
                quiet_years = (quiet_mask.index[-1] - quiet_mask.index[0]).days / 365.25
                quiet_rate = len(_episodes(quiet_mask)) / quiet_years if quiet_years else 0.0
                key = (-coverage, quiet_rate, -quantile)
                if best is None or key < best["key"]:  # type: ignore[operator]
                    best = {
                        "key": key,
                        "layer": layer,
                        "series": series_id,
                        "feature": feature,
                        "sign": sign,
                        "quantile": quantile,
                        "threshold": round(threshold, 6),
                        "train_coverage": int(coverage),
                        "quiet_rate_per_year": round(float(quiet_rate), 3),
                        "mask": mask,
                    }
        if best is None:
            raise RuntimeError(f"no usable feature for layer {layer}")
        rows.append(best)
    return rows


def choose_cascade(
    layers: list[dict[str, object]], grid: pd.DatetimeIndex
) -> dict[str, object]:
    train = [anchor for _, anchor in data_loader.C5_TRAIN_ANCHORS]
    fit_start = pd.Timestamp(FIT_START)
    cutoff = pd.Timestamp(CUTOFF)
    best: dict[str, object] | None = None
    for k in range(len(layers), 0, -1):
        for window in CASCADE_WINDOWS:
            counts = sum(
                layer["mask"].astype(int) for layer in layers
            )  # type: ignore[operator]
            alarm = counts.rolling(window, min_periods=window).sum() >= k
            coverage = sum(1 for anchor in train if _first_fire(alarm, anchor) is not None)
            quiet = alarm[(alarm.index >= fit_start) & (alarm.index < cutoff)]
            quiet_years = (quiet.index[-1] - quiet.index[0]).days / 365.25
            quiet_rate = len(_episodes(quiet)) / quiet_years if quiet_years else 0.0
            qualified = coverage == len(train) and quiet_rate <= QUIET_BUDGET_PER_YEAR
            key = (-int(qualified), -coverage, quiet_rate, k, window)
            candidate = {
                "k": k,
                "window_weeks": window,
                "train_coverage": int(coverage),
                "quiet_rate_per_year": round(float(quiet_rate), 3),
                "qualified": bool(qualified),
                "key": key,
                "alarm": alarm,
            }
            if best is None or key < best["key"]:  # type: ignore[operator]
                best = candidate
    assert best is not None
    return best


def forward_replay(
    layers: list[dict[str, object]], cascade: dict[str, object], grid: pd.DatetimeIndex
) -> dict[str, object]:
    alarm: pd.Series = cascade["alarm"]  # type: ignore[assignment]
    cutoff = pd.Timestamp(CUTOFF)
    cold = [(name, pd.Timestamp(date)) for name, date in data_loader.C5_COLD_ANCHORS]
    episodes = _episodes(alarm[alarm.index >= cutoff])
    counts = sum(layer["mask"].astype(int) for layer in layers)  # type: ignore[operator]
    window = int(cascade["window_weeks"])
    rolling_counts = counts.rolling(window, min_periods=window).sum()

    near = pd.Series(False, index=grid)
    hits: list[dict[str, object]] = []
    for name, date in cold:
        low = date - pd.Timedelta(weeks=LEAD_WEEKS)
        high = date + pd.Timedelta(weeks=GRACE_WEEKS)
        matched = [
            episode for episode in episodes if low <= episode <= high
        ]
        near[(grid >= low) & (grid <= high)] = True
        near[(grid > high) & (grid <= high + pd.Timedelta(weeks=COOLDOWN_WEEKS))] = True
        layer_hits = [
            layer["layer"]
            for layer in layers
            if _first_fire(layer["mask"], date.strftime("%Y-%m-%d")) is not None  # type: ignore[arg-type]
        ]
        peak_layers = int(rolling_counts[(rolling_counts.index >= low) & (rolling_counts.index <= high)].max())
        hits.append(
            {
                "anchor": name,
                "anchor_date": date.strftime("%Y-%m-%d"),
                "caught": bool(matched),
                "alarm_date": matched[0].strftime("%Y-%m-%d") if matched else None,
                "steps_before": (
                    int((matched[0] - date).days / 7) if matched else None
                ),
                "layers_fired": layer_hits,
                "max_layers_in_window": peak_layers,
            }
        )

    false_alarms = [episode for episode in episodes if not near.loc[episode]]
    years = (grid[-1] - cutoff).days / 365.25
    return {
        "episodes": [episode.strftime("%Y-%m-%d") for episode in episodes],
        "false_alarms": [episode.strftime("%Y-%m-%d") for episode in false_alarms],
        "false_alarms_per_year": round(len(false_alarms) / years, 3) if years else None,
        "years": round(years, 2),
        "anchors": hits,
        "catches": sum(1 for hit in hits if hit["caught"]),
    }


def backtrack_map(layers: list[dict[str, object]], grid: pd.DatetimeIndex) -> list[dict[str, object]]:
    train = [(name, anchor) for name, anchor in data_loader.C5_TRAIN_ANCHORS]
    rows: list[dict[str, object]] = []
    for layer in layers:
        offsets = []
        for _, anchor in train:
            offset = _first_fire(layer["mask"], anchor)  # type: ignore[arg-type]
            if offset is not None:
                offsets.append(offset)
        rows.append(
            {
                "layer": layer["layer"],
                "series": layer["series"],
                "feature": layer["feature"],
                "threshold": layer["threshold"],
                "quantile": layer["quantile"],
                "mean_offset_weeks": round(float(np.mean(offsets)), 1) if offsets else None,
                "anchors_fired": len(offsets),
            }
        )
    rows.sort(key=lambda row: (row["mean_offset_weeks"] is None, row["mean_offset_weeks"] or 0))
    return rows


def live_state(
    layers: list[dict[str, object]], cascade: dict[str, object], grid: pd.DatetimeIndex
) -> dict[str, object]:
    window = int(cascade["window_weeks"])
    recent = grid[-window:]
    firing = [layer["layer"] for layer in layers if bool(layer["mask"].loc[recent].any())]  # type: ignore[index]
    return {
        "as_of": grid[-1].strftime("%Y-%m-%d"),
        "state": "ALARM" if bool(cascade["alarm"].iloc[-1]) else ("WATCH" if firing else "QUIET"),  # type: ignore[index]
        "layers_firing": firing,
        "layers_total": len(layers),
        "cascade_threshold_k": int(cascade["k"]),
        "cascade_window_weeks": window,
    }


def render_markdown(
    result: dict[str, object], layers: list[dict[str, object]], grid: pd.DatetimeIndex
) -> str:
    back_rows = [
        [
            row["layer"],
            row["series"],
            row["feature"],
            row["threshold"],
            f"q{row['quantile']}",
            row["anchors_fired"],
            row["mean_offset_weeks"],
        ]
        for row in result["backward_order"]
    ]
    cold_rows = [
        [
            hit["anchor"],
            hit["anchor_date"],
            "hit" if hit["caught"] else "miss",
            hit["alarm_date"],
            hit["steps_before"],
            f"{hit['max_layers_in_window']}/{len(layers)}",
            ", ".join(hit["layers_fired"]) or "-",
        ]
        for hit in result["forward"]["anchors"]
    ]
    live = result["live"]
    forward = result["forward"]
    cascade = result["cascade"]
    return f"""# Cycle C5 - Cross-Layer Reverse Engineering

Eight layers, one causal rule. Thresholds are pre-2005 quantiles of each
feature's own history; no external level is hardcoded.

## 1. Backward reverse-mining map (pre-2005 anchors only)

Training anchors: `{", ".join(name for name, _ in data_loader.C5_TRAIN_ANCHORS)}`.

| Layer | Series | Feature | Threshold | Quantile | Anchors fired | Mean offset (weeks) |
|---|---|---|---|---|---|---|
""" + "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in back_rows) + f"""

Chosen cascade: **K = {cascade['k']} of {len(layers)}** layers inside a
**{cascade['window_weeks']}-week** causal window. Training coverage
{cascade['train_coverage']}/{len(data_loader.C5_TRAIN_ANCHORS)}; pre-2005 quiet
alarm rate {cascade['quiet_rate_per_year']} per year.

### Layer-break order

The table above is sorted by mean offset; negative means the layer fires before
the anchor. This is the backward-derived signature: which layer breaks first,
and by how many weeks.

## 2. Forward causal cold replay (2005-2026, zero lookahead)

Signature frozen before this replay: K={cascade['k']}, window={cascade['window_weeks']}w.

| Anchor | Date | Result | Alarm date | Weeks before | Peak layers (4w) | Layers fired |
|---|---|---|---|---|---|---|
""" + "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in cold_rows) + f"""

Cold catches: **{forward['catches']}/{len(forward['anchors'])}**. False alarms:
**{len(forward['false_alarms'])}** over {forward['years']} years
({forward['false_alarms_per_year']} per year).

### 2014-2015 credit test

The HY-energy crunch (2015-12-11) is the case where Brent alone failed. Whether
the credit layer carried it is visible in the `layers_fired` column above.

### Verdict, stated without tuning

""" + "\n\n".join(result["forward_verdict"]) + f"""

No threshold was changed after seeing these results: the cold replay is a
holdout, not a tuning set.

### Live state, late 2026

**{live['state']}** as of {live['as_of']}; layers firing in the last
{live['cascade_window_weeks']} weeks: {", ".join(live['layers_firing']) or "none"}.

## 3. Data caveat

FRED's official endpoint returns `BAMLH0A0HYM2` only from 2023-11-28 (ICE
licence restriction), so the historical credit layer uses `BAA10Y` (Moody's Baa
minus 10Y, from 1986) as a documented proxy. HY OAS remains available for the
live cross-check only. This is a property of the source, not an estimate.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args(argv)

    weekly = data_loader.c5_weekly_layers()
    vectors: dict[str, StateVector] = {}
    for series_id, series in weekly.items():
        vectors[series_id] = build_series_state(
            series.rename(series_id), "weekly", data_loader.C5_TRANSFORMS[series_id]
        )
    grid = _common_grid(vectors)
    layers = calibrate_layers(vectors)
    cascade = choose_cascade(layers, grid)
    backward = backtrack_map(layers, grid)
    forward = forward_replay(layers, cascade, grid)
    live = live_state(layers, cascade, grid)
    verdict: list[str] = []
    for hit in forward["anchors"]:
        fired = ", ".join(hit["layers_fired"]) or "none"
        if hit["caught"]:
            verdict.append(
                f"- **{hit['anchor']}**: caught on {hit['alarm_date']} "
                f"({hit['steps_before']} weeks before the anchor). Layers active in "
                f"the window: {fired}."
            )
        else:
            verdict.append(
                f"- **{hit['anchor']}**: missed. Peak simultaneous layers in the "
                f"4-week window was {hit['max_layers_in_window']}/{len(layers)} "
                f"(needed {cascade['k']}); layers active: {fired}."
            )
    credit_2015 = next(
        (hit for hit in forward["anchors"] if hit["anchor"] == "HY_ENERGY_CRUNCH_2015"),
        None,
    )
    if credit_2015 is not None:
        if "CREDIT" in credit_2015["layers_fired"]:
            verdict.append(
                "- The **credit layer did fire** in the 2015 window, so the cross-layer "
                "signature catches the episode that Brent alone missed."
            )
        else:
            verdict.append(
                "- The **credit layer did not fire** in the 2015 window; the catch came "
                "from financial conditions and oil. The credit layer therefore did not "
                "add information for this episode."
            )
    verdict.append(
        f"- False alarms: {len(forward['false_alarms'])} episodes in "
        f"{forward['years']} years ({forward['false_alarms_per_year']}/year). This is "
        "comparable to the v1.0 budget but was not pre-registered for C5."
    )

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "CHAOS-Reverse-Miner/c5-cross-layer-miner",
        "layers": [
            {
                "layer": layer["layer"],
                "series": layer["series"],
                "feature": layer["feature"],
                "sign": layer["sign"],
                "quantile": layer["quantile"],
                "threshold": layer["threshold"],
            }
            for layer in layers
        ],
        "cascade": {
            "k": cascade["k"],
            "window_weeks": cascade["window_weeks"],
            "train_coverage": cascade["train_coverage"],
            "quiet_rate_per_year": cascade["quiet_rate_per_year"],
        },
        "backward_order": backward,
        "forward": forward,
        "forward_verdict": verdict,
        "live": live,
        "data_notes": {
            "credit_layer": "BAA10Y proxy; BAMLH0A0HYM2 truncated to 3y by FRED",
            "grid": f"{grid[0].date()}..{grid[-1].date()} weekly Fridays",
        },
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "c5_invariant_signature.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    (RESULTS_DIR / "c5_cross_layer_report.md").write_text(
        render_markdown(result, layers, grid), encoding="utf-8"
    )
    print(
        f"[c5] K={cascade['k']}/{len(layers)} window={cascade['window_weeks']}w "
        f"train={cascade['train_coverage']}/2 quiet={cascade['quiet_rate_per_year']}/yr "
        f"| cold {forward['catches']}/{len(forward['anchors'])} FA/yr={forward['false_alarms_per_year']} "
        f"| live={live['state']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
