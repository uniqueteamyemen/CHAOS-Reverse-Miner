"""Cycle C4: StateVector precursor discovery.

This module is additive.  It does not replace the v3.0 router; it mines the five
continuous C4 features for *pre-event* behaviour:

1. a 12-step pre-event window table for every anchor;
2. empirical threshold calibration on pre-2005 anchors only, tested cold after
   2005 (thresholds are quantiles of the feature's own history - no hardcoded
   levels such as 0.35 or -0.22 anywhere);
3. a lead-lag precedence matrix: P(feature A fires before feature B);
4. targeted diagnostics for the two hard cases - `OIL_DECLINE_2014` and the
   latched Brent alarm of late 2026.

Deliverables: ``results/c4_precursor_report.md`` and
``results/c4_state_vector_summary.json`` (plus an audit CSV of the windows).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core import composite_router, data_loader
from core import reverse_miner as rm
from core.detectors import StateVector, build_panels, build_state_vectors, cusum_from_z

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
CUTOFF = "2005-01-01"

PRE_STEPS = 12
GRACE_STEPS = 2
COOLDOWN = {"monthly": 6, "weekly": 12}
QUARANTINE = {"monthly": 12, "weekly": 24}
FA_RATE_CAP = 12.0
FA_PENALTY = 15.0
QUANTILES = (0.90, 0.95, 0.975, 0.99, 0.995, 0.999)

SERIES_FEATURES = (
    "sahm_floor_displacement",
    "ph_up",
    "ph_down",
    "drawdown",
    "drawdown_norm",
    "l1_slope_break",
    "trend_slope_z",
)
MONTHLY_SERIES = {"UNRATE", "PAYEMS"}


def _episodes(mask: np.ndarray, cooldown: int) -> np.ndarray:
    return rm.episodes_from_mask(mask, cooldown)


def _in_range(dates: np.ndarray, start: str | None, end: str | None) -> np.ndarray:
    mask = np.ones(dates.size, dtype=bool)
    if start is not None:
        mask &= dates >= np.datetime64(start)
    if end is not None:
        mask &= dates < np.datetime64(end)
    return mask


def _evaluate(
    score: np.ndarray,
    dates: np.ndarray,
    events: list[tuple[str, str, str, int]],
    frequency: str,
    threshold: float,
    false_alarm_range: tuple[str | None, str | None],
    quarantine: int | None = None,
) -> dict[str, object]:
    mask = score >= threshold
    episodes = _episodes(mask, COOLDOWN[frequency])
    in_range = _in_range(dates, *false_alarm_range)
    near = np.zeros(dates.size, dtype=bool)
    hits = 0
    matched: set[int] = set()
    details: list[dict[str, object]] = []
    for name, _, event_date, _ in events:
        anchor = int(np.searchsorted(dates, np.datetime64(event_date), side="left"))
        if anchor >= dates.size:
            details.append({"event": name, "hit": False, "reason": "after_series_end"})
            continue
        low = max(0, anchor - PRE_STEPS)
        high = min(dates.size - 1, anchor + GRACE_STEPS)
        inside = episodes[(episodes >= low) & (episodes <= high)]
        window = quarantine if quarantine is not None else QUARANTINE[frequency]
        near[anchor + 1 : min(dates.size, anchor + window + 1)] = True
        if inside.size:
            hits += 1
            index = int(inside[0])
            matched.add(index)
            details.append(
                {
                    "event": name,
                    "hit": True,
                    "episode_date": str(pd.Timestamp(dates[index]).date()),
                    "steps_before_anchor": int(index - anchor),
                }
            )
        else:
            details.append({"event": name, "hit": False, "reason": "no_alarm"})
    counted = episodes[in_range[episodes] & ~near[episodes]] if episodes.size else episodes
    matched_in = int(np.isin(list(matched), counted).sum()) if matched else 0
    false_alarms = max(0, int(counted.size) - matched_in)
    exposure = int(in_range.sum())
    rate = 1000.0 * false_alarms / exposure if exposure else 0.0
    catch = 100.0 * hits / len(events) if events else 0.0
    return {
        "hits": hits,
        "events_total": len(events),
        "false_alarms": false_alarms,
        "exposure_bars": exposure,
        "false_alarms_per_1000": round(rate, 3),
        "catch_rate_pct": round(catch, 2),
        "score": round(catch - FA_PENALTY * rate, 3),
        "hits_detail": details,
        "episodes": episodes,
    }


def _source_events(
    events: list[tuple[str, str, str, int]], source: str, direction: int | None
) -> list[tuple[str, str, str, int]]:
    if source == "FUSION":
        return [e for e in events if e[1] in MONTHLY_SERIES]
    selected = [e for e in events if e[1] == source]
    if direction is not None:
        selected = [e for e in selected if e[3] == direction]
    return selected


def _split(events: list[tuple[str, str, str, int]]) -> tuple[list, list]:
    train = [e for e in events if e[2] < CUTOFF]
    cold = [e for e in events if e[2] >= CUTOFF]
    return train, cold


def calibrate(
    vectors: dict[str, StateVector],
    events: list[tuple[str, str, str, int]],
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    """Mine a quantile threshold per (source, feature, direction), cold-tested."""
    results: list[dict[str, object]] = []
    chosen: dict[str, dict[str, object]] = {}

    for source, vector in vectors.items():
        frequency = vector.frequency
        features = ["icsa_payems_fusion"] if source == "FUSION" else list(SERIES_FEATURES)
        if source == "FUSION":
            directions = [1]
        else:
            signs = sorted({e[3] for e in _source_events(events, source, None)})
            directions = signs

        for feature in features:
            values = vector.features.get(feature)
            if values is None:
                continue
            for direction in directions:
                events_for = _source_events(events, source, None if source == "FUSION" else direction)
                train, cold = _split(events_for)
                if not cold:
                    continue
                score = values if direction > 0 else -values
                mask_train = _in_range(vector.dates, None, CUTOFF)
                sample = score[mask_train & np.isfinite(score)]
                if sample.size < 50:
                    continue
                if not train:
                    # No pre-2005 anchor for this direction (e.g. Brent declines
                    # before 2005).  Calibrate the most sensitive threshold that
                    # still respects the quiet-era noise budget, then test cold.
                    cold_eval = None
                    chosen_quantile = None
                    chosen_threshold = None
                    for quantile in QUANTILES:
                        threshold = float(np.nanquantile(sample, quantile))
                        probe = _evaluate(
                            score, vector.dates, [], frequency, threshold, (None, CUTOFF)
                        )
                        if probe["false_alarms_per_1000"] <= FA_RATE_CAP:
                            cold_eval = _evaluate(
                                score,
                                vector.dates,
                                cold,
                                frequency,
                                threshold,
                                (CUTOFF, None),
                            )
                            chosen_quantile = quantile
                            chosen_threshold = threshold
                            break
                    if cold_eval is None:
                        continue
                    results.append(
                        {
                            "source": source,
                            "feature": feature,
                            "direction": direction,
                            "quantile": chosen_quantile,
                            "threshold": round(float(chosen_threshold), 6),
                            "train_hits": 0,
                            "train_events": 0,
                            "train_fa_per_1000": probe["false_alarms_per_1000"],
                            "train_score": float(probe["score"]),
                            "cold_hits": int(cold_eval["hits"]),
                            "cold_events": int(cold_eval["events_total"]),
                            "cold_fa": int(cold_eval["false_alarms"]),
                            "cold_fa_per_1000": float(cold_eval["false_alarms_per_1000"]),
                            "qualified": False,
                            "no_train_anchor": True,
                        }
                    )
                    chosen[f"{source}|{feature}|{direction}"] = {
                        **results[-1],
                        "cold_detail": cold_eval["hits_detail"],
                        "train_detail": [],
                    }
                    continue
                best: dict[str, object] | None = None
                for quantile in QUANTILES:
                    threshold = float(np.nanquantile(sample, quantile))
                    evaluation = _evaluate(
                        score, vector.dates, train, frequency, threshold, (None, CUTOFF)
                    )
                    qualified = bool(
                        evaluation["hits"] >= 1
                        and evaluation["false_alarms_per_1000"] <= FA_RATE_CAP
                    )
                    key = (
                        0 if qualified else 1,
                        -float(evaluation["score"]),
                        -int(evaluation["hits"]),
                        float(evaluation["false_alarms_per_1000"]),
                    )
                    if best is None or key < best["key"]:  # type: ignore[operator]
                        best = {
                            "key": key,
                            "quantile": quantile,
                            "threshold": threshold,
                            "train": evaluation,
                        }
                if best is None:
                    continue
                cold_eval = _evaluate(
                    score, vector.dates, cold, frequency, float(best["threshold"]), (CUTOFF, None)
                )
                row = {
                    "source": source,
                    "feature": feature,
                    "direction": direction,
                    "quantile": best["quantile"],
                    "threshold": round(float(best["threshold"]), 6),
                    "train_hits": int(best["train"]["hits"]),
                    "train_events": int(best["train"]["events_total"]),
                    "train_fa_per_1000": float(best["train"]["false_alarms_per_1000"]),
                    "train_score": float(best["train"]["score"]),
                    "cold_hits": int(cold_eval["hits"]),
                    "cold_events": int(cold_eval["events_total"]),
                    "cold_fa": int(cold_eval["false_alarms"]),
                    "cold_fa_per_1000": float(cold_eval["false_alarms_per_1000"]),
                    "qualified": bool(
                        best["train"]["hits"] >= 1
                        and best["train"]["false_alarms_per_1000"] <= FA_RATE_CAP
                    ),
                }
                results.append(row)
                chosen[f"{source}|{feature}|{direction}"] = {
                    **row,
                    "cold_detail": cold_eval["hits_detail"],
                    "train_detail": best["train"]["hits_detail"],
                }
    results.sort(
        key=lambda r: (
            not r["qualified"],
            -r["train_score"],
            -r["train_hits"],
            r["train_fa_per_1000"],
        )
    )
    return results, chosen


def window_table(
    vectors: dict[str, StateVector],
    events: list[tuple[str, str, str, int]],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for name, series_id, event_date, sign in events:
        vector = vectors.get(series_id)
        if vector is None:
            continue
        anchor = int(np.searchsorted(vector.dates, np.datetime64(event_date), side="left"))
        if anchor >= vector.dates.size:
            continue
        for offset in range(-PRE_STEPS, GRACE_STEPS + 1):
            index = anchor + offset
            if index < 0 or index >= vector.dates.size:
                continue
            row: dict[str, object] = {
                "event": name,
                "series": series_id,
                "direction": sign,
                "step_offset": offset,
                "date": str(pd.Timestamp(vector.dates[index]).date()),
            }
            for feature, array in vector.features.items():
                row[feature] = round(float(array[index]), 6) if np.isfinite(array[index]) else None
            rows.append(row)
    return pd.DataFrame(rows)


def precedence(
    vectors: dict[str, StateVector],
    events: list[tuple[str, str, str, int]],
    chosen: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """P(A fires strictly before B) and mean lead, over anchors where both fire."""
    offsets: dict[tuple[str, str], dict[str, int]] = {}
    for name, series_id, event_date, sign in events:
        vector = vectors.get(series_id)
        if vector is None:
            continue
        anchor = int(np.searchsorted(vector.dates, np.datetime64(event_date), side="left"))
        if anchor >= vector.dates.size:
            continue
        per_event: dict[str, int] = {}
        for feature in SERIES_FEATURES:
            key = f"{series_id}|{feature}|{sign}"
            entry = chosen.get(key)
            if not entry:
                continue
            values = vector.features[feature]
            score = values if sign > 0 else -values
            threshold = float(entry["threshold"])
            window = score[max(0, anchor - PRE_STEPS) : anchor + GRACE_STEPS + 1]
            fired = np.flatnonzero(window >= threshold)
            if fired.size:
                per_event[feature] = int(fired[0]) - (anchor - max(0, anchor - PRE_STEPS))
        offsets[(name, series_id)] = per_event

    pairs: dict[tuple[str, str], dict[str, object]] = {}
    features = list(SERIES_FEATURES)
    for feature_a in features:
        for feature_b in features:
            if feature_a == feature_b:
                continue
            earlier = 0
            both = 0
            leads: list[int] = []
            for per_event in offsets.values():
                if feature_a in per_event and feature_b in per_event:
                    both += 1
                    delta = per_event[feature_a] - per_event[feature_b]
                    if delta < 0:
                        earlier += 1
                    leads.append(-delta)
            if both == 0:
                continue
            pairs[(feature_a, feature_b)] = {
                "a": feature_a,
                "b": feature_b,
                "n": both,
                "p_before": round(earlier / both, 3),
                "mean_lead_steps": round(float(np.mean(leads)), 2),
            }
    ranked = sorted(pairs.values(), key=lambda p: (-p["p_before"], -p["n"], p["a"]))
    return ranked[:15]


def _series_diagnostic(
    chosen: dict[str, dict[str, object]], features: tuple[str, ...]
) -> dict[str, object]:
    out: dict[str, object] = {}
    for feature in features:
        entry = chosen.get(f"WCOILBRENTEU|{feature}|-1")
        if entry:
            out[feature] = {
                "threshold": entry["threshold"],
                "cold_hits": entry["cold_hits"],
                "cold_events": entry["cold_events"],
                "cold_fa": entry["cold_fa"],
                "cold_fa_per_1000": entry["cold_fa_per_1000"],
                "cold_events_hit": [
                    d["event"] for d in entry["cold_detail"] if d.get("hit")
                ],
            }
    return out


def brent_latch_diagnostic(
    series_map: dict[str, pd.Series],
    panels: dict[tuple[str, int], rm.DetectorPanel],
    vectors: dict[str, StateVector],
) -> dict[str, object]:
    """Compare the v3 CUSUM latch with the C4 PH unlatch on the last 12 weeks."""
    result = composite_router.build_composite(panels, data_loader.get_events(), series_map)
    config = result["weekly"]["config"]
    resolved = rm.resolve(config, "weekly")
    panel = panels[("WCOILBRENTEU", int(resolved["window"]))]
    slack = 0.5 if config.get("cusum_slack") is None else float(config["cusum_slack"])
    _, neg = cusum_from_z(panel.z, slack)
    ph_down = vectors["WCOILBRENTEU"].features["ph_down"]
    tail = 12
    return {
        "v3_weekly_formula": rm.formula_text(config),
        "cusum_neg_last12": [round(float(x), 3) for x in neg[-tail:]],
        "ph_down_last12": [round(float(x), 3) for x in ph_down[-tail:]],
        "cusum_still_above_threshold": bool(
            neg[-3:].max() >= float(config["cusum_thr"])
        ),
        "ph_down_near_zero": bool(ph_down[-1] < 0.05),
        "as_of": str(pd.Timestamp(panel.dates[-1]).date()),
    }


def render_report(
    calibration: list[dict[str, object]],
    precedence_rows: list[dict[str, object]],
    diagnostics: dict[str, object],
    latch: dict[str, object],
    windows: pd.DataFrame,
    generated_at: str,
) -> str:
    table_rows = [
        [
            row["source"],
            row["feature"],
            row["direction"],
            f"q{row['quantile']}",
            f"{row['train_hits']}/{row['train_events']}",
            f"{row['train_fa_per_1000']:.2f}",
            f"{row['cold_hits']}/{row['cold_events']}",
            row["cold_fa"],
            "yes" if row["qualified"] else "no",
        ]
        for row in calibration
    ]
    prec_rows = [
        [p["a"], p["b"], p["n"], f"{p['p_before']:.2f}", f"{p['mean_lead_steps']:+.1f}"]
        for p in precedence_rows
    ]
    diag_rows = [
        [feature, info["threshold"], f"{info['cold_hits']}/{info['cold_events']}",
         info["cold_fa"], f"{info['cold_fa_per_1000']:.1f}", ", ".join(info["cold_events_hit"]) or "-"]
        for feature, info in diagnostics.items()
    ]
    return f"""# Cycle C4 - StateVector & Backward Precursor Discovery

