"""Reverse miner v2.0 - frequency-aware, scale-invariant formula hunting.

Philosophy
----------
Signal engineering, not a theory paper.  Start from the shocks that actually
happened, walk backwards, and keep the formula that would have fired on time
without crying wolf every month.

A formula is defined once and mapped onto each series' own clock:

* ``window_policy`` (fast / medium / slow) -> 18/24/36 months for monthly series,
  36/44/52 weeks for weekly series;
* ``lead_policy`` (short / medium / long) -> 4/6/12 months, 6/12/24 weeks;
* cascade gaps and cooldowns are per frequency (3/6 steps, 6/12 steps).

That is what makes v2.0 frequency-aware: the same *rule* is evaluated at the
same *relative* horizon on every series, then tested cold on unseen shocks.

Engineering score
-----------------
``score = catch_rate_pct - 15 * false_alarms_per_1000``
``false_alarms_per_1000 = 1000 * false_alarms / exposure_bars``

Qualification requires ``hits >= 1`` and ``false_alarms_per_1000 <= 12.0``.
Only qualified formulas are ranked and exported to ``top_formulas.json``.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core import data_loader
from core.detectors import DetectorPanel, build_panels, cusum_from_z

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"

COMBINATIONS: tuple[str, ...] = (
    "HUBER_ONLY",
    "CUSUM_ONLY",
    "OR_GATE",
    "AND_GATE",
    "CASCADE_HUBER_THEN_CUSUM",
    "VOL_ACCELERATION_GATE",
    "ACCEL_PLUS_HUBER",
    "DUAL_SCALE_CASCADE",
    "CROSS_SERIES_MACRO_GATE",
)

HUBER_GRID: tuple[float, ...] = (4.0, 6.0, 8.0, 12.0, 16.0)
#: Recalibrated after the v2.0 scale fix.  With a unit-scale z the classic
#: CUSUM thresholds sit at 2-10 sigma, not 6-20; the old grid was calibrated
#: against the saturated pre-fix accumulators.
CUSUM_GRID: tuple[float, ...] = (2.0, 3.0, 4.0, 6.0, 10.0, 20.0)
SLACK_GRID: tuple[float, ...] = (0.1, 0.25, 0.5)
VOL_GRID: tuple[float, ...] = (1.5, 2.0, 2.5)
ACCEL_GRID: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5)

GRACE_STEPS = 2
FA_PENALTY_PER_1000 = 15.0
FA_RATE_CAP = 12.0
MIN_CATCH_RATE = 40.0
MACRO_TOLERANCE = 2  # months
#: An alarm during the aftermath of a real crisis is late confirmation, not a
#: false alarm.  Quiet-period counting therefore excludes the 12 months
#: (24 weeks) that follow every anchor.
QUARANTINE_STEPS: dict[str, int] = {"monthly": 12, "weekly": 24}

WINDOW_POLICIES = data_loader.get_window_policies()
LEAD_POLICIES = data_loader.get_lead_policies()
FREQUENCY_PROFILES = data_loader.get_frequency_profiles()


# --------------------------------------------------------------------------- #
# Grid resolution
# --------------------------------------------------------------------------- #
def resolve(config: dict[str, object], frequency: str) -> dict[str, int]:
    """Map a policy-based formula onto one frequency's native steps."""
    window = int(WINDOW_POLICIES[str(config["window_policy"])][frequency])
    slow = int(WINDOW_POLICIES["slow"][frequency])
    if slow == window:
        slow = int(WINDOW_POLICIES["medium"][frequency])
    return {
        "window": window,
        "slow_window": slow,
        "lead_w": int(LEAD_POLICIES[str(config["lead_policy"])][frequency]),
        "gap": int(FREQUENCY_PROFILES[frequency]["cascade_gap"]),
        "cooldown": int(FREQUENCY_PROFILES[frequency]["cooldown"]),
    }


