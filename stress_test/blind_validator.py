"""Blind holdout validation for the mined formulas.

Protocol (strict temporal split)
--------------------------------
* Train / mine **only** on anchors strictly before ``2005-01-01``.
* Freeze the winning parameters (no re-tuning afterwards).
* Test **blind** on the post-2005 shocks, which were never shown to the miner:
  ``NBER_2007_PEAK``, ``GFC_LEHMAN_2008``, ``OIL_DECLINE_2014``,
  ``NBER_2020_PEAK``, ``COVID_ICSA_2020``, ``UKRAINE_OIL_2022``.

Two families of formulas are reported, because the task brief asks for both:

1. **Primary (honest) candidates** - the top 3 formulas from a *fresh* mining
   run restricted to pre-2005 anchors.  These are the only truly blind results.
2. **Reference** - the top 3 formulas from ``results/top_formulas.json``,
   which were mined on the full timeline.  They have seen the holdout events
   and therefore are **not** blind; their holdout numbers are optimistic and
   are shown only for comparison.

The report never substitutes a good-looking number for an honest one.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core import data_loader
from core.detectors import build_panels
from core.reverse_miner import (
    evaluate_config,
    formula_text,
    run_mining,
    to_public_frame,
)

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


def _config_from_formula(formula: dict[str, object]) -> dict[str, object]:
    params = formula["parameters"]
    return {
        "combination": formula["combination"],
        "window": int(params["window"]),
        "lead_w": int(params["lead_w"]),
        "huber_thr": params["huber_thr"],
        "cusum_thr": params["cusum_thr"],
        "vol_thr": params["vol_thr"],
    }


def _markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args(argv)

    json_path = args.results_dir / "top_formulas.json"
    if not json_path.exists():
        raise SystemExit(f"{json_path} not found - run `python -m core.reverse_miner` first.")
    mined = json.loads(json_path.read_text(encoding="utf-8"))
    reference_formulas = mined["formulas"][: args.top_n]

    series_map = data_loader.load_all(force_refresh=args.force_refresh)
    panels = build_panels(series_map)
    events = data_loader.get_events()

    train_events = [e for e in events if e[2] < CUTOFF]
    holdout_events = [e for e in events if e[2] >= CUTOFF]
    named_holdout = [e for e in holdout_events if e[0] in HOLDOUT_EVENT_NAMES]
    missing = [name for name in HOLDOUT_EVENT_NAMES if name not in {e[0] for e in named_holdout}]
    if missing:
        raise SystemExit(f"holdout anchors missing from registry: {missing}")

    print(f"[blind] training anchors (< {CUTOFF}): {len(train_events)}")
    print(f"[blind] blind anchors (>= {CUTOFF}): {len(named_holdout)} named shocks")

    # 1) Fresh mining, pre-2005 anchors only; false alarms counted pre-2005 only.
    train_leaderboard = run_mining(
        train_events,
        panels=panels,
        false_alarm_range=(None, CUTOFF),
        verbose=True,
    )
    train_public = to_public_frame(train_leaderboard)
    train_public = train_public[train_public["eligible"]].head(args.top_n)
    frozen = [
        {
            "name": f"B{int(row['rank'])}",
            "combination": row["combination"],
            "parameters": {
                "window": int(row["window"]),
                "lead_w": int(row["lead_w"]),
                "huber_thr": None if pd.isna(row["huber_thr"]) else float(row["huber_thr"]),
                "cusum_thr": None if pd.isna(row["cusum_thr"]) else float(row["cusum_thr"]),
                "vol_thr": None if pd.isna(row["vol_thr"]) else float(row["vol_thr"]),
            },
            "train_metrics": {
                "hits": int(row["hits"]),
                "events_total": int(row["events_total"]),
                "hit_rate_pct": float(row["hit_rate_pct"]),
                "false_alarms": int(row["false_alarms"]),
                "score": float(row["score"]),
            },
        }
        for _, row in train_public.iterrows()
    ]

    # 2) Blind test: parameters frozen, holdout events only, false alarms post-2005.
    blind_results = []
    for candidate in frozen:
        config = _config_from_formula(candidate)
        evaluation = evaluate_config(
            config, panels, named_holdout, false_alarm_range=(CUTOFF, None)
        )
        blind_results.append({"candidate": candidate, "evaluation": evaluation})

    # 3) Reference (non-blind) evaluation of the globally mined formulas.
    reference_results = []
    for formula in reference_formulas:
        config = _config_from_formula(formula)
        evaluation = evaluate_config(
            config, panels, named_holdout, false_alarm_range=(CUTOFF, None)
        )
        reference_results.append({"formula": formula, "evaluation": evaluation})

    fingerprints = data_loader.fingerprint()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ---------------- report ----------------
    fp_rows = [
        [sid, info["start"], info["end"], info["rows"], f"`{str(info['sha256'])[:16]}...`"]
        for sid, info in fingerprints.items()
    ]
    split_rows = [
        ["Train (mined)", "anchors before 2005-01-01", len(train_events)],
        ["Blind holdout", "named post-2005 shocks", len(named_holdout)],
        ["Holdout (all)", "every registered anchor >= 2005-01-01", len(holdout_events)],
    ]

    train_rows = [
        [
            int(row["rank"]),
            formula_text(
                {
                    "combination": row["combination"],
                    "window": int(row["window"]),
                    "lead_w": int(row["lead_w"]),
                    "huber_thr": None if pd.isna(row["huber_thr"]) else float(row["huber_thr"]),
                    "cusum_thr": None if pd.isna(row["cusum_thr"]) else float(row["cusum_thr"]),
                    "vol_thr": None if pd.isna(row["vol_thr"]) else float(row["vol_thr"]),
                }
            ),
            f"{row['hits']}/{row['events_total']}",
            f"{row['hit_rate_pct']:.2f}%",
            int(row["false_alarms"]),
            f"{row['score']:.2f}",
        ]
        for _, row in train_public.iterrows()
    ]

    blind_rows = []
    for item in blind_results:
        evaluation, candidate = item["evaluation"], item["candidate"]
        blind_rows.append(
            [
                candidate["name"],
                formula_text(_config_from_formula(candidate)),
                f"{evaluation['hits']}/{evaluation['events_total']}",
                f"{evaluation['hit_rate_pct']:.2f}%",
                int(evaluation["false_alarms"]),
                f"{evaluation['score']:.2f}",
            ]
        )

    reference_rows = []
    for item in reference_results:
        evaluation, formula = item["evaluation"], item["formula"]
        reference_rows.append(
            [
                formula["name"],
                formula["formula"],
                f"{evaluation['hits']}/{evaluation['events_total']}",
                f"{evaluation['hit_rate_pct']:.2f}%",
                int(evaluation["false_alarms"]),
                f"{evaluation['score']:.2f}",
            ]
        )

    matrix_rows = []
    for item in blind_results:
        evaluation, candidate = item["evaluation"], item["candidate"]
        detail = {d["event"]: d for d in evaluation["hits_detail"]}
        row = [candidate["name"]]
        for name in HOLDOUT_EVENT_NAMES:
            entry = detail.get(name, {})
            if entry.get("hit"):
                row.append(f"hit ({entry['steps_before_anchor']:+d} steps)")
            else:
                row.append("miss")
        matrix_rows.append(row)

    best_blind = max(
        (item["evaluation"]["score"] for item in blind_results), default=float("-inf")
    )
    achieved = [item for item in blind_results if item["evaluation"]["score"] == best_blind]
    avg_blind_hits = (
        sum(item["evaluation"]["hits"] for item in blind_results) / len(blind_results)
        if blind_results
        else 0.0
    )

    report = f"""# Blind Holdout Report - CHAOS-Reverse-Miner