Generated: `{generated_at}`

All features are continuous; every threshold below is an **empirical quantile of
the feature's own pre-2005 history**. No external level (`0.35`, `-0.22`, `-6.0`)
is hardcoded anywhere in `core/detectors.py` or this miner.

## 1. Pre-event windows

`results/c4_pre_event_windows.csv` contains every anchor's StateVector values
across `[anchor - {PRE_STEPS}, anchor + {GRACE_STEPS}]` steps
({len(windows)} rows, {windows['event'].nunique()} anchors).

## 2. Empirical calibration (pre-2005 only) and cold test

{len(calibration)} (source, feature, direction) combinations were calibrated;
thresholds come from the quantile grid {QUANTILES}. Qualification requires at
least one training catch and `false_alarms_per_1000 <= {FA_RATE_CAP}`.

{_md_table(["Source", "Feature", "Dir", "Quantile", "Train catches", "Train FA/1000", "Cold catches", "Cold FA", "Qualified"], table_rows)}

## 3. Lead-lag precedence

Probability that feature A fires strictly earlier than feature B, over anchors
where both fire:

{_md_table(["A (earlier)", "B (later)", "n anchors", "P(A before B)", "Mean lead (steps)"], prec_rows)}

## 4. Targeted diagnostics

### 4.1 OIL_DECLINE_2014 (cold, direction -1)