def iter_configs() -> list[dict[str, object]]:
    configs: list[dict[str, object]] = []
    for window_policy in WINDOW_POLICIES:
        for lead_policy in LEAD_POLICIES:
            base = {
                "window_policy": window_policy,
                "lead_policy": lead_policy,
                "huber_thr": None,
                "cusum_thr": None,
                "vol_thr": None,
                "accel_thr": None,
                "cusum_slack": None,
            }
            for huber_thr in HUBER_GRID:
                configs.append({**base, "combination": "HUBER_ONLY", "huber_thr": huber_thr})
            for cusum_thr in CUSUM_GRID:
                for cusum_slack in SLACK_GRID:
                    configs.append(
                        {
                            **base,
                            "combination": "CUSUM_ONLY",
                            "cusum_thr": cusum_thr,
                            "cusum_slack": cusum_slack,
                        }
                    )
            for huber_thr in HUBER_GRID:
                for cusum_thr in CUSUM_GRID:
                    for cusum_slack in SLACK_GRID:
                        for combination in (
                            "OR_GATE",
                            "AND_GATE",
                            "CASCADE_HUBER_THEN_CUSUM",
                            "DUAL_SCALE_CASCADE",
                            "CROSS_SERIES_MACRO_GATE",
                        ):
                            configs.append(
                                {
                                    **base,
                                    "combination": combination,
                                    "huber_thr": huber_thr,
                                    "cusum_thr": cusum_thr,
                                    "cusum_slack": cusum_slack,
                                }
                            )
            for vol_thr in VOL_GRID:
                for huber_thr in HUBER_GRID:
                    configs.append(
                        {
                            **base,
                            "combination": "VOL_ACCELERATION_GATE",
                            "huber_thr": huber_thr,
                            "vol_thr": vol_thr,
                        }
                    )
            for accel_thr in ACCEL_GRID:
                for huber_thr in HUBER_GRID:
                    configs.append(
                        {
                            **base,
                            "combination": "ACCEL_PLUS_HUBER",
                            "huber_thr": huber_thr,
                            "accel_thr": accel_thr,
                        }
                    )
    return configs


# --------------------------------------------------------------------------- #
# Alarm construction
# --------------------------------------------------------------------------- #
def episodes_from_mask(mask: np.ndarray, cooldown: int) -> np.ndarray:
    episodes: list[int] = []
    last = -10**9
    for index in np.flatnonzero(mask):
        if index - last >= cooldown:
            episodes.append(int(index))
            last = int(index)
    return np.asarray(episodes, dtype=int)


def _cascade(first: np.ndarray, second: np.ndarray, gap: int) -> np.ndarray:
    """First fires, second confirms within `gap` steps; alarm at confirmation."""
    confirmed: list[int] = []
    for index in np.flatnonzero(first):
        stop = min(index + gap + 1, second.size)
        follow = np.flatnonzero(second[index:stop])
        if follow.size:
            confirmed.append(int(index + follow[0]))
    if not confirmed:
        return np.zeros_like(first, dtype=bool)
    mask = np.zeros_like(first, dtype=bool)
    mask[np.asarray(confirmed, dtype=int)] = True
    return mask


def _cusum_pair(
    panel: DetectorPanel, slack: float, cache: dict[tuple[object, ...], object]
) -> tuple[np.ndarray, np.ndarray]:
    key = ("cusum", id(panel), slack)
    pair = cache.get(key)
    if pair is None:
        pair = cusum_from_z(panel.z, slack)
        cache[key] = pair
    return pair  # type: ignore[return-value]


