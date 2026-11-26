"""Reverse miner: start from historical crisis anchors and search backwards.

For every registered anchor the miner opens a *backward lookback window*
(``lead_w`` steps before the anchor, plus ``GRACE_STEPS`` after it) and asks a
single question:

    Which detector formula would have fired inside that window, while firing as
    rarely as possible everywhere else?

The search space is the Cartesian product of

* detector windows ``{40, 60}``
* detector combinations ``{HUBER_ONLY, CUSUM_ONLY, OR_GATE,
  CASCADE_HUBER_THEN_CUSUM, VOL_ACCELERATION_GATE}``
* Huber thresholds ``{4, 6, 8, 12, 16}`` (where the combination uses Huber)
* CUSUM thresholds ``{6, 10, 15, 20}`` (where the combination uses CUSUM)
* lookback lengths ``{3, 6, 12}`` steps

Every alarm episode is deduplicated with a ``COOLDOWN``-step refractory period,
so one long alarm wave counts once.

Scoring
-------
``hit_rate_pct  = 100 * matched_events / events``
``false_alarms  = distinct alarm episodes outside every event window``
``score         = hit_rate_pct - 0.8 * false_alarms``

The score is deliberately *pragmatic*: it rewards recall and punishes noise in
the same units, so a formula that "catches everything" by alarming constantly
loses to a formula that is quiet and timely.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core import data_loader
from core.detectors import DetectorPanel, build_panels

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"

COMBINATIONS: tuple[str, ...] = (
    "HUBER_ONLY",
    "CUSUM_ONLY",
    "OR_GATE",
    "CASCADE_HUBER_THEN_CUSUM",
    "VOL_ACCELERATION_GATE",
)
HUBER_GRID: tuple[float, ...] = (4.0, 6.0, 8.0, 12.0, 16.0)
CUSUM_GRID: tuple[float, ...] = (6.0, 10.0, 15.0, 20.0)
VOL_GRID: tuple[float, ...] = (1.5, 2.0, 2.5)
WINDOWS: tuple[int, ...] = (40, 60)
LEAD_WINDOWS: tuple[int, ...] = (3, 6, 12)
GRACE_STEPS = 2
COOLDOWN = 6
CASCADE_CONFIRM_STEPS = 4
FALSE_ALARM_PENALTY = 0.8


# --------------------------------------------------------------------------- #
# Alarm construction
# --------------------------------------------------------------------------- #
def episodes_from_mask(mask: np.ndarray, cooldown: int = COOLDOWN) -> np.ndarray:
    """Indices of the first bar of each distinct alarm wave."""
    candidates = np.flatnonzero(mask)
    episodes: list[int] = []
    last = -10**9
    for index in candidates:
        if index - last >= cooldown:
            episodes.append(int(index))
            last = int(index)
    return np.asarray(episodes, dtype=int)


def _cascade(huber_mask: np.ndarray, cusum_mask: np.ndarray) -> np.ndarray:
    """Huber fires first; CUSUM must confirm within CASCADE_CONFIRM_STEPS."""
    confirmed: list[int] = []
    for index in np.flatnonzero(huber_mask):
        stop = min(index + CASCADE_CONFIRM_STEPS + 1, cusum_mask.size)
        follow = np.flatnonzero(cusum_mask[index:stop])
        if follow.size:
            confirmed.append(int(index + follow[0]))
    if not confirmed:
        return np.zeros_like(huber_mask, dtype=bool)
    mask = np.zeros_like(huber_mask, dtype=bool)
    mask[np.asarray(confirmed, dtype=int)] = True
    return mask


def build_mask(
    panel: DetectorPanel,
    combination: str,
    direction: int,
    huber_thr: float | None,
    cusum_thr: float | None,
    vol_thr: float = 2.0,
) -> np.ndarray:
    """Boolean alarm mask for one formula direction (+1 up / -1 down)."""
    huber = panel.huber_pos if direction > 0 else panel.huber_neg
    cusum = panel.cusum_pos if direction > 0 else panel.cusum_neg

    if combination == "HUBER_ONLY":
        return huber >= float(huber_thr)
    if combination == "CUSUM_ONLY":
        return cusum >= float(cusum_thr)
    if combination == "OR_GATE":
        return (huber >= float(huber_thr)) | (cusum >= float(cusum_thr))
    if combination == "CASCADE_HUBER_THEN_CUSUM":
        return _cascade(huber >= float(huber_thr), cusum >= float(cusum_thr))
    if combination == "VOL_ACCELERATION_GATE":
        vol = np.nan_to_num(panel.vol_ratio, nan=0.0)
        return (vol >= float(vol_thr)) & (huber >= float(huber_thr))
    raise ValueError(f"unknown combination {combination!r}")


# --------------------------------------------------------------------------- #
# Grid
# --------------------------------------------------------------------------- #
def iter_configs(
    windows: tuple[int, ...] = WINDOWS, lead_windows: tuple[int, ...] = LEAD_WINDOWS
) -> list[dict[str, object]]:
    """Enumerate the search grid (parameters a combination ignores stay None)."""
    configs: list[dict[str, object]] = []
    for window in windows:
        for lead in lead_windows:
            for huber_thr in HUBER_GRID:
                configs.append(
                    {
                        "combination": "HUBER_ONLY",
                        "window": window,
                        "lead_w": lead,
                        "huber_thr": huber_thr,
                        "cusum_thr": None,
                        "vol_thr": None,
                    }
                )
            for cusum_thr in CUSUM_GRID:
                configs.append(
                    {
                        "combination": "CUSUM_ONLY",
                        "window": window,
                        "lead_w": lead,
                        "huber_thr": None,
                        "cusum_thr": cusum_thr,
                        "vol_thr": None,
                    }
                )
            for huber_thr in HUBER_GRID:
                for cusum_thr in CUSUM_GRID:
                    configs.append(
                        {
                            "combination": "OR_GATE",
                            "window": window,
                            "lead_w": lead,
                            "huber_thr": huber_thr,
                            "cusum_thr": cusum_thr,
                            "vol_thr": None,
                        }
                    )
                    configs.append(
                        {
                            "combination": "CASCADE_HUBER_THEN_CUSUM",
                            "window": window,
                            "lead_w": lead,
                            "huber_thr": huber_thr,
                            "cusum_thr": cusum_thr,
                            "vol_thr": None,
                        }
                    )
            for vol_thr in VOL_GRID:
                for huber_thr in HUBER_GRID:
                    configs.append(
                        {
                            "combination": "VOL_ACCELERATION_GATE",
                            "window": window,
                            "lead_w": lead,
                            "huber_thr": huber_thr,
                            "cusum_thr": None,
                            "vol_thr": vol_thr,
                        }
                    )
    return configs


def formula_text(config: dict[str, object]) -> str:
    parts = [f"window={config['window']}", f"lead={config['lead_w']}"]
    if config["huber_thr"] is not None:
        parts.append(f"huber_thr={config['huber_thr']}")
    if config["cusum_thr"] is not None:
        parts.append(f"cusum_thr={config['cusum_thr']}")
    if config["vol_thr"] is not None:
        parts.append(f"vol_thr={config['vol_thr']}")
    return f"{config['combination']}({', '.join(parts)})"


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def _date_mask(dates: np.ndarray, start: str | None, end: str | None) -> np.ndarray:
    mask = np.ones(dates.size, dtype=bool)
    if start is not None:
        mask &= dates >= np.datetime64(start)
    if end is not None:
        mask &= dates < np.datetime64(end)
    return mask


def evaluate_config(
    config: dict[str, object],
    panels: dict[tuple[str, int], DetectorPanel],
    events: list[tuple[str, str, str, int]],
    false_alarm_range: tuple[str | None, str | None] = (None, None),
) -> dict[str, object]:
    """Score one formula over the event list and the quiet remainder."""
    window = int(config["window"])
    lead_w = int(config["lead_w"])

    # group events by (series, direction) so each mask is built once
    grouped: dict[tuple[str, int], list[tuple[str, str, str, int]]] = {}
    for event in events:
        _, series_id, _, sign = event
        grouped.setdefault((series_id, int(sign)), []).append(event)

    matched_episodes: set[tuple[str, int, int]] = set()
    total_episodes = 0
    exposure_bars = 0
    hits = 0
    hits_detail: list[dict[str, object]] = []
    out_of_range = 0

    for (series_id, direction), group in grouped.items():
        panel = panels.get((series_id, window))
        if panel is None:
            continue
        mask = build_mask(
            panel,
            str(config["combination"]),
            direction,
            config["huber_thr"],
            config["cusum_thr"],
            2.0 if config["vol_thr"] is None else float(config["vol_thr"]),
        )
        episodes = episodes_from_mask(mask)
        in_range = _date_mask(panel.dates, *false_alarm_range)
        exposure_bars += int(in_range.sum())
        total_episodes += int(sum(in_range[episodes])) if episodes.size else 0

        for name, _, event_date, _ in group:
            anchor = int(np.searchsorted(panel.dates, np.datetime64(event_date), side="left"))
            if anchor >= panel.dates.size:
                out_of_range += 1
                hits_detail.append(
                    {"event": name, "hit": False, "reason": "anchor_after_series_end"}
                )
                continue
            low = max(0, anchor - lead_w)
            high = min(panel.dates.size - 1, anchor + GRACE_STEPS)
            inside = episodes[(episodes >= low) & (episodes <= high)]
            if inside.size:
                hits += 1
                index = int(inside[0])
                matched_episodes.add((series_id, direction, index))
                step = int(index - anchor)
                hits_detail.append(
                    {
                        "event": name,
                        "hit": True,
                        "episode_date": str(pd.Timestamp(panel.dates[index]).date()),
                        "steps_before_anchor": -step,
                    }
                )
            else:
                hits_detail.append({"event": name, "hit": False, "reason": "no_alarm_in_window"})

    events_total = len(events)
    hit_rate_pct = 100.0 * hits / events_total if events_total else 0.0
    false_alarms = max(0, total_episodes - len(matched_episodes))
    score = hit_rate_pct - FALSE_ALARM_PENALTY * false_alarms
    alarm_rate = 1000.0 * false_alarms / exposure_bars if exposure_bars else 0.0
    # Diagnostic only: the specified score charges the same 0.8 for an alarm in a
    # 78-year quiet stretch and in a 5-year one.  Dividing the count by exposure
    # keeps the trade-off on a per-opportunity basis.  The leaderboard is still
    # ranked by the specified `score`.
    score_normalized = hit_rate_pct - FALSE_ALARM_PENALTY * alarm_rate

    return {
        **{k: config[k] for k in ("combination", "window", "lead_w", "huber_thr", "cusum_thr", "vol_thr")},
        "hits": hits,
        "events_total": events_total,
        "events_out_of_range": out_of_range,
        "hit_rate_pct": round(hit_rate_pct, 2),
        "matched_episodes": len(matched_episodes),
        "total_episodes": total_episodes,
        "false_alarms": false_alarms,
        "exposure_bars": exposure_bars,
        "alarm_rate_per_1000": round(alarm_rate, 3),
        "score": round(score, 3),
        "score_normalized": round(score_normalized, 3),
        "hits_detail": hits_detail,
    }


def rank_rows(rows: list[dict[str, object]]) -> pd.DataFrame:
    """Rank *active* formulas by the specified pragmatic score.

    A formula that never fires sits at score 0 and would beat every real
    detector on a timeline this long (the penalty is charged per alarm, not per
    opportunity).  Such a formula is not a detector, so it is marked ineligible
    and left unranked instead of being allowed to win by doing nothing.
    """
    frame = pd.DataFrame(rows)
    frame["eligible"] = frame["hits"] > 0
    frame = frame.sort_values(
        by=["eligible", "score", "false_alarms", "hits", "combination", "window", "lead_w"],
        ascending=[False, False, True, False, True, True, True],
        kind="mergesort",
    ).reset_index(drop=True)
    ranks: list[object] = []
    counter = 0
    for eligible in frame["eligible"]:
        if eligible:
            counter += 1
            ranks.append(counter)
        else:
            ranks.append(pd.NA)
    frame.insert(0, "rank", pd.array(ranks, dtype="Int64"))
    return frame


def run_mining(
    events: list[tuple[str, str, str, int]],
    panels: dict[tuple[str, int], DetectorPanel] | None = None,
    windows: tuple[int, ...] = WINDOWS,
    lead_windows: tuple[int, ...] = LEAD_WINDOWS,
    false_alarm_range: tuple[str | None, str | None] = (None, None),
    verbose: bool = True,
) -> pd.DataFrame:
    """Grid-search every formula and return the ranked leaderboard."""
    if panels is None:
        series_map = data_loader.load_all()
        panels = build_panels(series_map, windows=windows)
    rows: list[dict[str, object]] = []
    configs = iter_configs(windows=windows, lead_windows=lead_windows)
    for position, config in enumerate(configs, start=1):
        rows.append(evaluate_config(config, panels, events, false_alarm_range))
        if verbose and position % 50 == 0:
            print(f"[reverse_miner] evaluated {position}/{len(configs)} formulas")
    return rank_rows(rows)


def to_public_frame(leaderboard: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "rank",
        "combination",
        "window",
        "huber_thr",
        "cusum_thr",
        "lead_w",
        "vol_thr",
        "hits",
        "events_total",
        "hit_rate_pct",
        "matched_episodes",
        "total_episodes",
        "false_alarms",
        "exposure_bars",
        "alarm_rate_per_1000",
        "score",
        "score_normalized",
        "eligible",
    ]
    return leaderboard[columns].copy()


def export_results(
    leaderboard: pd.DataFrame,
    fingerprint: dict[str, dict[str, object]],
    top_n: int = 10,
    results_dir: Path = RESULTS_DIR,
    metadata: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    csv_path = results_dir / "reverse_mining_leaderboard.csv"
    json_path = results_dir / "top_formulas.json"

    to_public_frame(leaderboard).to_csv(csv_path, index=False)

    formulas: list[dict[str, object]] = []
    ranked = leaderboard[leaderboard["eligible"]].head(top_n)
    for _, row in ranked.iterrows():
        config = {
            "combination": row["combination"],
            "window": int(row["window"]),
            "lead_w": int(row["lead_w"]),
            "huber_thr": None if pd.isna(row["huber_thr"]) else float(row["huber_thr"]),
            "cusum_thr": None if pd.isna(row["cusum_thr"]) else float(row["cusum_thr"]),
            "vol_thr": None if pd.isna(row["vol_thr"]) else float(row["vol_thr"]),
        }
        formulas.append(
            {
                "rank": int(row["rank"]),
                "name": f"F{int(row['rank'])}",
                "combination": config["combination"],
                "parameters": {k: config[k] for k in ("window", "lead_w", "huber_thr", "cusum_thr", "vol_thr")},
                "formula": formula_text(config),
                "metrics": {
                    "hits": int(row["hits"]),
                    "events_total": int(row["events_total"]),
                    "hit_rate_pct": float(row["hit_rate_pct"]),
                    "false_alarms": int(row["false_alarms"]),
                    "alarm_rate_per_1000": float(row["alarm_rate_per_1000"]),
                    "score": float(row["score"]),
                    "score_normalized": float(row["score_normalized"]),
                },
                "event_hits": [
                    detail["event"]
                    for detail in row["hits_detail"]
                    if isinstance(detail, dict) and detail.get("hit")
                ],
            }
        )

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "CHAOS-Reverse-Miner/reverse_miner",
        "scoring": {
            "score": "hit_rate_pct - 0.8 * false_alarms",
            "cooldown_steps": COOLDOWN,
            "grace_steps": GRACE_STEPS,
            "cascade_confirm_steps": CASCADE_CONFIRM_STEPS,
            "eligibility": "hits > 0 (a formula that never fires is not ranked as a detector)",
            "diagnostic": "score_normalized = hit_rate_pct - 0.8 * false_alarms_per_1000_bars (not used for ranking)",
        },
        "grid": {
            "windows": list(WINDOWS),
            "lead_windows": list(LEAD_WINDOWS),
            "huber_thresholds": list(HUBER_GRID),
            "cusum_thresholds": list(CUSUM_GRID),
            "vol_thresholds": list(VOL_GRID),
            "formulas_evaluated": int(len(leaderboard)),
            "formulas_eligible": int(leaderboard["eligible"].sum()),
        },
        "data_fingerprint": fingerprint,
        "events": [{"name": n, "series": s, "date": d, "direction": sign} for n, s, d, sign in data_loader.get_events()],
        "formulas": formulas,
        **(metadata or {}),
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return csv_path, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--force-refresh", action="store_true", help="re-download FRED CSVs")
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args(argv)

    series_map = data_loader.load_all(force_refresh=args.force_refresh)
    for series_id, series in series_map.items():
        print(
            f"[data] {series_id:<14} {len(series):>5} obs  "
            f"{series.index.min().date()} .. {series.index.max().date()}"
        )
    events = data_loader.get_events()
    panels = build_panels(series_map)
    leaderboard = run_mining(events, panels=panels)
    csv_path, json_path = export_results(
        leaderboard,
        data_loader.fingerprint(),
        top_n=args.top_n,
        results_dir=args.results_dir,
    )

    print("\n=== Top 10 formulas (pragmatic score) ===")
    preview = to_public_frame(leaderboard).head(10)
    with pd.option_context("display.width", 200, "display.max_columns", None):
        print(preview.to_string(index=False))
    print(f"\n[reverse_miner] leaderboard -> {csv_path}")
    print(f"[reverse_miner] top formulas -> {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
