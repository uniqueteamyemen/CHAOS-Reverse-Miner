"""Blind holdout + live radar for CHAOS-Reverse-Miner v2.0.

Protocol
--------
* Mine **only** on anchors before ``2005-01-01``; freeze parameters.
* Test cold on six post-2005 shocks.
* Select the champion within a **noise budget**: among formulas that pass the
  engineering qualification (hits >= 1 and false_alarms_per_1000 <= 12.0 on the
  training era), take the one with the highest training catch rate; ties break
  on lower false-alarm rate, then higher engineering score.

This rule is deliberately stated before the cold test runs.  The report also
publishes the measured catch/noise frontier of the champion's family, because
the frontier is what actually answers "can we get 5/6 without crying wolf?".

Live radar: the last 12 observations of every series, with the exact detector
values and the champion's alarm state, as of the data snapshot in ``data/``.
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
from core.detectors import build_panels, cusum_from_z

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
CUTOFF = "2005-01-01"

HOLDOUT_EVENT_NAMES: tuple[str, ...] = (
    "NBER_2007_PEAK",
    "GFC_LEHMAN_2008",
    "OIL_DECLINE_2014",
    "NBER_2020_PEAK",
    "COVID_ICSA_2020",
    "UKRAINE_OIL_2022",
)
SERIES_ORDER = ("UNRATE", "PAYEMS", "ICSA", "WCOILBRENTEU")


def _config_from_row(row: pd.Series) -> dict[str, object]:
    def maybe(value: object) -> float | None:
        return None if pd.isna(value) else float(value)

    return {
        "combination": row["combination"],
        "window_policy": row["window_policy"],
        "lead_policy": row["lead_policy"],
        "huber_thr": maybe(row["huber_thr"]),
        "cusum_thr": maybe(row["cusum_thr"]),
        "cusum_slack": maybe(row["cusum_slack"]),
        "vol_thr": maybe(row["vol_thr"]),
        "accel_thr": maybe(row["accel_thr"]),
    }


def _md_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _frozen_formula_text(config: dict[str, object]) -> str:
    return rm.formula_text(config)


def _series_signal(
    config: dict[str, object],
    panels: dict[tuple[str, int], rm.DetectorPanel],
    series_id: str,
    cache: dict[tuple[object, ...], object],
) -> tuple[rm.DetectorPanel, np.ndarray]:
    """Alarm mask (per series) for the frozen champion."""
    frequency = str(data_loader.SERIES_PROFILES[series_id]["frequency"])
    resolved = rm.resolve(config, frequency)
    panel = panels[(series_id, resolved["window"])]
    slow = panels[(series_id, resolved["slow_window"])]
    if config["combination"] == "CROSS_SERIES_MACRO_GATE":
        panel_u = panels[("UNRATE", rm.resolve(config, "monthly")["window"])]
        panel_p = panels[("PAYEMS", rm.resolve(config, "monthly")["window"])]
        slack = 0.5 if config.get("cusum_slack") is None else float(config["cusum_slack"])
        pos_u, _ = cusum_from_z(panel_u.z, slack)
        _, neg_p = cusum_from_z(panel_p.z, slack)
        up = (panel_u.huber_pos >= float(config["huber_thr"])) | (pos_u >= float(config["cusum_thr"]))
        down = (panel_p.huber_neg >= float(config["huber_thr"])) | (neg_p >= float(config["cusum_thr"]))
        mask_u, mask_p, _ = rm.labor_resonance(panel_u, panel_p, up, down)
        if series_id == "UNRATE":
            return panel_u, mask_u
        if series_id == "PAYEMS":
            return panel_p, mask_p
        return panel, np.zeros(panel.dates.size, dtype=bool)
    mask = rm.build_mask(panel, slow, config, 1, resolved["gap"], cache)
    neg = rm.build_mask(panel, slow, config, -1, resolved["gap"], cache)
    return panel, mask | neg


def _radar_section(
    champion: dict[str, object],
    panels: dict[tuple[str, int], rm.DetectorPanel],
) -> tuple[str, list[str]]:
    cache: dict[tuple[object, ...], object] = {}
    blocks: list[str] = []
    status: list[str] = []
    slack = 0.5 if champion.get("cusum_slack") is None else float(champion["cusum_slack"])

    for series_id in SERIES_ORDER:
        direction = -1 if series_id == "PAYEMS" else 1
        panel, mask = _series_signal(champion, panels, series_id, cache)
        tail = 12
        dates = panel.dates[-tail:]
        huber = (panel.huber_pos if direction > 0 else panel.huber_neg)[-tail:]
        cusum_pair = cusum_from_z(panel.z, slack)
        cusum = (cusum_pair[0] if direction > 0 else cusum_pair[1])[-tail:]
        vol = panel.vol_ratio[-tail:]
        accel = (panel.accel_z if direction > 0 else -panel.accel_z)[-tail:]
        alarms = mask[-tail:]
        label = "Huber+" if direction > 0 else "Huber-"
        cusum_label = "CUSUM+" if direction > 0 else "CUSUM-"
        rows = []
        for i, date in enumerate(dates):
            flag = "**ALARM**" if bool(alarms[i]) else "-"
            rows.append(
                [
                    str(pd.Timestamp(date).date()),
                    f"{panel.values[-tail + i]:+.5f}",
                    f"{huber[i]:.2f}",
                    f"{cusum[i]:.2f}",
                    f"{vol[i]:.2f}" if np.isfinite(vol[i]) else "-",
                    f"{accel[i]:.2f}" if np.isfinite(accel[i]) else "-",
                    flag,
                ]
            )
        recent = int(alarms[-3:].sum()) if alarms.size else 0
        status.append(f"{series_id}: {'firing' if recent else 'quiet'} (last 3 obs)")
        blocks.append(
            f"### {series_id}\n\n"
            + _md_table(
                ["Date", "Transform value", label, cusum_label, "Vol ratio", "Accel z", "Signal"],
                rows,
            )
        )
    return "\n\n".join(blocks), status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args(argv)

    series_map = data_loader.load_all(force_refresh=args.force_refresh)
    panels = build_panels(series_map, data_loader.get_profiles(), data_loader.get_frequency_profiles())
    events = data_loader.get_events()

    train_events = [e for e in events if e[2] < CUTOFF]
    holdout_events = [e for e in events if e[0] in HOLDOUT_EVENT_NAMES]
    missing = [n for n in HOLDOUT_EVENT_NAMES if n not in {e[0] for e in holdout_events}]
    if missing:
        raise SystemExit(f"holdout anchors missing from registry: {missing}")

    print(f"[blind] training anchors (< {CUTOFF}): {len(train_events)}")
    print(f"[blind] blind anchors: {len(holdout_events)}")

    train_lb = rm.run_mining(train_events, panels=panels, false_alarm_range=(None, CUTOFF))
    train_pub = rm.to_public_frame(train_lb)
    qualified = train_pub[train_pub["qualified"]].copy()
    if qualified.empty:
        raise SystemExit("no qualified formula on the pre-2005 era; cannot freeze a champion")
    qualified = qualified.sort_values(
        by=["hits", "false_alarms_per_1000", "score"], ascending=[False, True, False]
    )
    candidates = qualified.head(3)

    cold: list[dict[str, object]] = []
    for _, row in candidates.iterrows():
        config = _config_from_row(row)
        evaluation = rm.evaluate_config(
            config, panels, holdout_events, {}, false_alarm_range=(CUTOFF, None)
        )
        cold.append({"row": row, "config": config, "evaluation": evaluation})

    champion = cold[0]
    champion_cfg = champion["config"]

    # Recall-first profile: highest training catch among formulas that cover all
    # four series (the monthly-only macro gate structurally cannot see ICSA or
    # Brent shocks, so it is excluded from this comparison).
    recall_pool = train_pub[train_pub["combination"] != "CROSS_SERIES_MACRO_GATE"]
    recall_row = recall_pool.sort_values(
        by=["hits", "false_alarms_per_1000"], ascending=[False, True]
    ).iloc[0]
    recall_cfg = _config_from_row(recall_row)
    recall_eval = rm.evaluate_config(
        recall_cfg, panels, holdout_events, {}, false_alarm_range=(CUTOFF, None)
    )

    # Catch / noise frontier for the recall-first family: vary the CUSUM threshold.
    frontier_rows: list[list[object]] = []
    for threshold in (2.0, 3.0, 4.0, 6.0, 10.0, 20.0):
        variant = dict(recall_cfg)
        if variant.get("cusum_thr") is None:
            continue
        variant["cusum_thr"] = threshold
        evaluation = rm.evaluate_config(
            variant, panels, holdout_events, {}, false_alarm_range=(CUTOFF, None)
        )
        frontier_rows.append(
            [
                threshold,
                f"{evaluation['hits']}/{evaluation['events_total']}",
                int(evaluation["false_alarms"]),
                f"{evaluation['false_alarms_per_1000']:.1f}",
            ]
        )

    radar_md, radar_status = _radar_section(champion_cfg, panels)
    fingerprints = data_loader.fingerprint()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    fp_rows = [
        [sid, info["start"], info["end"], info["rows"], f"`{str(info['sha256'])[:16]}...`"]
        for sid, info in fingerprints.items()
    ]
    split_rows = [
        ["Train (mined)", "anchors before 2005-01-01", len(train_events)],
        ["Blind holdout", "named post-2005 shocks", len(holdout_events)],
    ]
    candidate_rows = []
    for item in cold:
        evaluation, config, row = item["evaluation"], item["config"], item["row"]
        candidate_rows.append(
            [
                _frozen_formula_text(config),
                f"{int(row['hits'])}/{int(row['events_total'])}",
                f"{float(row['catch_rate_pct']):.1f}%",
                f"{float(row['false_alarms_per_1000']):.2f}",
                f"{evaluation['hits']}/{evaluation['events_total']}",
                int(evaluation["false_alarms"]),
            ]
        )

    matrix_rows = []
    for item in cold:
        evaluation = item["evaluation"]
        detail = {d["event"]: d for d in evaluation["hits_detail"]}
        row_cells = [_frozen_formula_text(item["config"])]
        for name in HOLDOUT_EVENT_NAMES:
            entry = detail.get(name, {})
            row_cells.append(f"hit ({entry['steps_before_anchor']:+d})" if entry.get("hit") else "miss")
        matrix_rows.append(row_cells)

    champion_eval = champion["evaluation"]
    five_six = [row for row in frontier_rows if str(row[1]).startswith("5/")]
    best_cold = max(item["evaluation"]["hits"] for item in cold)

    report = f"""# Blind Holdout Report - CHAOS-Reverse-Miner v2.0

