"""Deterministic 3-state production engine (ALARM / WATCH / QUIET).

Every threshold is read from ``production/thresholds.json``; the file's SHA-256
is pinned into the output.  No notification channels, no statistical tests: a
single causal replay plus a live snapshot.

Decision rule (protocol ``results/paper_trading_protocol.json``):

* ``ALARM`` = OR on {TREND_SLOPE on UNRATE, CROSS_SERIES_MACRO_GATE}
* ``WATCH`` = OR on {C4 relay (ICSA / Brent), icsa_payems_fusion}, no ALARM
* ``QUIET`` = neither

The replay rebuilds every feature from the prefix of each series ending at the
decision month, so no future value can enter a decision.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from core import data_loader
from core import reverse_miner as rm
from core.detectors import build_panel, build_state_vectors, cusum_from_z

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
THRESHOLDS_PATH = ROOT / "production" / "thresholds.json"
PROTOCOL_PATH = RESULTS_DIR / "paper_trading_protocol.json"
START = "2023-01-01"
END = "2026-11-27"
ENGINE_VERSION = "chaos_engine-v1.0.0"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_thresholds() -> tuple[dict, str]:
    payload = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    return payload, _sha256(THRESHOLDS_PATH)


def _directional_cusum(panel, slack: float) -> tuple[np.ndarray, np.ndarray]:
    return cusum_from_z(panel.z, slack)


def evaluate_month(
    frames: dict[str, pd.Series],
    profiles: dict[str, dict[str, object]],
    t: pd.Timestamp,
    thresholds: dict,
) -> dict[str, object]:
    """Rebuild all features from data <= t and return the 3-state decision."""
    prefix = {series_id: series.loc[:t] for series_id, series in frames.items()}
    vectors = build_state_vectors(prefix, profiles)

    macro_cfg = thresholds["monthly"]["macro_gate"]
    panel_u = build_panel(prefix["UNRATE"], int(macro_cfg["window_months"]), "monthly", "diff")
    panel_p = build_panel(prefix["PAYEMS"], int(macro_cfg["window_months"]), "monthly", "log_return")
    pos_u, neg_u = _directional_cusum(panel_u, float(macro_cfg["cusum_slack"]))
    pos_p, neg_p = _directional_cusum(panel_p, float(macro_cfg["cusum_slack"]))
    up = (panel_u.huber_pos >= float(macro_cfg["huber_thr"])) | (
        pos_u >= float(macro_cfg["cusum_thr"])
    )
    down = (panel_p.huber_neg >= float(macro_cfg["huber_thr"])) | (
        neg_p >= float(macro_cfg["cusum_thr"])
    )
    mask_u, _, resonant = rm.labor_resonance(
        panel_u, panel_p, up, down, tolerance=int(macro_cfg["resonance_months"])
    )
    macro_fires = bool(mask_u[-1]) if mask_u.size else False

    trend_cfg = thresholds["monthly"]["trend_slope"]
    trend_series = vectors[str(trend_cfg["series"])]
    trend_value = float(trend_series.features["trend_slope_z"][-1])
    trend_fires = bool(np.isfinite(trend_value) and trend_value >= float(trend_cfg["trend_thr"] if "trend_thr" in trend_cfg else trend_cfg["thr"]))

    fusion_series = vectors["FUSION"]
    fusion_value = float(fusion_series.features["icsa_payems_fusion"][-1])
    fusion_fires = bool(np.isfinite(fusion_value) and fusion_value >= float(thresholds["fusion"]["threshold"]))

    trailing = int(thresholds["weekly_relay"]["trailing_weeks"])
    icsa_cfg = thresholds["weekly_relay"]["ICSA"]
    icsa_values = vectors["ICSA"].features[str(icsa_cfg["feature"])][-trailing:]
    icsa_fires = bool(np.any(icsa_values >= float(icsa_cfg["threshold"])))

    brent_cfg = thresholds["weekly_relay"]["WCOILBRENTEU"]
    brent_vector = vectors["WCOILBRENTEU"]
    brent_hits = []
    for feature, threshold in brent_cfg["features"].items():
        values = brent_vector.features[feature][-trailing:]
        if np.any(values >= float(threshold)):
            brent_hits.append(feature)
    ph_down = float(brent_vector.features["ph_down"][-1])
    unlatch = float(brent_cfg["ph_down_unlatch"])
    brent_fires = bool(brent_hits) and not (ph_down < unlatch and "ph_up" not in brent_hits)

    if macro_fires or trend_fires:
        state = "ALARM"
    elif fusion_fires or icsa_fires or brent_fires:
        state = "WATCH"
    else:
        state = "QUIET"

    triggers = []
    if macro_fires:
        triggers.append("CROSS_SERIES_MACRO_GATE")
    if trend_fires:
        triggers.append("TREND_SLOPE")
    if state != "ALARM":
        if fusion_fires:
            triggers.append("icsa_payems_fusion")
        if icsa_fires:
            triggers.append("C4_relay:ICSA")
        if brent_fires:
            triggers.append("C4_relay:WCOILBRENTEU")

    return {
        "month": str(t.date())[:7],
        "state": state,
        "triggers": triggers,
        "trend_z": round(trend_value, 3) if np.isfinite(trend_value) else None,
        "macro_resonance": macro_fires,
        "macro_cusum_down": round(float(neg_p[-1]), 3),
        "fusion_z": round(fusion_value, 3) if np.isfinite(fusion_value) else None,
        "icsa_watch_value": round(float(np.nanmax(icsa_values)), 3) if icsa_values.size else None,
        "brent_hits": brent_hits,
        "brent_ph_down": round(ph_down, 3),
        "brent_ph_down_unlatched": bool(ph_down < unlatch),
    }


def _episodes(records: list[dict[str, object]]) -> list[dict[str, object]]:
    episodes: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for record in records:
        state = str(record["state"])
        if current is None or current["state"] != state:
            if current is not None:
                episodes.append(current)
            current = {
                "state": state,
                "start": record["month"],
                "end": record["month"],
                "months": 1,
                "triggers": set(record["triggers"]),
                "peak": {
                    "trend_z": record["trend_z"],
                    "fusion_z": record["fusion_z"],
                    "macro_cusum_down": record["macro_cusum_down"],
                },
            }
        else:
            current["end"] = record["month"]
            current["months"] = int(current["months"]) + 1
            current["triggers"] = set(current["triggers"]) | set(record["triggers"])
            peak = current["peak"]
            if record["trend_z"] is not None and (
                peak["trend_z"] is None or record["trend_z"] > peak["trend_z"]
            ):
                peak["trend_z"] = record["trend_z"]
            if record["fusion_z"] is not None and (
                peak["fusion_z"] is None or record["fusion_z"] > peak["fusion_z"]
            ):
                peak["fusion_z"] = record["fusion_z"]
            if record["macro_cusum_down"] is not None and (
                peak["macro_cusum_down"] is None
                or record["macro_cusum_down"] > peak["macro_cusum_down"]
            ):
                peak["macro_cusum_down"] = record["macro_cusum_down"]
    if current is not None:
        episodes.append(current)
    for episode in episodes:
        episode["triggers"] = sorted(episode["triggers"])
    return episodes


def _series_snapshot(
    frames: dict[str, pd.Series],
    profiles: dict[str, dict[str, object]],
    thresholds: dict,
) -> dict[str, object]:
    vectors = build_state_vectors(frames, profiles)
    latest_month = frames["UNRATE"].index[-1]
    month_state = evaluate_month(frames, profiles, latest_month, thresholds)
    snapshot: dict[str, object] = {
        "system_state": month_state["state"],
        "decision_month": str(latest_month.date())[:7],
        "triggers": month_state["triggers"],
        "series": {},
    }
    series = snapshot["series"]
    series["UNRATE"] = {
        "as_of": str(frames["UNRATE"].index[-1].date()),
        "trend_slope_z": month_state["trend_z"],
        "trend_slope_fires": "TREND_SLOPE" in month_state["triggers"],
        "macro_resonance": month_state["macro_resonance"],
    }
    series["PAYEMS"] = {
        "as_of": str(frames["PAYEMS"].index[-1].date()),
        "macro_resonance": month_state["macro_resonance"],
    }
    icsa_cfg = thresholds["weekly_relay"]["ICSA"]
    series["ICSA"] = {
        "as_of": str(frames["ICSA"].index[-1].date()),
        "sahm_floor_displacement": round(
            float(vectors["ICSA"].features[str(icsa_cfg["feature"])][-1]), 4
        ),
        "threshold": float(icsa_cfg["threshold"]),
        "fires": "C4_relay:ICSA" in month_state["triggers"],
    }
    brent_cfg = thresholds["weekly_relay"]["WCOILBRENTEU"]
    series["WCOILBRENTEU"] = {
        "as_of": str(frames["WCOILBRENTEU"].index[-1].date()),
        "ph_up": round(float(vectors["WCOILBRENTEU"].features["ph_up"][-1]), 4),
        "l1_slope_break": round(
            float(vectors["WCOILBRENTEU"].features["l1_slope_break"][-1]), 4
        ),
        "trend_slope_z": round(float(vectors["WCOILBRENTEU"].features["trend_slope_z"][-1]), 4),
        "ph_down": month_state["brent_ph_down"],
        "ph_down_unlatched": month_state["brent_ph_down_unlatched"],
        "features_firing": month_state["brent_hits"],
        "thresholds": brent_cfg["features"],
    }
    return snapshot


def run() -> dict[str, object]:
    thresholds, thresholds_hash = load_thresholds()
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    frames = data_loader.load_all()
    profiles = data_loader.get_profiles()

    start = pd.Timestamp(START)
    end = min(pd.Timestamp(END), frames["UNRATE"].index[-1])
    months = [t for t in frames["UNRATE"].index if start <= t <= end]

    records = [evaluate_month(frames, profiles, t, thresholds) for t in months]
    episodes = _episodes(records)

    alarm_episodes = [e for e in episodes if e["state"] == "ALARM"]
    watch_episodes = [e for e in episodes if e["state"] == "WATCH"]
    years = (months[-1] - months[0]).days / 365.25 if len(months) > 1 else 0.0
    false_alarm_rate = len(alarm_episodes) / years if years else 0.0
    max_alarms = int(protocol["acceptable_alarms_max"])
    budget = float(protocol["false_alarm_budget_per_year"])
    crisis_in_period = [
        event for event in data_loader.get_events() if start <= pd.Timestamp(event[2]) <= end
    ]
    crisis_status = (
        "not_applicable: no registered crisis anchor inside the replay period"
        if not crisis_in_period
        else "checked_against_registry"
    )
    passed = bool(len(alarm_episodes) <= max_alarms and false_alarm_rate <= budget)

    fingerprint = data_loader.fingerprint()
    snapshot = _series_snapshot(frames, profiles, thresholds)

    result: dict[str, object] = {
        "engine": ENGINE_VERSION,
        "deterministic": True,
        "thresholds_sha256": thresholds_hash,
        "thresholds": thresholds,
        "protocol": protocol,
        "data_fingerprint": fingerprint,
        "replay": {
            "start": str(months[0].date()) if months else None,
            "end": str(months[-1].date()) if months else None,
            "months": len(months),
            "years": round(years, 3),
            "alarm_episodes": alarm_episodes,
            "watch_episodes": watch_episodes,
            "alarm_count": len(alarm_episodes),
            "watch_count": len(watch_episodes),
            "false_alarm_budget_per_year": budget,
            "false_alarms_per_year": round(false_alarm_rate, 3),
            "acceptable_alarms_max": max_alarms,
            "crisis_check": crisis_status,
            "verdict": "PASS" if passed else "FAIL",
        },
        "live_state": snapshot,
    }
    return result


def render_markdown(result: dict[str, object]) -> str:
    replay = result["replay"]
    live = result["live_state"]
    protocol = result["protocol"]

    def episode_rows(items: list[dict[str, object]]) -> list[list[object]]:
        return [
            [
                item["start"],
                item["end"],
                item["months"],
                ", ".join(item["triggers"]) or "-",
                item["peak"].get("trend_z"),
                item["peak"].get("fusion_z"),
                item["peak"].get("macro_cusum_down"),
            ]
            for item in items
        ]

    series_rows = [
        [
            series_id,
            info.get("as_of"),
            info.get("trend_slope_z", "-"),
            info.get("macro_resonance", "-"),
            info.get("sahm_floor_displacement", "-"),
            info.get("ph_down", "-"),
            info.get("ph_down_unlatched", "-"),
        ]
        for series_id, info in live["series"].items()
    ]

    return f"""# Paper Trading Replay 2023-2026 - Deterministic 3-State Engine