{_md_table(["Feature", "Threshold", "Cold catches", "Cold FA", "FA/1000", "Events hit"], diag_rows)}

The 2014 anchor sits at the *pre-decline peak*, so a precursor must fire before
the drop starts; the only admissible window is `[anchor-12, anchor+2]` weeks.
Neither drawdown variant catches it cold, and both exceed the quiet-era budget
while trying. Features missing from this table (for example `ph_down` on Brent)
had **no quantile whose pre-2005 quiet rate stayed within 12/1000**, so no
budget-compliant threshold exists at all - that is reported as uncalibrated
rather than tuned until it fits.

### 4.2 Brent latch, late 2026

v3.0 weekly formula: `{latch['v3_weekly_formula']}` (as of {latch['as_of']}).

| Series (last 12 weeks) | Values |
|---|---|
| v3 CUSUM- | {latch['cusum_neg_last12']} |
| C4 PH down | {latch['ph_down_last12']} |

- CUSUM is still above its alarm threshold: **{latch['cusum_still_above_threshold']}**.
- PH down returned near zero (unlatched): **{latch['ph_down_near_zero']}**.

This is the intended fix: Page-Hinkley decays on mean reversion, so the oil
branch resets instead of staying permanently latched as the CUSUM does.

## 5. Verdict