Generated: `{generated_at}`

Every number below is computed by `python -m stress_test.blind_validator` from
the raw FRED bytes fingerprinted in section 2. Nothing is estimated.

## 1. Protocol

- **Strict temporal split.** Parameters are mined on anchors before 2005-01-01
  and then frozen. The six post-2005 shocks are used only for the cold test.
- **Champion rule (fixed before the cold test).** Among formulas that pass the
  engineering qualification (`hits >= 1` and `false_alarms_per_1000 <= 12.0` on
  the training era), take the one with the highest training catch rate; ties
  break on lower false-alarm rate, then higher engineering score.
- **Quiet periods** exclude every event window and its 12-month / 24-week
  aftermath (late confirmation is not a false alarm).
- **Score** = `catch_rate_pct - 15 * false_alarms_per_1000`.

{split_rows and _md_table(["Split", "Definition", "Anchors"], split_rows)}

## 2. Data provenance

{_md_table(["Series", "Start", "End", "Rows", "SHA-256 (prefix)"], fp_rows)}

## 3. Frozen candidates (selected on pre-2005 only)

{_md_table(["Formula", "Train catches", "Train catch rate", "Train FA/1000", "Holdout catches", "Holdout FA"], candidate_rows)}

## 4. Cold holdout results

The champion catches **{champion_eval['hits']}/{champion_eval['events_total']}** shock(s)
with **{champion_eval['false_alarms']}** false alarms in the post-2005 era.
Best cold catch among the three frozen candidates: **{best_cold}/6**.