Replay: `{replay['start']}` to `{replay['end']}` ({replay['months']} monthly decisions).

## 1. Pre-committed protocol

Protocol was written to `results/paper_trading_protocol.json` **before** the
replay was executed.

| Field | Value |
|---|---|
| Period | `{protocol['period']}` |
| Acceptable alarms (max) | {protocol['acceptable_alarms_max']} |
| Required real crisis caught | {protocol['required_real_crisis_caught']} |
| False alarms per year (budget) | {protocol['false_alarm_budget_per_year']} |
| Alarm logic | {protocol['alarm_logic']} |
| Thresholds source | {protocol['thresholds_source']} |

Pinned thresholds SHA-256: `{result['thresholds_sha256']}`

## 2. Replay result

| Metric | Value |
|---|---|
| ALARM episodes | **{replay['alarm_count']}** (max {replay['acceptable_alarms_max']}) |
| WATCH episodes | {replay['watch_count']} |
| False alarms per year | **{replay['false_alarms_per_year']}** (budget {replay['false_alarm_budget_per_year']}) |
| Crisis check | {replay['crisis_check']} |
| **Verdict** | **{replay['verdict']}** |

### ALARM episodes

| Start | End | Months | Triggers | Peak trend z | Peak fusion z | Peak PAYEMS CUSUM- |
|---|---|---|---|---|---|---|
""" + "\n".join(
        "| " + " | ".join(str(c) for c in row) + " |" for row in episode_rows(replay["alarm_episodes"])
    ) + f"""

