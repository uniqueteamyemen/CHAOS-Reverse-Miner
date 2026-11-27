"""Stage 2 - blind structural-break detector.

Reads ONLY ``data/blind_matrix_2026_09_25.csv``.  The columns are anonymous
(``S_01``..``S_08``); no asset name, weight or external event is used anywhere in
this module or its outputs.

For every column it computes, causally:

* Page-Hinkley upward/downward accumulators on the daily change,
* rolling 60-day OLS slope z-score of the level,
* 252-day drawdown normalised by rolling volatility,
* L1 slope-break magnitude,
* causal rolling quantiles (3-year window, shifted one day) for every statistic.

States: NORMAL, UPWARD_BREAK, DOWNWARD_BREAK, PRE_BREAK_TENSION.
The Blind Co-Fracture Index counts columns in an active break on the same day.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core.detectors import drawdown_features, l1_slope_break, page_hinkley, trend_slope_z

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "blind_matrix_2026_09_25.csv"
RESULTS_DIR = ROOT / "results"
CUTOFF = "2026-09-25"
STAT_START = "2004-01-01"
Q_WINDOW = 756
Q_MIN = 252
COLUMNS = [f"S_{i:02d}" for i in range(1, 9)]
FEATURES = ("ph_up", "ph_down", "drawdown", "drawdown_norm", "trend_slope_z", "l1_slope_break")


def _causal_quantile(series: pd.Series, q: float) -> pd.Series:
    return series.rolling(Q_WINDOW, min_periods=Q_MIN).quantile(q).shift(1)


def _drawdown_norm(level: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """252-day drawdown from the rolling peak, normalised by rolling volatility.

    Uses an absolute distance from the peak instead of a ratio, so columns that
    can print negative levels (indices/spreads) keep a consistent direction.
    """
    peak = level.rolling(252, min_periods=252).max()
    drawdown = level - peak
    volatility = level.diff().rolling(252, min_periods=252).std(ddof=1)
    normalised = drawdown / volatility.replace(0.0, np.nan)
    return drawdown.to_numpy(), normalised.to_numpy()


def compute_features(frame: pd.DataFrame) -> dict[str, dict[str, pd.Series]]:
    features: dict[str, dict[str, pd.Series]] = {}
    for column in COLUMNS:
        level = frame[column].astype(float)
        change = level.diff()
        ph_up, ph_down = page_hinkley(change.fillna(0.0).to_numpy())
        drawdown, drawdown_norm = _drawdown_norm(level)
        slope_break = l1_slope_break(change.fillna(0.0).to_numpy(), "daily")
        trend = trend_slope_z(level, 60, 252)
        features[column] = {
            "ph_up": pd.Series(ph_up, index=frame.index),
            "ph_down": pd.Series(ph_down, index=frame.index),
            "drawdown": pd.Series(drawdown, index=frame.index),
            "drawdown_norm": pd.Series(drawdown_norm, index=frame.index),
            "trend_slope_z": pd.Series(trend, index=frame.index),
            "l1_slope_break": pd.Series(slope_break, index=frame.index),
        }
    return features


def classify(
    features: dict[str, dict[str, pd.Series]], frame: pd.DataFrame
) -> tuple[dict[str, pd.Series], pd.DataFrame]:
    states: dict[str, pd.Series] = {}
    diagnostics: dict[str, dict[str, pd.Series]] = {}
    for column in COLUMNS:
        f = features[column]
        q_up = _causal_quantile(f["ph_up"], 0.98)
        q_trend = _causal_quantile(f["trend_slope_z"], 0.95)
        q_down = _causal_quantile(f["ph_down"], 0.98)
        q_dd = _causal_quantile(f["drawdown_norm"], 0.02)
        q_slope = _causal_quantile(f["l1_slope_break"], 0.98)

        up_core = (f["ph_up"] >= q_up) & (f["trend_slope_z"] >= q_trend)
        down_core = (f["ph_down"] >= q_down) & (
            (f["drawdown_norm"] <= q_dd) | (f["l1_slope_break"] >= q_slope)
        )
        up_tension = (f["ph_up"] >= q_up) | (f["trend_slope_z"] >= q_trend)
        down_tension = (f["ph_down"] >= q_down) | (f["drawdown_norm"] <= q_dd) | (
            f["l1_slope_break"] >= q_slope
        )

        up_ratio = (f["ph_up"] / q_up.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)
        down_ratio = (f["ph_down"] / q_down.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)

        state = pd.Series("NORMAL", index=frame.index, dtype=object)
        state[up_tension | down_tension] = "PRE_BREAK_TENSION"
        state[up_core & ~down_core] = "UPWARD_BREAK"
        state[down_core & ~up_core] = "DOWNWARD_BREAK"
        both = up_core & down_core
        state[both] = np.where(
            up_ratio[both].fillna(-1) >= down_ratio[both].fillna(-1),
            "UPWARD_BREAK",
            "DOWNWARD_BREAK",
        )
        state[frame.index < pd.Timestamp(STAT_START)] = "NORMAL"
        states[column] = state
        diagnostics[column] = {
            "q_up": q_up,
            "q_trend": q_trend,
            "q_down": q_down,
            "q_dd": q_dd,
            "q_slope": q_slope,
            "up_core": up_core,
            "down_core": down_core,
        }
    return states, pd.DataFrame(diagnostics)


def co_fracture(states: dict[str, pd.Series]) -> pd.DataFrame:
    state_frame = pd.DataFrame(states)
    breaks = state_frame.isin(["UPWARD_BREAK", "DOWNWARD_BREAK"]).sum(axis=1)
    tension = (state_frame == "PRE_BREAK_TENSION").sum(axis=1)
    return pd.DataFrame({"co_fracture": breaks, "tension": tension})


def episodes(state: pd.Series, start: str, end: str) -> list[dict[str, object]]:
    window = state[(state.index >= pd.Timestamp(start)) & (state.index <= pd.Timestamp(end))]
    rows: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for date, value in window.items():
        if value == "NORMAL":
            continue
        if (
            current is not None
            and current["state"] == value
            and (date - current["end"]).days <= 5
        ):
            current["end"] = date
            current["days"] = int(current["days"]) + 1
            continue
        if current is not None:
            rows.append(current)
        current = {"state": value, "start": date, "end": date, "days": 1}
    if current is not None:
        rows.append(current)
    return rows


def _snapshot(
    features: dict[str, dict[str, pd.Series]],
    states: dict[str, pd.Series],
    diagnostics: pd.DataFrame,
    index: pd.DatetimeIndex,
) -> dict[str, dict[str, object]]:
    date = index[-1]
    snapshot: dict[str, dict[str, object]] = {}
    for column in COLUMNS:
        f = features[column]
        diag = diagnostics[column]
        snapshot[column] = {
            "state": states[column].loc[date],
            "ph_up": round(float(f["ph_up"].loc[date]), 4),
            "ph_down": round(float(f["ph_down"].loc[date]), 4),
            "drawdown": round(float(f["drawdown"].loc[date]), 4),
            "drawdown_norm": round(float(f["drawdown_norm"].loc[date]), 3),
            "trend_slope_z": round(float(f["trend_slope_z"].loc[date]), 3),
            "l1_slope_break": round(float(f["l1_slope_break"].loc[date]), 3),
            "q_up": round(float(diag["q_up"].loc[date]), 4),
            "q_down": round(float(diag["q_down"].loc[date]), 4),
            "q_dd": round(float(diag["q_dd"].loc[date]), 3),
            "q_slope": round(float(diag["q_slope"].loc[date]), 3),
            "up_core": bool(diag["up_core"].loc[date]),
            "down_core": bool(diag["down_core"].loc[date]),
        }
    return snapshot


def _projection(
    snapshot: dict[str, dict[str, object]],
    cf: pd.DataFrame,
    states: dict[str, pd.Series],
    index: pd.DatetimeIndex,
) -> dict[str, object]:
    date = index[-1]
    recent = index[-20:]
    cf_now = int(cf.loc[date, "co_fracture"])
    cf_recent_mean = float(cf.loc[recent, "co_fracture"].mean())
    cf_slope = float(cf.loc[date, "co_fracture"] - cf.loc[recent[0], "co_fracture"])
    tension_now = int(cf.loc[date, "tension"])

    candidates = []
    for column in COLUMNS:
        info = snapshot[column]
        days_in_break = int(states[column].loc[recent].isin(["UPWARD_BREAK", "DOWNWARD_BREAK"]).sum())
        days_tension = int((states[column].loc[recent] == "PRE_BREAK_TENSION").sum())
        if info["state"] == "UPWARD_BREAK":
            direction = "upside continuation"
            score = 3 + days_in_break / 10
        elif info["state"] == "DOWNWARD_BREAK":
            direction = "downside continuation"
            score = 3 + days_in_break / 10
        elif info["state"] == "PRE_BREAK_TENSION" or days_tension >= 5:
            down_pressure = (
                float(info["drawdown_norm"]) <= float(info["q_dd"])
                or float(info["l1_slope_break"]) >= float(info["q_slope"])
            )
            direction = "downside break risk" if down_pressure else "upside break risk"
            score = 1 + days_tension / 10
        else:
            continue
        candidates.append(
            {
                "series": column,
                "state": info["state"],
                "direction": direction,
                "score": round(score, 2),
                "days_in_break_20d": days_in_break,
                "days_tension_20d": days_tension,
                "drawdown_norm": info["drawdown_norm"],
                "l1_slope_break": info["l1_slope_break"],
            }
        )
    candidates.sort(key=lambda item: -item["score"])

    if cf_now >= 4 or cf_recent_mean >= 3:
        regime = "elevated co-fracture regime"
    elif cf_now >= 2 or cf_recent_mean >= 1.5:
        regime = "moderate co-fracture regime"
    elif cf_now >= 1 or tension_now >= 2:
        regime = "localised stress"
    else:
        regime = "calm"

    outlook = []
    for month, label in (
        (10, "October 2026"),
        (11, "November 2026"),
        (12, "December 2026"),
    ):
        if month == 10:
            base = regime
            cols = [item["series"] for item in candidates[:3]]
        elif month == 11:
            base = regime if cf_slope >= 0 else "stress decaying" if cf_now > 0 else "calm"
            cols = [item["series"] for item in candidates[:3] if item["score"] >= 1.2]
        else:
            base = "calm" if cf_slope < 0 and cf_now <= 1 else regime
            cols = [item["series"] for item in candidates[:2] if item["score"] >= 2]
        outlook.append(
            {
                "month": label,
                "structural_regime": base,
                "candidate_columns": cols,
                "basis": "conditional on the 2026-09-25 stress state and its 20-day slope",
            }
        )

    return {
        "as_of": date.strftime("%Y-%m-%d"),
        "co_fracture": cf_now,
        "tension": tension_now,
        "co_fracture_20d_mean": round(cf_recent_mean, 2),
        "co_fracture_20d_change": round(cf_slope, 0),
        "regime": regime,
        "candidates": candidates,
        "monthly_outlook": outlook,
        "caveat": "structural scenario conditional on pre-cutoff data; not a forecast or a probability",
    }


def _table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def render(payload: dict[str, object]) -> str:
    snapshot = payload["snapshot"]
    q3 = payload["q3_2026"]
    projection = payload["projection"]

    summary_rows = [
        [
            column,
            snapshot[column]["state"],
            snapshot[column]["ph_up"],
            snapshot[column]["ph_down"],
            snapshot[column]["drawdown_norm"],
            snapshot[column]["trend_slope_z"],
            snapshot[column]["l1_slope_break"],
        ]
        for column in COLUMNS
    ]
    q3_rows = []
    for column, items in q3["episodes"].items():
        for item in items:
            q3_rows.append(
                [
                    column,
                    item["state"],
                    item["start"],
                    item["end"],
                    item["days"],
                ]
            )
    candidate_rows = [
        [
            item["series"],
            item["state"],
            item["direction"],
            item["days_in_break_20d"],
            item["days_tension_20d"],
        ]
        for item in projection["candidates"][:5]
    ]
    outlook_rows = [
        [item["month"], item["structural_regime"], ", ".join(item["candidate_columns"]) or "-"]
        for item in projection["monthly_outlook"]
    ]

    return f"""# Blind Structural-Break Test - 2026-09-25