Generated: `{generated_at}`

This report is produced by `python -m stress_test.blind_validator` from the same
raw FRED files whose fingerprints appear below. Every number is computed, not
estimated.

## 1. Protocol

- **Strict temporal split.** Parameters are mined on anchors **before 2005** only,
  then frozen. The six post-2005 shocks are used **only** for the final test.
- **No peeking, no re-tuning.** The miner never receives the holdout anchors.
- **False alarms** are counted as distinct alarm episodes inside the post-2005
  era that fall outside every holdout event window (6-step cooldown per episode).
- **Score** is the same pragmatic objective used during mining:
  `hit_rate_pct - 0.8 * false_alarms`.

{split_rows and _markdown_table(["Split", "Definition", "Anchors"], split_rows)}

### Train anchors ({len(train_events)})

```
{chr(10).join(e[0] for e in train_events)}
```

### Blind holdout anchors ({len(named_holdout)})

```
{chr(10).join(e[0] for e in named_holdout)}
```

## 2. Data provenance (SHA-256 of the exact raw bytes used)

{_markdown_table(["Series", "Start", "End", "Rows", "SHA-256 (prefix)"], fp_rows)}

## 3. Training leaderboard (pre-2005 only)

{_markdown_table(["Rank", "Formula", "Hits", "Hit rate", "False alarms", "Score"], train_rows)}