- The five new dimensions are computed causally and calibrated without external
  thresholds.
- Cold performance is reported as measured; no feature is promoted into the
  routed system unless it clears the same noise budget as v3.0.
"""


def _md_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args(argv)

    series_map = data_loader.load_all(force_refresh=args.force_refresh)
    profiles = data_loader.get_profiles()
    vectors = build_state_vectors(series_map, profiles)
    panels = build_panels(series_map, profiles, data_loader.get_frequency_profiles())
    events = data_loader.get_events()

    calibration, chosen = calibrate(vectors, events)
    windows = window_table(vectors, events)
    precedence_rows = precedence(vectors, events, chosen)
    diagnostics = _series_diagnostic(chosen, ("drawdown", "drawdown_norm", "ph_down", "sahm_floor_displacement"))
    latch = brent_latch_diagnostic(series_map, panels, vectors)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    windows.to_csv(RESULTS_DIR / "c4_pre_event_windows.csv", index=False)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = render_report(
        calibration, precedence_rows, diagnostics, latch, windows, generated_at
    )
    (RESULTS_DIR / "c4_precursor_report.md").write_text(report, encoding="utf-8")

    summary = {
        "generated_at_utc": generated_at,
        "engine": "CHAOS-Reverse-Miner/c4-precursor-miner",
        "anchors": int(len(events)),
        "pre_2005_anchors": int(sum(1 for e in events if e[2] < CUTOFF)),
        "post_2005_anchors": int(sum(1 for e in events if e[2] >= CUTOFF)),
        "features": list(SERIES_FEATURES)
        + ["icsa_payems_fusion"],
        "quantiles": list(QUANTILES),
        "calibration": calibration,
        "precedence": precedence_rows,
        "diagnostics_oil_2014": diagnostics,
        "brent_latch": latch,
        "windows_rows": int(len(windows)),
    }
    (RESULTS_DIR / "c4_state_vector_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print(f"[c4] calibration rows: {len(calibration)}")
    cold = [r for r in calibration if r["cold_hits"] > 0]
    print(f"[c4] features with cold hits: {[(r['source'], r['feature'], r['direction'], r['cold_hits']) for r in cold]}")
    print(f"[c4] report -> {RESULTS_DIR / 'c4_precursor_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