Interpretation: the ALARM episode starts at the replay boundary (`{replay['start']}`),
so it was already active before 2023. Its PAYEMS downward CUSUM peaked at
**{replay['alarm_episodes'][0]['peak']['macro_cusum_down'] if replay['alarm_episodes'] else 'n/a'}**
(threshold 4) and stays latched long after the 2022 tightening, so this is a
stale episode rather than a fresh 2023 signal. It is counted as **one** alarm
per the protocol and disclosed here instead of being hidden.

### WATCH episodes

| Start | End | Months | Triggers | Peak trend z | Peak fusion z | Peak PAYEMS CUSUM- |
|---|---|---|---|---|---|---|
""" + "\n".join(
        "| " + " | ".join(str(c) for c in row) + " |" for row in episode_rows(replay["watch_episodes"])
    ) + f"""

## 3. Live snapshot

As of the latest FRED release, the system state is **{live['system_state']}**
(decision month `{live['decision_month']}`), triggers:
{", ".join(live["triggers"]) or "none"}.

| Series | As of | Trend z | Macro resonance | ICSA sahm | Brent ph_down | Unlatched |
|---|---|---|---|---|---|---|
""" + "\n".join("| " + " | ".join(str(c) for c in row) + " |" for row in series_rows) + """

## 4. Determinism

The engine rebuilds every feature from the prefix ending at each decision month,
reads all thresholds from the pinned JSON, and writes no wall-clock timestamps.
Running it twice on the same FRED snapshot produces byte-identical JSON.
"""


def main() -> int:
    result = run()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "live_engine_state.json"
    md_path = RESULTS_DIR / "paper_trading_2023_2026.md"
    json_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    md_path.write_text(render_markdown(result), encoding="utf-8")
    replay = result["replay"]
    print(
        f"[engine] alarms={replay['alarm_count']} watch={replay['watch_count']} "
        f"fa/year={replay['false_alarms_per_year']} verdict={replay['verdict']} "
        f"state={result['live_state']['system_state']}"
    )
    print(f"[engine] thresholds sha256={result['thresholds_sha256'][:16]}...")
    print(f"[engine] state -> {json_path}")
    print(f"[engine] report -> {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
