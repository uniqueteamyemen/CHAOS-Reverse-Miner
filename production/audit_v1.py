"""v1.0 audit: release-lag realism and out-of-sample segment replay.

Two questions, one deterministic run each:

* **P0.2** - does the 2023-2026 PASS survive realistic FRED publication lags?
  Each series is shifted forward by its release lag (monthly +1 month, weekly
  +1 week) before the same causal replay.
* **1.1** - does the engine still look sane on 2015-2022 (COVID 2020, 2022
  shock), using the identical pinned thresholds and zero re-tuning?

The protocol was pre-registered for 2023-2026; applying its budgets to the
2015-2022 audit is a deliberately conservative choice, disclosed in the report.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from production import chaos_engine as engine

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"

SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("2015-01-01", "2022-12-31", "2015-2022 (out-of-sample)"),
    ("2023-01-01", "2026-11-27", "2023-2026 (protocol period)"),
)

RELEASE_LAG: dict[str, object] = {
    "UNRATE": pd.DateOffset(months=1),
    "PAYEMS": pd.DateOffset(months=1),
    "ICSA": pd.Timedelta(weeks=1),
    "WCOILBRENTEU": pd.Timedelta(weeks=1),
}


def _roundtrip(start: str, end: str, lag: dict[str, object] | None) -> dict[str, object]:
    result = engine.run(start=start, end=end, lag=lag, include_snapshot=False)
    replay = result["replay"]
    return {
        "start": replay["start"],
        "end": replay["end"],
        "months": replay["months"],
        "years": replay["years"],
        "alarm_count": replay["alarm_count"],
        "watch_count": replay["watch_count"],
        "false_alarms_per_year": replay["false_alarms_per_year"],
        "verdict": replay["verdict"],
        "thresholds_sha256": result["thresholds_sha256"],
        "alarm_episodes": replay["alarm_episodes"],
        "watch_episodes": replay["watch_episodes"],
        "release_lag": replay["release_lag"],
        "crisis_check": replay["crisis_check"],
    }


def _episode_rows(episodes: list[dict[str, object]]) -> list[list[object]]:
    return [
        [
            item["start"],
            item["end"],
            item["months"],
            ", ".join(item["triggers"]),
            item["peak"].get("trend_z"),
            item["peak"].get("macro_cusum_down"),
        ]
        for item in episodes
    ]


def render(result: dict[str, object]) -> str:
    lines = [
        "# v1.0 Audit - Release Lag and Out-of-Sample Segments",
        "",
        "Thresholds were **not** touched. Same pinned JSON, same decision rule.",
        "",
        f"Pinned thresholds SHA-256: `{result['thresholds_sha256']}`",
        "",
        "| Segment | Lag | ALARM | WATCH | FA/year | Verdict |",
        "|---|---|---|---|---|---|",
    ]
    for row in result["runs"]:
        lines.append(
            f"| {row['segment']} | {row['lag']} | {row['alarm_count']} | {row['watch_count']} "
            f"| {row['false_alarms_per_year']} | **{row['verdict']}** |"
        )

    for segment in result["segments"]:
        lines += ["", f"## {segment['name']}", ""]
        for run in segment["runs"]:
            lines.append(f"### {run['lag']} lag - {run['verdict']}")
            lines.append("")
            if run["alarm_episodes"]:
                lines += [
                    "| Start | End | Months | Triggers | Peak trend z | Peak PAYEMS CUSUM- |",
                    "|---|---|---|---|---|---|",
                ]
                lines += [
                    "| " + " | ".join(str(cell) for cell in row) + " |"
                    for row in _episode_rows(run["alarm_episodes"])
                ]
            else:
                lines.append("No ALARM episodes.")
            lines.append("")

    fails = [row for row in result["runs"] if row["verdict"] == "FAIL"]
    lines += ["## Failure analysis", ""]
    if not fails:
        lines.append("Every audited run passed its alarm and false-alarm budgets.")
    else:
        for row in fails:
            lines.append(
                f"- **{row['segment']} / {row['lag']} lag fails**: "
                f"{row['alarm_count']} alarms (budget {result['budget']}), "
                f"{row['false_alarms_per_year']} false alarms/year."
            )
    lines += [
        "",
        "## Method notes",
        "",
        "1. Release lag shifts each series index forward: monthly +1 month, weekly",
        "   +1 week. The decision calendar stays the same, so a decision at `t` can",
        "   only use values released by `t`.",
        "2. The 2015-2022 segment is out-of-sample with respect to threshold",
        "   selection (all thresholds were fixed on pre-2005 anchors), but the",
        "   protocol budgets were written for 2023-2026; applying them here is a",
        "   conservative audit, not a new pre-registration.",
        "3. No threshold was re-tuned after seeing any of these results.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-lag-run", action="store_true", help="skip the lagged runs (debug)")
    args = parser.parse_args(argv)

    result: dict[str, object] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": engine.ENGINE_VERSION,
        "thresholds_sha256": engine.load_thresholds()[1],
        "budget": 3,
        "segments": [],
        "runs": [],
    }
    for start, end, name in SEGMENTS:
        segment: dict[str, object] = {"name": name, "runs": []}
        for lag_label, lag in (("zero", None), ("release-lag", RELEASE_LAG)):
            row = _roundtrip(start, end, lag)
            row["segment"] = name
            row["lag"] = lag_label
            segment["runs"].append(row)
            result["runs"].append(row)
        result["segments"].append(segment)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "engine_audit_v1.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    (RESULTS_DIR / "engine_audit_v1.md").write_text(render(result), encoding="utf-8")
    for row in result["runs"]:
        print(
            f"[audit] {row['segment']:<28} {row['lag']:<12} "
            f"alarms={row['alarm_count']} fa/yr={row['false_alarms_per_year']} {row['verdict']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