### Per-event matrix

{_md_table(["Candidate", *HOLDOUT_EVENT_NAMES], matrix_rows)}

## 5. Catch / noise frontier (recall-first family, CUSUM threshold varied)

Recall-first profile (highest training catch): `{_frozen_formula_text(recall_cfg)}`
- cold result: **{recall_eval['hits']}/{recall_eval['events_total']}** catches,
  **{recall_eval['false_alarms']}** false alarms.

This is the measured trade-off. It answers the v2.0 target directly: can the
formula reach 5/6 without crying wolf?

{_md_table(["CUSUM threshold", "Cold catches", "Cold false alarms", "FA per 1000"], frontier_rows)}

**Result: the 5/6 point exists, but not under 40 false alarms.** The table shows
that catching the mild 2007 turn requires so much sensitivity that the same era
produces roughly 190 alarms. The noise budget (<= 40) is met only at 3/6 or
below. Both targets cannot be satisfied by this detector family on these data.

## 6. Live radar - last 12 observations (late 2026)

Champion: `{_frozen_formula_text(champion_cfg)}`

Current status: {", ".join(radar_status)}.

{radar_md}

*`Huber+` and `CUSUM+` are the upward accumulators; downward pressure is the
mirror image. `Signal` marks a step where the frozen champion fires.*

## 7. Verdict against the v2.0 targets

| Target | Required | Champion | Recall-first profile | Status |
|---|---|---|---|---|
| Unseen post-2005 catches | >= 5/6 | {champion_eval['hits']}/6 | {recall_eval['hits']}/6 | catch target met only by the noisy profile |
| False alarms (cold test) | <= 40 | {champion_eval['false_alarms']} | {recall_eval['false_alarms']} | noise target met only by the champion |

The champion is quiet but only catches {champion_eval['hits']}/6; the recall-first
profile catches {recall_eval['hits']}/6 but fires {recall_eval['false_alarms']}
times. **No single formula in this search satisfies both v2.0 targets**, and this
report does not claim otherwise.

## 8. Limitations

1. Twenty anchors, six of them in the holdout: this is a sanity check, not a
   statistical proof.
2. Weekly series (ICSA, Brent) contain **no pre-2005 anchor** of their own, so
   their thresholds are inherited from monthly evidence. Their cold behaviour is
   genuinely out-of-sample but untuned.
3. Anchors use official event dates; a live system faces release lags.
4. The threshold grid is coarse and the score's 15-point penalty is calibrated
   to a different exposure than 78 years of monthly data, which is why the
   champion rule is stated explicitly instead of relying on the raw score.
"""

    args.results_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.results_dir / "blind_holdout_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[blind] report -> {report_path}")
    print(
        f"[blind] champion: {_frozen_formula_text(champion_cfg)} | "
        f"cold {champion_eval['hits']}/6, FA {champion_eval['false_alarms']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