Top {len(frozen)} frozen candidates: {", ".join(c["name"] for c in frozen)}.

## 4. Blind holdout results (parameters frozen on pre-2005)

{_markdown_table(["Candidate", "Formula", "Hits", "Hit rate", "False alarms", "Score"], blind_rows)}

Best blind score: **{best_blind:.2f}**; mean holdout hits across the
{len(blind_results)} frozen candidates: **{avg_blind_hits:.2f} / {len(named_holdout)}**.

### Per-event blind matrix

{_markdown_table(["Candidate", *HOLDOUT_EVENT_NAMES], matrix_rows)}

`steps` is negative when the alarm fired *before* the anchor (the intended
precursor behaviour) and positive when it fired up to 2 steps after.

## 5. Reference: globally mined formulas (NOT blind - optimistic)

The formulas below come from `results/top_formulas.json`, mined on the full
timeline, so their holdout anchors were part of their own search. These numbers
are **contaminated by selection** and are shown only to contrast in-sample
optimism with the honest, frozen-parameter results above.

{_markdown_table(["Formula", "Definition", "Hits", "Hit rate", "False alarms", "Score"], reference_rows)}

## 6. Verdict

- Blind recall of the frozen candidates: **{", ".join(f"{item['candidate']['name']} {item['evaluation']['hits']}/{len(named_holdout)}" for item in blind_results)}**.
- This is a **6-event** holdout. It is a sanity check on generalisation, not a
  statistical proof: six binary outcomes cannot support a confidence claim.
- The pragmatic score penalises false alarms at 0.8 points per wave. A negative
  blind score means the formula alarmed more often than its hits are worth, and
  that result is reported as-is.

## 7. Limitations

1. The anchor registry is small (12 specified anchors + 8 PAYEMS peaks), so the
   effective training sample is a few dozen windows at most.
2. Event windows overlap the 40/60-step rolling baselines; a detector cannot
   fire before enough history exists (warm-up).
3. Thresholds are searched on a coarse grid; the leaderboard is a discrete
   optimum, not a global one.
4. Anchors are dated by NBER/official event dates, which are themselves revised
   or announced with a lag; a real-time system would face release delays that
   this retrospective test does not model.
5. No transaction costs, no macro covariates, no regime detection: this is a
   detector-mining benchmark, not a trading system.
"""

    args.results_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.results_dir / "blind_holdout_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[blind] report -> {report_path}")
    for item in blind_results:
        print(
            f"[blind] {item['candidate']['name']}: "
            f"{item['evaluation']['hits']}/{len(named_holdout)} hits, "
            f"{item['evaluation']['false_alarms']} false alarms, "
            f"score {item['evaluation']['score']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