def _directional(
    panel: DetectorPanel,
    direction: int,
    slack: float,
    cache: dict[tuple[object, ...], object],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cusum_pos, cusum_neg = _cusum_pair(panel, slack, cache)
    if direction > 0:
        return panel.huber_pos, cusum_pos, panel.accel_z
    return panel.huber_neg, cusum_neg, -panel.accel_z


def build_mask(
    panel: DetectorPanel,
    slow: DetectorPanel | None,
    config: dict[str, object],
    direction: int,
    gap: int,
    cusum_cache: dict[tuple[object, ...], object],
) -> np.ndarray:
    slack = 0.5 if config.get("cusum_slack") is None else float(config["cusum_slack"])
    huber, cusum, accel = _directional(panel, direction, slack, cusum_cache)
    combination = str(config["combination"])
    huber_thr = config["huber_thr"]
    cusum_thr = config["cusum_thr"]

    if combination == "HUBER_ONLY":
        return huber >= float(huber_thr)
    if combination == "CUSUM_ONLY":
        return cusum >= float(cusum_thr)
    if combination == "OR_GATE":
        return (huber >= float(huber_thr)) | (cusum >= float(cusum_thr))
    if combination == "AND_GATE":
        return (huber >= float(huber_thr)) & (cusum >= float(cusum_thr))
    if combination == "CASCADE_HUBER_THEN_CUSUM":
        return _cascade(huber >= float(huber_thr), cusum >= float(cusum_thr), gap)
    if combination == "ACCEL_PLUS_HUBER":
        return _cascade(
            accel >= float(config["accel_thr"]), huber >= float(huber_thr), gap
        )
    if combination == "DUAL_SCALE_CASCADE":
        if slow is None:
            return np.zeros_like(huber, dtype=bool)
        _, slow_cusum, _ = _directional(slow, direction, slack, cusum_cache)
        return _cascade(huber >= float(huber_thr), slow_cusum >= float(cusum_thr), gap)
    if combination == "VOL_ACCELERATION_GATE":
        ratio = np.nan_to_num(panel.vol_ratio, nan=0.0)
        return (ratio >= float(config["vol_thr"])) & (huber >= float(huber_thr))
    raise ValueError(f"unknown combination {combination!r}")


# --------------------------------------------------------------------------- #
# Cross-series labour resonance
# --------------------------------------------------------------------------- #
def _month_key(date: np.datetime64 | pd.Timestamp) -> int:
    return int(pd.Timestamp(date).to_period("M").ordinal)


def labor_resonance(
    panel_u: DetectorPanel,
    panel_p: DetectorPanel,
    up_mask: np.ndarray,
    down_mask: np.ndarray,
    tolerance: int = MACRO_TOLERANCE,
) -> tuple[np.ndarray, np.ndarray, set[int]]:
    up = {_month_key(d): bool(v) for d, v in zip(panel_u.dates, up_mask)}
    down = {_month_key(d): bool(v) for d, v in zip(panel_p.dates, down_mask)}
    months = sorted(set(up) | set(down))
    resonant: set[int] = set()
    for month in months:
        if up.get(month) and any(down.get(month + off) for off in range(-tolerance, tolerance + 1)):
            resonant.add(month)
        if down.get(month) and any(up.get(month + off) for off in range(-tolerance, tolerance + 1)):
            resonant.add(month)
    mask_u = np.asarray([_month_key(d) in resonant for d in panel_u.dates], dtype=bool)
    mask_p = np.asarray([_month_key(d) in resonant for d in panel_p.dates], dtype=bool)
    return mask_u, mask_p, resonant


def _episodes_from_months(months: set[int], cooldown: int) -> list[int]:
    episodes: list[int] = []
    last = -10**9
    for month in sorted(months):
        if month - last >= cooldown:
            episodes.append(month)
            last = month
    return episodes


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


def _score_block(hits: int, events_total: int, false_alarms: int, exposure: int) -> dict[str, object]:
    catch_rate = 100.0 * hits / events_total if events_total else 0.0
    fa_rate = 1000.0 * false_alarms / exposure if exposure else 0.0
    score = catch_rate - FA_PENALTY_PER_1000 * fa_rate
    score_qualified = bool(hits >= 1 and fa_rate <= FA_RATE_CAP)
    return {
        "hits": hits,
        "events_total": events_total,
        "catch_rate_pct": round(catch_rate, 2),
        "false_alarms": false_alarms,
        "exposure_bars": exposure,
        "false_alarms_per_1000": round(fa_rate, 3),
        "score": round(score, 3),
        "score_qualified": score_qualified,
        # Engineering guard: a formula must actually catch shocks to be ranked.
        "qualified": bool(score_qualified and catch_rate >= MIN_CATCH_RATE),
    }


def _cache_key(
    panel: DetectorPanel,
    slow: DetectorPanel | None,
    config: dict[str, object],
    direction: int,
    resolved: dict[str, int],
) -> tuple[object, ...]:
    return (
        "ep",
        id(panel),
        id(slow),
        config["combination"],
        resolved["window"],
        resolved["slow_window"],
        direction,
        config["huber_thr"],
        config["cusum_thr"],
        config.get("cusum_slack"),
        config["vol_thr"],
        config["accel_thr"],
        resolved["gap"],
        resolved["cooldown"],
    )


def _episodes_for(
    cache: dict[tuple[object, ...], object] | None,
    panel: DetectorPanel,
    slow: DetectorPanel | None,
    config: dict[str, object],
    direction: int,
    resolved: dict[str, int],
) -> np.ndarray:
    key = _cache_key(panel, slow, config, direction, resolved)
    if cache is not None and key in cache:
        return cache[key]  # type: ignore[return-value]
    mask = build_mask(panel, slow, config, direction, resolved["gap"], cache or {})
    episodes = episodes_from_mask(mask, resolved["cooldown"])
    if cache is not None:
        cache[key] = episodes
    return episodes


def evaluate_config(
    config: dict[str, object],
    panels: dict[tuple[str, int], DetectorPanel],
    events: list[tuple[str, str, str, int]],
    cache: dict[tuple[object, ...], object] | None = None,
    false_alarm_range: tuple[str | None, str | None] = (None, None),
    series_filter: set[str] | None = None,
    cooldown_override: int | None = None,
) -> dict[str, object]:
    if str(config["combination"]) == "CROSS_SERIES_MACRO_GATE":
        return _evaluate_macro(config, panels, events, cache, false_alarm_range)

    grouped: dict[tuple[str, int], list[tuple[str, str, str, int]]] = {}
    for event in events:
        _, series_id, _, sign = event
        if series_filter is not None and series_id not in series_filter:
            continue
        grouped.setdefault((series_id, int(sign)), []).append(event)

    matched: set[tuple[str, int, int]] = set()
    total_episodes = 0
    raw_episodes = 0
    exposure = 0
    hits = 0
    details: list[dict[str, object]] = []
    out_of_range = 0

    for (series_id, direction), group in grouped.items():
        frequency = str(data_loader.SERIES_PROFILES[series_id]["frequency"])
        resolved = resolve(config, frequency)
        if cooldown_override is not None:
            resolved["cooldown"] = int(cooldown_override)
        panel = panels.get((series_id, resolved["window"]))
        if panel is None:
            continue
        slow = panels.get((series_id, resolved["slow_window"]))
        episodes = _episodes_for(cache, panel, slow, config, direction, resolved)
        in_range = _date_mask(panel.dates, *false_alarm_range)
        exposure += int(in_range.sum())
        raw_episodes += int(sum(in_range[episodes])) if episodes.size else 0
        quarantine = int(QUARANTINE_STEPS[frequency])

        anchors: list[int] = []
        lead = resolved["lead_w"]
        for name, _, event_date, _ in group:
            anchor = int(np.searchsorted(panel.dates, np.datetime64(event_date), side="left"))
            if anchor >= panel.dates.size:
                out_of_range += 1
                details.append({"event": name, "hit": False, "reason": "anchor_after_series_end"})
                continue
            anchors.append(anchor)
            low = max(0, anchor - lead)
            high = min(panel.dates.size - 1, anchor + GRACE_STEPS)
            inside = episodes[(episodes >= low) & (episodes <= high)]
            if inside.size:
                hits += 1
                index = int(inside[0])
                matched.add((series_id, direction, index))
                details.append(
                    {
                        "event": name,
                        "hit": True,
                        "episode_date": str(pd.Timestamp(panel.dates[index]).date()),
                        "steps_before_anchor": int(index - anchor),
                    }
                )
            else:
                details.append({"event": name, "hit": False, "reason": "no_alarm_in_window"})

        # Quiet periods: outside every event window AND outside its aftermath.
        near_event = np.zeros(panel.dates.size, dtype=bool)
        for anchor in anchors:
            near_event[anchor + 1 : min(panel.dates.size, anchor + quarantine + 1)] = True
        counted = episodes[in_range[episodes] & ~near_event[episodes]] if episodes.size else episodes
        matched_here = np.asarray(
            [index for (sid, direction_, index) in matched if sid == series_id and direction_ == direction],
            dtype=int,
        )
        matched_in_counted = (
            int(np.isin(matched_here, counted).sum()) if matched_here.size else 0
        )
        total_episodes += int(counted.size) - matched_in_counted

    false_alarms = total_episodes
    block = _score_block(hits, len(events), false_alarms, exposure)
    block.update({"matched_episodes": len(matched), "total_episodes": raw_episodes})
    return {
        **_public_params(config),
        **block,
        "hits_detail": details,
        "events_out_of_range": out_of_range,
    }


def _evaluate_macro(
    config: dict[str, object],
    panels: dict[tuple[str, int], DetectorPanel],
    events: list[tuple[str, str, str, int]],
    cache: dict[tuple[object, ...], np.ndarray] | None,
    false_alarm_range: tuple[str | None, str | None],
) -> dict[str, object]:
    resolved = resolve(config, "monthly")
    window = resolved["window"]
    panel_u = panels.get(("UNRATE", window))
    panel_p = panels.get(("PAYEMS", window))
    if panel_u is None or panel_p is None:
        return {
            **_public_params(config),
            **_score_block(0, len(events), 0, 0),
            "hits_detail": [],
            "events_out_of_range": 0,
        }

    key = (
        "macro",
        id(panel_u),
        id(panel_p),
        window,
        config["huber_thr"],
        config["cusum_thr"],
        config.get("cusum_slack"),
    )
    cached = cache.get(key) if cache is not None else None
    if cached is not None:
        mask_u, mask_p, resonant = cached  # type: ignore[misc]
    else:
        slack = 0.5 if config.get("cusum_slack") is None else float(config["cusum_slack"])
        pos_u, _ = _cusum_pair(panel_u, slack, cache or {})
        _, neg_p = _cusum_pair(panel_p, slack, cache or {})
        up = (panel_u.huber_pos >= float(config["huber_thr"])) | (
            pos_u >= float(config["cusum_thr"])
        )
        down = (panel_p.huber_neg >= float(config["huber_thr"])) | (
            neg_p >= float(config["cusum_thr"])
        )
        mask_u, mask_p, resonant = labor_resonance(panel_u, panel_p, up, down)
        if cache is not None:
            cache[key] = (mask_u, mask_p, resonant)  # type: ignore[assignment]

    in_range = _date_mask(panel_u.dates, *false_alarm_range)
    months_in_range = {_month_key(d) for d, keep in zip(panel_u.dates, in_range) if keep}
    exposed_months = len(months_in_range)
    episode_months = _episodes_from_months(resonant & months_in_range, resolved["cooldown"])
    raw_episodes = len(episode_months)

    hits = 0
    details: list[dict[str, object]] = []
    matched_months: set[int] = set()
    quarantine_months: set[int] = set()
    lead = int(LEAD_POLICIES[str(config["lead_policy"])]["monthly"])
    for name, series_id, event_date, _ in events:
        panel = panels.get((series_id, window))
        if panel is None or panel.frequency != "monthly":
            details.append({"event": name, "hit": False, "reason": "not_covered_by_monthly_gate"})
            continue
        anchor = int(np.searchsorted(panel.dates, np.datetime64(event_date), side="left"))
        if anchor >= panel.dates.size:
            details.append({"event": name, "hit": False, "reason": "anchor_after_series_end"})
            continue
        anchor_month = _month_key(panel.dates[anchor])
        for offset in range(1, int(QUARANTINE_STEPS["monthly"]) + 1):
            quarantine_months.add(anchor_month + offset)
        low = max(0, anchor - lead)
        high = min(panel.dates.size - 1, anchor + GRACE_STEPS)
        window_months = {_month_key(d) for d in panel.dates[low : high + 1]}
        overlap = sorted(window_months & set(episode_months))
        if overlap:
            hits += 1
            matched_months.update(overlap)
            hit_month = overlap[0]
            hit_index = next(
                (i for i, d in enumerate(panel.dates) if _month_key(d) == hit_month), low
            )
            details.append(
                {
                    "event": name,
                    "hit": True,
                    "episode_date": str(pd.Timestamp(panel.dates[hit_index]).date())[:7],
                    "steps_before_anchor": int(hit_index - anchor),
                }
            )
        else:
            details.append({"event": name, "hit": False, "reason": "no_resonance_in_window"})

    counted_months = [m for m in episode_months if m not in quarantine_months]
    false_alarms = max(0, len(counted_months) - len(matched_months & set(counted_months)))
    block = _score_block(hits, len(events), false_alarms, exposed_months)
    block.update({"matched_episodes": len(matched_months), "total_episodes": raw_episodes})
    return {**_public_params(config), **block, "hits_detail": details, "events_out_of_range": 0}


def _public_params(config: dict[str, object]) -> dict[str, object]:
    return {
        "combination": config["combination"],
        "window_policy": config["window_policy"],
        "lead_policy": config["lead_policy"],
        "huber_thr": config["huber_thr"],
        "cusum_thr": config["cusum_thr"],
        "cusum_slack": config.get("cusum_slack"),
        "vol_thr": config["vol_thr"],
        "accel_thr": config["accel_thr"],
        "windows": dict(WINDOW_POLICIES[str(config["window_policy"])]),
        "lead_steps": dict(LEAD_POLICIES[str(config["lead_policy"])]),
    }


def formula_text(config: dict[str, object]) -> str:
    parts = [
        f"window={config['window_policy']}",
        f"lead={config['lead_policy']}",
    ]
    for key in ("huber_thr", "cusum_thr", "cusum_slack", "vol_thr", "accel_thr"):
        if config.get(key) is not None:
            parts.append(f"{key}={config[key]}")
    if config["combination"] == "CROSS_SERIES_MACRO_GATE":
        parts.append("resonance<=2mo")
    return f"{config['combination']}({', '.join(parts)})"


def rank_rows(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["qualified"] = frame["qualified"].astype(bool)
    frame = frame.sort_values(
        by=[
            "qualified",
            "score",
            "false_alarms_per_1000",
            "hits",
            "combination",
            "window_policy",
            "lead_policy",
        ],
        ascending=[False, False, True, False, True, True, True],
        kind="mergesort",
    ).reset_index(drop=True)
    ranks: list[object] = []
    counter = 0
    for qualified in frame["qualified"]:
        if qualified:
            counter += 1
            ranks.append(counter)
        else:
            ranks.append(pd.NA)
    frame.insert(0, "rank", pd.array(ranks, dtype="Int64"))
    return frame


def run_mining(
    events: list[tuple[str, str, str, int]],
    panels: dict[tuple[str, int], DetectorPanel] | None = None,
    false_alarm_range: tuple[str | None, str | None] = (None, None),
    verbose: bool = True,
) -> pd.DataFrame:
    if panels is None:
        series_map = data_loader.load_all()
        panels = build_panels(
            series_map, data_loader.get_profiles(), data_loader.get_frequency_profiles()
        )
    cache: dict[tuple[object, ...], object] = {}
    rows = [
        evaluate_config(config, panels, events, cache, false_alarm_range)
        for config in iter_configs()
    ]
    if verbose:
        qualified = sum(1 for row in rows if row["qualified"])
        print(f"[reverse_miner] evaluated {len(rows)} formulas, {qualified} qualified")
    return rank_rows(rows)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
PUBLIC_COLUMNS = [
    "rank",
    "combination",
    "window_policy",
    "lead_policy",
    "huber_thr",
    "cusum_thr",
    "cusum_slack",
    "vol_thr",
    "accel_thr",
    "hits",
    "events_total",
    "catch_rate_pct",
    "matched_episodes",
    "total_episodes",
    "false_alarms",
    "exposure_bars",
    "false_alarms_per_1000",
    "score",
    "score_qualified",
    "qualified",
]


def to_public_frame(leaderboard: pd.DataFrame) -> pd.DataFrame:
    return leaderboard[PUBLIC_COLUMNS].copy()


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
    for _, row in leaderboard[leaderboard["qualified"]].head(top_n).iterrows():
        config = {
            "combination": row["combination"],
            "window_policy": row["window_policy"],
            "lead_policy": row["lead_policy"],
            "huber_thr": None if pd.isna(row["huber_thr"]) else float(row["huber_thr"]),
            "cusum_thr": None if pd.isna(row["cusum_thr"]) else float(row["cusum_thr"]),
            "cusum_slack": None if pd.isna(row["cusum_slack"]) else float(row["cusum_slack"]),
            "vol_thr": None if pd.isna(row["vol_thr"]) else float(row["vol_thr"]),
            "accel_thr": None if pd.isna(row["accel_thr"]) else float(row["accel_thr"]),
        }
        formulas.append(
            {
                "rank": int(row["rank"]),
                "name": f"F{int(row['rank'])}",
                "combination": config["combination"],
                "formula": formula_text(config),
                "parameters": {
                    **config,
                    "windows": dict(WINDOW_POLICIES[str(row["window_policy"])]),
                    "lead_steps": dict(LEAD_POLICIES[str(row["lead_policy"])]),
                },
                "metrics": {
                    "hits": int(row["hits"]),
                    "events_total": int(row["events_total"]),
                    "catch_rate_pct": float(row["catch_rate_pct"]),
                    "false_alarms": int(row["false_alarms"]),
                    "false_alarms_per_1000": float(row["false_alarms_per_1000"]),
                    "score": float(row["score"]),
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
        "engine": "CHAOS-Reverse-Miner/reverse_miner-v2.0",
        "scoring": {
            "score": "catch_rate_pct - 15.0 * false_alarms_per_1000",
            "qualification": "hits >= 1 and false_alarms_per_1000 <= 12.0 and catch_rate_pct >= 40.0",
            "grace_steps": GRACE_STEPS,
            "macro_resonance_tolerance_months": MACRO_TOLERANCE,
        },
        "grid": {
            "frequency_profiles": {
                freq: {
                    key: (list(value) if isinstance(value, tuple) else value)
                    for key, value in profile.items()
                }
                for freq, profile in FREQUENCY_PROFILES.items()
            },
            "window_policies": WINDOW_POLICIES,
            "lead_policies": LEAD_POLICIES,
            "series_profiles": data_loader.get_profiles(),
            "combinations": list(COMBINATIONS),
            "huber_thresholds": list(HUBER_GRID),
            "cusum_thresholds": list(CUSUM_GRID),
            "vol_thresholds": list(VOL_GRID),
            "accel_thresholds": list(ACCEL_GRID),
            "formulas_evaluated": int(len(leaderboard)),
            "formulas_qualified": int(leaderboard["qualified"].sum()),
        },
        "data_fingerprint": fingerprint,
        "events": [
            {"name": n, "series": s, "date": d, "direction": sign}
            for n, s, d, sign in data_loader.get_events()
        ],
        "formulas": formulas,
        **(metadata or {}),
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return csv_path, json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args(argv)

    series_map = data_loader.load_all(force_refresh=args.force_refresh)
    for series_id, series in series_map.items():
        print(
            f"[data] {series_id:<14} {len(series):>5} obs  "
            f"{series.index.min().date()} .. {series.index.max().date()}"
        )
    panels = build_panels(
        series_map, data_loader.get_profiles(), data_loader.get_frequency_profiles()
    )
    leaderboard = run_mining(data_loader.get_events(), panels=panels)
    csv_path, json_path = export_results(leaderboard, data_loader.fingerprint(), top_n=args.top_n)

    print("\n=== v2.0 top formulas (engineering score) ===")
    with pd.option_context("display.width", 240, "display.max_columns", None):
        print(to_public_frame(leaderboard).head(10).to_string(index=False))
    print(f"\n[reverse_miner] leaderboard -> {csv_path}")
    print(f"[reverse_miner] top formulas -> {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