All columns are anonymous. The detector read only
`data/blind_matrix_2026_09_25.csv`; no external news, prices or events after the
cutoff were consulted, and no asset identity appears in this report.

## 1. State at the 2026-09-25 close

{_table(["Column", "State", "ph_up", "ph_down", "drawdown_norm", "trend_slope_z", "l1_slope_break"], summary_rows)}

Blind Co-Fracture Index: **{projection['co_fracture']}** columns in an active
break, **{projection['tension']}** in pre-break tension.

## 2. Q3 2026 timeline (2026-07-01 to 2026-09-25)

{_table(["Column", "State", "Start", "End", "Days"], q3_rows) if q3_rows else "No non-normal episodes in the quarter."}

## 3. Structural reading for October-December 2026

Regime at the cutoff: **{projection['regime']}**
(co-fracture {projection['co_fracture']}, 20-day mean
{projection['co_fracture_20d_mean']}, 20-day change
{projection['co_fracture_20d_change']}).

{_table(["Column", "State", "Direction", "Break days (20d)", "Tension days (20d)"], candidate_rows) if candidate_rows else "No stressed candidates."}

{_table(["Month", "Structural regime", "Candidate columns"], outlook_rows)}

**Caveat:** {projection['caveat']}. The months are conditional readings of the
cutoff stress state and its recent slope, not predictions with probabilities.

