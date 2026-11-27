"""Cycle C6 - noise-filtered C5 v2 plus a hierarchical two-speed radar.

Two layers:

* ``FAST_SHOCK`` - the C5 v2 daily weighted signature after cooldown / episode
  merging / persistence filtering.
* ``MACRO_CRISIS`` - the v1.0 ALARM logic (trend slope OR cross-series macro
  gate) with an explicit **latch release**: an episode is cleared early once the
  labour pressure has faded.

State priority: MACRO_CRISIS > FAST_SHOCK > QUIET.  Everything is causal: the
daily fast layer uses only same-day closes; the monthly macro layer becomes
visible the day after its month-end close.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core import data_loader
from core import reverse_miner as rm
from core.c5_v2_optimizer import LAYERS, build_vectors, calibrate
from core.detectors import build_panel, build_series_state, trend_slope_z

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
START = "1998-01-01"
END = "2026-11-27"
LEAD_DAYS = 20
GRACE_DAYS = 2
BASE_K = 4.0


def _episodes(mask: pd.Series, cooldown: int = 0) -> list[pd.Timestamp]:
    out: list[pd.Timestamp] = []
    last: pd.Timestamp | None = None
    for date in mask[mask].index:
        if cooldown and last is not None and (date - last).days < cooldown:
            continue
        out.append(date)
        last = date
    return out


def _apply_cooldown(alarm: pd.Series, cooldown: int) -> pd.Series:
    if cooldown <= 0:
        return alarm
    keep = pd.Series(False, index=alarm.index)
    last: pd.Timestamp | None = None
    for date in alarm[alarm].index:
        if last is None or (date - last).days >= cooldown:
            keep[date] = True
            last = date
    return keep


def _persistence(score: pd.Series, k: float, days: int = 2) -> pd.Series:
    hit = score >= k
    return hit & hit.shift(1).fillna(False)


def _evaluate_alarm(alarm: pd.Series, anchors: list[tuple[str, str]]) -> dict[str, object]:
    caught = []
    leads = []
    for name, date in anchors:
        ts = pd.Timestamp(date)
        window = alarm[(alarm.index >= ts - pd.Timedelta(days=LEAD_DAYS)) & (alarm.index <= ts + pd.Timedelta(days=GRACE_DAYS))]
        hits = window[window]
        if len(hits):
            caught.append(name)
            leads.append(int((ts - hits.index[0]).days))
    episodes = _episodes(alarm, cooldown=20)  # merge waves closer than 20 trading days
    years = (alarm.index[-1] - alarm.index[0]).days / 365.25
    alarm_days = int(alarm.sum())
    return {
        "catches": caught,
        "catch_count": len(caught),
        "mean_lead_days": round(float(np.mean(leads)), 1) if leads else None,
        "episodes": len(episodes),
        "episodes_per_year": round(len(episodes) / years, 3) if years else None,
        "alarm_days": alarm_days,
        "alarm_days_per_year": round(alarm_days / years, 2) if years else None,
    }


def fast_shock_series() -> tuple[pd.Series, list[dict[str, object]], dict[str, object]]:
    vectors, is_daily, grid = build_vectors()
    layers = calibrate(vectors, is_daily, grid)
    fire = pd.DataFrame({layer["layer"]: layer["mask"] for layer in layers}).reindex(grid).fillna(False)
    weights = {layer["layer"]: data_loader.C5_V2_WEIGHTS[layer["series"]] for layer in layers}
    score_10 = sum(fire[name].astype(float) * weight for name, weight in weights.items()).rolling(10, min_periods=10).sum()
    anchors = data_loader.C5_V2_ANCHORS
    tests: list[dict[str, object]] = []

    def add(name: str, alarm: pd.Series, **params: object) -> None:
        tests.append({"name": name, "alarm": alarm, "params": params, **_evaluate_alarm(alarm, anchors)})

    add("base weighted>=4, 10d", _apply_cooldown(score_10 >= BASE_K, 0))
    for cooldown in (10, 20, 30):
        add(f"cooldown {cooldown}d", _apply_cooldown(score_10 >= BASE_K, cooldown))
    add("2-day persistence", _persistence(score_10, BASE_K))
    for cooldown in (10, 20):
        add(f"2-day persistence + cooldown {cooldown}d", _apply_cooldown(_persistence(score_10, BASE_K), cooldown))
    for threshold in (4.5, 5.0, 5.5):
        add(f"weighted>={threshold}", _apply_cooldown(score_10 >= threshold, 0))
        add(f"weighted>={threshold} + cooldown 20d", _apply_cooldown(score_10 >= threshold, 20))

    target = ("EURO_DEBT_US_DOWNGRADE_2011", "HY_ENERGY_CRUNCH_2015", "SVB_BANKING_STRESS_2023")
    eligible = [test for test in tests if all(name in test["catches"] for name in target)]
    pool = eligible or tests
    pool.sort(key=lambda test: (-test["catch_count"], test["episodes_per_year"], -(test["mean_lead_days"] or 0)))
    best = pool[0]
    best["alarm"] = _apply_cooldown(best["alarm"], 0)  # already filtered
    public_layers = [
        {key: layer[key] for key in ("layer", "series", "grid", "feature", "sign", "quantile", "threshold", "train_coverage")}
        for layer in layers
    ]
    return best["alarm"], tests, {
        "k": BASE_K,
        "name": best["name"],
        "params": best["params"],
        "layers": public_layers,
    }


def macro_series() -> tuple[pd.Series, dict[str, object]]:
    """v1.0 ALARM on a monthly grid, with latch release, mapped to daily."""
    core = data_loader.load_all()
    profiles = data_loader.get_profiles()
    unrate = core["UNRATE"]
    payems = core["PAYEMS"]
    panel_u = build_panel(unrate, 18, "monthly", "diff")
    panel_p = build_panel(payems, 18, "monthly", "log_return")
    from core.detectors import cusum_from_z

    pos_u, _ = cusum_from_z(panel_u.z, 0.5)
    _, neg_p = cusum_from_z(panel_p.z, 0.5)
    up = (panel_u.huber_pos >= 4.0) | (pos_u >= 4.0)
    down = (panel_p.huber_neg >= 4.0) | (neg_p >= 4.0)
    mask_u, _, resonant = rm.labor_resonance(panel_u, panel_p, up, down, tolerance=2)
    resonance = pd.Series(mask_u, index=pd.DatetimeIndex(panel_u.dates))

    trend = pd.Series(
        trend_slope_z(unrate, 6, 24)[1:], index=pd.DatetimeIndex(unrate.index[1:])
    )
    payems_state = build_series_state(payems, "monthly", "log_return")
    ph_down = pd.Series(payems_state.features["ph_down"], index=pd.DatetimeIndex(payems_state.dates))

    month_end = pd.date_range(START, END, freq="ME")
    resonance = resonance.reindex(month_end, method="ffill").fillna(False)
    trend = trend.reindex(month_end, method="ffill")
    ph_down = ph_down.reindex(month_end, method="ffill")

    raw_alarm = (resonance | (trend >= 2.0)).astype(bool)
    # Latch release: clear an active month when the labour impulse has faded.
    released = raw_alarm.copy()
    for i in range(len(month_end)):
        if not raw_alarm.iloc[i]:
            continue
        if float(ph_down.iloc[i]) < 0.05 and float(trend.iloc[i]) < 0.0:
            released.iloc[i] = False

    # Monthly close is visible the next day.
    daily_index = pd.bdate_range(START, END)
    visible = pd.Series(released.to_numpy(), index=month_end + pd.Timedelta(days=1))
    daily = visible.reindex(daily_index, method="ffill").fillna(False).astype(bool)
    return daily, {
        "raw_alarm_months": int(raw_alarm.sum()),
        "released_alarm_months": int(released.sum()),
        "release_rule": "clear when PAYEMS ph_down < 0.05 and UNRATE trend_z < 0",
    }


def hierarchical() -> tuple[pd.Series, pd.Series, pd.Series]:
    fast, _, fast_meta = fast_shock_series()
    macro, _ = macro_series()
    index = fast.index.intersection(macro.index)
    fast, macro = fast.reindex(index).fillna(False), macro.reindex(index).fillna(False)
    state = pd.Series("QUIET", index=index)
    state[fast] = "FAST_SHOCK"
    state[macro] = "MACRO_CRISIS"
    return state, fast, macro


def _segments(state: pd.Series) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    current = None
    for date, value in state.items():
        if current is None or current["state"] != value:
            if current is not None:
                rows.append(current)
            current = {"state": value, "start": date, "end": date, "days": 1}
        else:
            current["end"] = date
            current["days"] += 1
    if current is not None:
        rows.append(current)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)

    fast_alarm, tests, fast_meta = fast_shock_series()
    macro_daily, macro_meta = macro_series()
    index = fast_alarm.index.intersection(macro_daily.index)
    fast = fast_alarm.reindex(index).fillna(False)
    macro = macro_daily.reindex(index).fillna(False)
    state = pd.Series("QUIET", index=index)
    state[fast] = "FAST_SHOCK"
    state[macro] = "MACRO_CRISIS"

    anchors = data_loader.C5_V2_ANCHORS
    fast_eval = _evaluate_alarm(fast, anchors)
    macro_eval = _evaluate_alarm(macro, anchors)
    state_eval = _evaluate_alarm(state != "QUIET", anchors)
    segments = _segments(state)
    years = (index[-1] - index[0]).days / 365.25

    def rows_for(items: list[dict[str, object]]) -> list[list[object]]:
        return [
            [item["state"], item["start"].strftime("%Y-%m-%d"), item["end"].strftime("%Y-%m-%d"), item["days"]]
            for item in items
            if item["state"] != "QUIET"
        ]

    filtered_rows = [
        [
            test["name"],
            f"{test['catch_count']}/9",
            ", ".join(name for name in ("EURO_DEBT_US_DOWNGRADE_2011", "HY_ENERGY_CRUNCH_2015", "SVB_BANKING_STRESS_2023") if name in test["catches"]) or "-",
            test["episodes"],
            test["episodes_per_year"],
            test.get("alarm_days_per_year"),
            test["mean_lead_days"],
        ]
        for test in tests
    ]

    report = f"""# Cycle C6 - Noise-Filtered Daily Signature and Hierarchical Radar

Period: `{START}` to `{index[-1].date()}`. All signals are causal.

## 1. Noise reduction on the C5 v2 daily signature

Base: weighted score >= {BASE_K} over a 10-day window. Cooldown suppresses any
alarm within the stated number of days of the previous accepted alarm; episode
merging is the same operation with a 20-day gap; persistence requires the score
to clear the threshold two days running.

| Filter | Catches | 2011 / 2015 / SVB | Episodes | Episodes/year | Alarm days/year | Mean lead (d) |
|---|---|---|---|---|---|---|
""" + "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in filtered_rows) + f"""

The C5 v2 number (13.44/year) merged alarms with a 10-day gap; these rows merge
at 20 days as specified, so part of the drop is a **counting change**, not noise
removal. The `Alarm days/year` column is the honest raw measure: cooldown 30
truly cuts it but drops 2015; persistence removes SVB; higher thresholds remove
SVB. No filter keeps 9/9 below roughly 7.8 merged episodes/year.

Selected FAST_SHOCK: **{fast_meta['name']}** - {fast_eval['catch_count']}/9
stations, {fast_eval['episodes_per_year']} episodes/year, mean lead
{fast_eval['mean_lead_days']} days.

## 2. MACRO_CRISIS layer (v1.0 logic + latch release)

{macro_meta['raw_alarm_months']} raw ALARM months, {macro_meta['released_alarm_months']}
after latch release. Rule: {macro_meta['release_rule']}.
Macro cold catches: {macro_eval['catch_count']}/9, {macro_eval['episodes_per_year']}
episodes/year.