## 4. Method

- Page-Hinkley accumulators on daily changes (upward and downward).
- 60-day OLS slope z-score on the level.
- 252-day drawdown normalised by rolling volatility.
- L1 slope-break magnitude.
- Every threshold is a **causal** rolling 3-year quantile shifted one day
  (Q98/Q95 upward, Q02 downward), starting from 2004.
- State = UPWARD_BREAK when the upward pair fires, DOWNWARD_BREAK when the
  downward pair fires, PRE_BREAK_TENSION when only part of a pair fires.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)

    if not DATA_PATH.exists():
        raise SystemExit(f"{DATA_PATH} not found - run scripts/build_blind_matrix.py first")
    frame = pd.read_csv(DATA_PATH, parse_dates=["date"]).set_index("date").sort_index()
    if frame.index.max() > pd.Timestamp(CUTOFF):
        raise SystemExit("blind matrix violates the cutoff")
    missing = [column for column in COLUMNS if column not in frame.columns]
    if missing:
        raise SystemExit(f"missing columns: {missing}")

    features = compute_features(frame)
    states, diagnostics = classify(features, frame)
    cf = co_fracture(states)
    snapshot = _snapshot(features, states, diagnostics, frame.index)

    q3_episodes = {
        column: episodes(states[column], "2026-07-01", CUTOFF) for column in COLUMNS
    }
    projection = _projection(snapshot, cf, states, frame.index)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "CHAOS-Reverse-Miner/blind-break-detector",
        "cutoff": CUTOFF,
        "rows": int(len(frame)),
        "columns": COLUMNS,
        "snapshot": snapshot,
        "co_fracture": {
            "at_cutoff": int(cf.iloc[-1]["co_fracture"]),
            "tension_at_cutoff": int(cf.iloc[-1]["tension"]),
            "max_q3": int(cf.loc["2026-07-01":CUTOFF, "co_fracture"].max()),
        },
        "q3_2026": {
            "start": "2026-07-01",
            "end": CUTOFF,
            "episodes": {
                column: [
                    {
                        "state": item["state"],
                        "start": item["start"].strftime("%Y-%m-%d"),
                        "end": item["end"].strftime("%Y-%m-%d"),
                        "days": item["days"],
                    }
                    for item in items
                ]
                for column, items in q3_episodes.items()
            },
        },
        "projection": projection,
        "disclosure": "no asset names, no post-cutoff data, no external events",
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "blind_test_2026_09_25_report.md").write_text(render(payload), encoding="utf-8")
    (RESULTS_DIR / "blind_test_2026_09_25.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    stress = [column for column in COLUMNS if snapshot[column]["state"] != "NORMAL"]
    print(
        f"[blind] cutoff={CUTOFF} co-fracture={payload['co_fracture']['at_cutoff']} "
        f"tension={payload['co_fracture']['tension_at_cutoff']} active={stress or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