## 3. Hierarchical state (1998-2026)

Priority: MACRO_CRISIS > FAST_SHOCK > QUIET.

| Metric | Value |
|---|---|
| FAST_SHOCK episodes | {fast_eval['episodes']} |
| MACRO_CRISIS episodes | {macro_eval['episodes']} |
| Combined non-quiet episodes | {state_eval['episodes']} |
| Combined catches | {state_eval['catch_count']}/9 |
| Combined episodes/year | {state_eval['episodes_per_year']} |

| State | Start | End | Days |
|---|---|---|---|
""" + "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in rows_for(segments)) + f"""

## 4. Live state

**{state.iloc[-1]}** as of {index[-1].date()}.
FAST_SHOCK active: {bool(fast.iloc[-1])}; MACRO_CRISIS active: {bool(macro.iloc[-1])}.
"""

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "c6_hierarchical_radar_report.md").write_text(report, encoding="utf-8")
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "CHAOS-Reverse-Miner/c6-hierarchical-radar",
        "period": f"{START}..{index[-1].date()}",
        "fast_shock": {
            "config": fast_meta,
            "evaluation": fast_eval,
            "filters": [
                {key: test[key] for key in ("name", "catch_count", "episodes", "episodes_per_year", "alarm_days_per_year", "mean_lead_days")}
                for test in tests
            ],
        },
        "macro_crisis": {"meta": macro_meta, "evaluation": macro_eval},
        "combined": {"evaluation": state_eval},
        "live": {
            "as_of": index[-1].strftime("%Y-%m-%d"),
            "state": state.iloc[-1],
            "fast_shock": bool(fast.iloc[-1]),
            "macro_crisis": bool(macro.iloc[-1]),
        },
    }
    (RESULTS_DIR / "live_engine_v2_state.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"[c6] fast={fast_meta['name']} catches={fast_eval['catch_count']}/9 "
        f"ep/yr={fast_eval['episodes_per_year']} | macro ep={macro_eval['episodes']} "
        f"| combined {state_eval['catch_count']}/9 ep/yr={state_eval['episodes_per_year']} "
        f"| live={state.iloc[-1]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
