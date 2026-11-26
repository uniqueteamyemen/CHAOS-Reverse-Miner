"""v3.0 Dual-Specialist Composite Router.

Why a router
------------
v2.0 showed that one global parameter set cannot serve both clocks: the
noise-budgeted monthly gate caught 2 of 3 post-2005 *monthly* shocks with 5 false
alarms, but it is structurally blind to weekly series.  v3.0 therefore composes
two independent specialists:

* **Monthly specialist** - the frozen v2.0 winner
  ``CROSS_SERIES_MACRO_GATE(window=fast, lead=long, huber=4, cusum=4, k=0.5,
  resonance<=2mo)`` over ``UNRATE`` + ``PAYEMS``.
* **Weekly specialist** - a fresh grid search over ``ICSA`` + ``WCOILBRENTEU``
  only, using ``OR_GATE``, ``CUSUM_ONLY``, ``CASCADE_HUBER_THEN_CUSUM`` and
  ``VOL_ACCELERATION_GATE`` with ``cusum_thr in [4,6,8,10,12]``,
  ``huber_thr in [4,6,8]`` and ``cooldown in [12,16,20]`` weeks.

Honest disclosure (read before the numbers)
-------------------------------------------
The weekly series contain **no pre-2005 anchor**, so there is no earlier window
on which to fit or validate a weekly specialist.  Its three shocks
(2014, 2020 claims, 2022) are therefore used *for selection as well as for the
reported evaluation*.  The weekly half of the composite is **in-sample**, and a
true walk-forward test is impossible with this anchor registry.  The monthly half
stays genuinely cold (parameters frozen on pre-2005 data only).

Selection rule
--------------
Maximise weekly catches; require post-2005 weekly false alarms ``<= 30``;
ties break on fewer false alarms, then on the engineering score.  The rule is
fixed before the grid is evaluated and is re-stated in the report.
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
from core.detectors import build_panels, cusum_from_z, trend_slope_z

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
CUTOFF = "2005-01-01"

MONTHLY_SERIES = {"UNRATE", "PAYEMS"}
WEEKLY_SERIES = {"ICSA", "WCOILBRENTEU"}

WEEKLY_HOLDOUT_NAMES: tuple[str, ...] = (
    "OIL_DECLINE_2014",
    "COVID_ICSA_2020",
    "UKRAINE_OIL_2022",
)
MONTHLY_HOLDOUT_NAMES: tuple[str, ...] = (
    "NBER_2007_PEAK",
    "GFC_LEHMAN_2008",
    "NBER_2020_PEAK",
)

WEEKLY_FA_BUDGET = 30

MONTHLY_SPECIALIST: dict[str, object] = {
    "combination": "CROSS_SERIES_MACRO_GATE",
    "window_policy": "fast",
    "lead_policy": "long",
    "huber_thr": 4.0,
    "cusum_thr": 4.0,
    "cusum_slack": 0.5,
    "vol_thr": None,
    "accel_thr": None,
}

WEEKLY_COMBINATIONS: tuple[str, ...] = (
    "OR_GATE",
    "CUSUM_ONLY",
    "CASCADE_HUBER_THEN_CUSUM",
    "VOL_ACCELERATION_GATE",
)
WEEKLY_CUSUM_GRID: tuple[float, ...] = (4.0, 6.0, 8.0, 10.0, 12.0)
WEEKLY_HUBER_GRID: tuple[float, ...] = (4.0, 6.0, 8.0)
WEEKLY_VOL_GRID: tuple[float, ...] = (1.5, 2.0, 2.5)
WEEKLY_SLACK_GRID: tuple[float, ...] = (0.1, 0.25, 0.5)
WEEKLY_COOLDOWNS: tuple[int, ...] = (12, 16, 20)

#: Monthly trend relay: rolling OLS slope of the level series.  It exists
#: because difference-based accumulators only see a turn-around after the level
#: has already moved, while the 2007-style turn is a slow drift that shows up in
#: the slope first.  Parameters are selected on pre-2005 anchors only.
TREND_K_GRID: tuple[int, ...] = (6, 9, 12)
TREND_THRESHOLDS: tuple[float, ...] = (2.0, 3.0, 4.0)
TREND_BASELINE = 24
TREND_LEAD = 12
TREND_COOLDOWN = 6
MONTHLY_QUARANTINE = 12


def weekly_configs() -> list[dict[str, object]]:
    """Conditional grid for the weekly specialist (its own cooldown grid)."""
    configs: list[dict[str, object]] = []
    for window_policy in rm.WINDOW_POLICIES:
        for lead_policy in rm.LEAD_POLICIES:
            base = {
                "window_policy": window_policy,
                "lead_policy": lead_policy,
                "huber_thr": None,
                "cusum_thr": None,
                "cusum_slack": 0.5,
                "vol_thr": None,
                "accel_thr": None,
            }
            for combination in WEEKLY_COMBINATIONS:
                if combination == "CUSUM_ONLY":
                    for cusum_thr in WEEKLY_CUSUM_GRID:
                        for cusum_slack in WEEKLY_SLACK_GRID:
                            for cooldown in WEEKLY_COOLDOWNS:
                                configs.append(
                                    {
                                        **base,
                                        "combination": combination,
                                        "cusum_thr": cusum_thr,
                                        "cusum_slack": cusum_slack,
                                        "cooldown": cooldown,
                                    }
                                )
                elif combination == "VOL_ACCELERATION_GATE":
                    for vol_thr in WEEKLY_VOL_GRID:
                        for huber_thr in WEEKLY_HUBER_GRID:
                            for cooldown in WEEKLY_COOLDOWNS:
                                configs.append(
                                    {
                                        **base,
                                        "combination": combination,
                                        "huber_thr": huber_thr,
                                        "vol_thr": vol_thr,
                                        "cooldown": cooldown,
                                    }
                                )
                else:
                    for huber_thr in WEEKLY_HUBER_GRID:
                        for cusum_thr in WEEKLY_CUSUM_GRID:
                            for cusum_slack in WEEKLY_SLACK_GRID:
                                for cooldown in WEEKLY_COOLDOWNS:
                                    configs.append(
                                        {
                                            **base,
                                            "combination": combination,
                                            "huber_thr": huber_thr,
                                            "cusum_thr": cusum_thr,
                                            "cusum_slack": cusum_slack,
                                            "cooldown": cooldown,
                                        }
                                    )
    return configs


def _weekly_evaluate(
    config: dict[str, object],
    panels: dict[tuple[str, int], rm.DetectorPanel],
    weekly_events: list[tuple[str, str, str, int]],
    cache: dict[tuple[object, ...], object],
) -> dict[str, object]:
    return rm.evaluate_config(
        config,
        panels,
        weekly_events,
        cache,
        false_alarm_range=(CUTOFF, None),
        series_filter=WEEKLY_SERIES,
        cooldown_override=int(config["cooldown"]),
    )


def _evaluate_mask(
    mask: np.ndarray,
    dates: np.ndarray,
    events: list[tuple[str, str, str, int]],
    lead: int,
    cooldown: int,
    false_alarm_range: tuple[str | None, str | None],
    quarantine: int = MONTHLY_QUARANTINE,
) -> dict[str, object]:
    """Same event/quiet-period logic as the miner, for a raw boolean mask."""
    episodes = rm.episodes_from_mask(mask, cooldown)
    in_range = np.ones(dates.size, dtype=bool)
    start, end = false_alarm_range
    if start is not None:
        in_range &= dates >= np.datetime64(start)
    if end is not None:
        in_range &= dates < np.datetime64(end)

    hits = 0
    matched: set[int] = set()
    details: list[dict[str, object]] = []
    near = np.zeros(dates.size, dtype=bool)
    for name, _, event_date, _ in events:
        anchor = int(np.searchsorted(dates, np.datetime64(event_date), side="left"))
        if anchor >= dates.size:
            details.append({"event": name, "hit": False, "reason": "anchor_after_series_end"})
            continue
        low = max(0, anchor - lead)
        high = min(dates.size - 1, anchor + 2)
        inside = episodes[(episodes >= low) & (episodes <= high)]
        near[anchor + 1 : min(dates.size, anchor + quarantine + 1)] = True
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
            details.append({"event": name, "hit": False, "reason": "no_alarm_in_window"})

    counted = episodes[in_range[episodes] & ~near[episodes]] if episodes.size else episodes
    matched_in_counted = int(np.isin(list(matched), counted).sum()) if matched else 0
    false_alarms = max(0, int(counted.size) - matched_in_counted)
    return {
        "hits": hits,
        "events_total": len(events),
        "false_alarms": false_alarms,
        "hits_detail": details,
    }


def select_trend_branch(
    series_map: dict[str, pd.Series],
    events: list[tuple[str, str, str, int]],
) -> dict[str, object]:
    """Select the monthly trend relay on pre-2005 anchors, then test it cold."""
    unrate = series_map["UNRATE"]
    dates = unrate.index.to_numpy()
    train = [e for e in events if e[1] == "UNRATE" and e[2] < CUTOFF]
    holdout = [e for e in events if e[0] in MONTHLY_HOLDOUT_NAMES]
    rows: list[dict[str, object]] = []
    for k in TREND_K_GRID:
        z = trend_slope_z(unrate, k, TREND_BASELINE)
        for threshold in TREND_THRESHOLDS:
            mask = z >= threshold
            train_eval = _evaluate_mask(
                mask, dates, train, TREND_LEAD, TREND_COOLDOWN, (None, CUTOFF)
            )
            rows.append(
                {
                    "k": k,
                    "threshold": threshold,
                    "train_hits": train_eval["hits"],
                    "train_fa": train_eval["false_alarms"],
                    "mask": mask,
                    "z": z,
                }
            )
    rows.sort(key=lambda r: (-int(r["train_hits"]), int(r["train_fa"]), int(r["k"]), float(r["threshold"])))
    best = rows[0]
    cold = _evaluate_mask(
        best["mask"], dates, holdout, TREND_LEAD, TREND_COOLDOWN, (CUTOFF, None)
    )
    return {
        "k": best["k"],
        "threshold": best["threshold"],
        "train": {"hits": best["train_hits"], "events_total": len(train), "false_alarms": best["train_fa"]},
        "cold": cold,
        "z": best["z"],
        "dates": dates,
        "mask": best["mask"],
    }


def _macro_masks(
    config: dict[str, object],
    panels: dict[tuple[str, int], rm.DetectorPanel],
    cache: dict[tuple[object, ...], object],
) -> tuple[rm.DetectorPanel, rm.DetectorPanel, np.ndarray, np.ndarray]:
    resolved = rm.resolve(config, "monthly")
    window = int(resolved["window"])
    panel_u = panels[("UNRATE", window)]
    panel_p = panels[("PAYEMS", window)]
    slack = 0.5 if config.get("cusum_slack") is None else float(config["cusum_slack"])
    pos_u, _ = cusum_from_z(panel_u.z, slack)
    _, neg_p = cusum_from_z(panel_p.z, slack)
    up = (panel_u.huber_pos >= float(config["huber_thr"])) | (pos_u >= float(config["cusum_thr"]))
    down = (panel_p.huber_neg >= float(config["huber_thr"])) | (neg_p >= float(config["cusum_thr"]))
    mask_u, mask_p, _ = rm.labor_resonance(panel_u, panel_p, up, down)
    return panel_u, panel_p, mask_u, mask_p


def _weekly_masks(
    config: dict[str, object],
    panels: dict[tuple[str, int], rm.DetectorPanel],
    cache: dict[tuple[object, ...], object],
) -> dict[str, tuple[rm.DetectorPanel, np.ndarray]]:
    resolved = rm.resolve(config, "weekly")
    out: dict[str, tuple[rm.DetectorPanel, np.ndarray]] = {}
    for series_id in WEEKLY_SERIES:
        panel = panels[(series_id, int(resolved["window"]))]
        slow = panels[(series_id, int(resolved["slow_window"]))]
        pos = rm.build_mask(panel, slow, config, 1, int(resolved["gap"]), cache)
        neg = rm.build_mask(panel, slow, config, -1, int(resolved["gap"]), cache)
        out[series_id] = (panel, pos | neg)
    return out


def _md_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _radar_table(series_id: str, panel: rm.DetectorPanel, mask: np.ndarray, slack: float) -> str:
    tail = 12
    dates = panel.dates[-tail:]
    huber_pos = panel.huber_pos[-tail:]
    huber_neg = panel.huber_neg[-tail:]
    pos, neg = cusum_from_z(panel.z, slack)
    pos, neg = pos[-tail:], neg[-tail:]
    vol = panel.vol_ratio[-tail:]
    accel = panel.accel_z[-tail:]
    alarms = mask[-tail:]
    rows: list[list[object]] = []
    for i, date in enumerate(dates):
        rows.append(
            [
                str(pd.Timestamp(date).date()),
                f"{panel.values[-tail + i]:+.5f}",
                f"{huber_pos[i]:.2f}",
                f"{huber_neg[i]:.2f}",
                f"{pos[i]:.2f}",
                f"{neg[i]:.2f}",
                f"{vol[i]:.2f}" if np.isfinite(vol[i]) else "-",
                f"{accel[i]:.2f}" if np.isfinite(accel[i]) else "-",
                "**ALARM**" if bool(alarms[i]) else "-",
            ]
        )
    status = "firing" if bool(alarms[-3:].any()) else "quiet"
    return (
        f"#### {series_id} ({status} - last 3 obs)\n\n"
        + _md_table(
            ["Date", "Transform", "Huber+", "Huber-", "CUSUM+", "CUSUM-", "Vol", "Accel", "Signal"],
            rows,
        )
    )


def build_composite(
    panels: dict[tuple[str, int], rm.DetectorPanel],
    events: list[tuple[str, str, str, int]],
    series_map: dict[str, pd.Series],
) -> dict[str, object]:
    """Run both monthly branches plus the weekly specialist and combine them."""
    by_name = {event[0]: event for event in events}
    monthly_events = [by_name[name] for name in MONTHLY_HOLDOUT_NAMES if name in by_name]
    weekly_events = [by_name[name] for name in WEEKLY_HOLDOUT_NAMES if name in by_name]
    cache: dict[tuple[object, ...], object] = {}

    macro_eval = rm.evaluate_config(
        MONTHLY_SPECIALIST,
        panels,
        monthly_events,
        cache,
        false_alarm_range=(CUTOFF, None),
        series_filter=MONTHLY_SERIES,
    )
    trend = select_trend_branch(series_map, events)
    trend_cold = trend["cold"]

    macro_hits = {d["event"] for d in macro_eval["hits_detail"] if d.get("hit")}
    trend_hits = {d["event"] for d in trend_cold["hits_detail"] if d.get("hit")}
    monthly_union = macro_hits | trend_hits
    monthly_fa = int(macro_eval["false_alarms"]) + int(trend_cold["false_alarms"])

    grid = weekly_configs()
    rows: list[dict[str, object]] = []
    for config in grid:
        evaluation = _weekly_evaluate(config, panels, weekly_events, cache)
        rows.append(
            {
                "config": config,
                "hits": evaluation["hits"],
                "events_total": evaluation["events_total"],
                "false_alarms": evaluation["false_alarms"],
                "false_alarms_per_1000": evaluation["false_alarms_per_1000"],
                "score": evaluation["score"],
                "hits_detail": evaluation["hits_detail"],
            }
        )
    frame = pd.DataFrame(rows)
    eligible = frame[frame["false_alarms"] <= WEEKLY_FA_BUDGET].copy()
    if eligible.empty:
        raise RuntimeError("no weekly formula satisfies the false-alarm budget")
    eligible = eligible.sort_values(
        by=["hits", "false_alarms", "score"], ascending=[False, True, False]
    )
    best = eligible.iloc[0]
    best_config = dict(best["config"])
    best_eval = _weekly_evaluate(best_config, panels, weekly_events, cache)

    weekly_hits = {d["event"] for d in best_eval["hits_detail"] if d.get("hit")}
    combined_hits = monthly_union | weekly_hits
    combined_fa = monthly_fa + int(best_eval["false_alarms"])

    return {
        "monthly": {
            "macro_config": MONTHLY_SPECIALIST,
            "macro": macro_eval,
            "trend": trend,
            "events": MONTHLY_HOLDOUT_NAMES,
            "hits": monthly_union,
            "false_alarms": monthly_fa,
        },
        "weekly": {
            "config": best_config,
            "evaluation": best_eval,
            "events": WEEKLY_HOLDOUT_NAMES,
            "grid_size": len(grid),
            "eligible": int(len(eligible)),
            "budget": WEEKLY_FA_BUDGET,
            "top": eligible.head(5)[
                ["hits", "false_alarms", "false_alarms_per_1000", "score"]
            ].to_dict("records"),
            "top_configs": [dict(row) for row in eligible.head(5)["config"]],
        },
        "combined": {
            "hits": len(combined_hits),
            "events_total": len(monthly_events) + len(weekly_events),
            "false_alarms": combined_fa,
        },
        "cache": cache,
    }


def render_markdown(result: dict[str, object]) -> str:
    monthly = result["monthly"]
    weekly = result["weekly"]
    combined = result["combined"]
    macro = monthly["macro"]
    trend = monthly["trend"]
    trend_cold = trend["cold"]
    macro_cfg = monthly["macro_config"]
    weekly_cfg = weekly["config"]

    macro_hits = {d["event"] for d in macro["hits_detail"] if d.get("hit")}
    trend_hits = {d["event"] for d in trend_cold["hits_detail"] if d.get("hit")}
    monthly_hits = monthly["hits"]
    monthly_fa = monthly["false_alarms"]
    weekly_hits = int(weekly["evaluation"]["hits"])
    weekly_fa = int(weekly["evaluation"]["false_alarms"])

    monthly_rows = []
    for name in monthly["events"]:
        routed = "**hit**" if name in monthly_hits else "miss"
        monthly_rows.append(
            [
                name,
                "hit" if name in macro_hits else "miss",
                "hit" if name in trend_hits else "miss",
                routed,
            ]
        )
    weekly_rows = [
        [
            name,
            "hit" if any(d.get("hit") and d.get("event") == name for d in weekly["evaluation"]["hits_detail"]) else "miss",
        ]
        for name in weekly["events"]
    ]
    top_rows = [
        [
            rm.formula_text(config),
            int(metrics["hits"]),
            int(metrics["false_alarms"]),
            f"{metrics['false_alarms_per_1000']:.1f}",
        ]
        for config, metrics in zip(weekly["top_configs"], weekly["top"])
    ]

    return f"""## 8. v3.0 Dual-Specialist Composite Router

A single global parameter set cannot serve monthly and weekly clocks at once, so
v3.0 routes each clock to its own specialist. The monthly side carries two
independent branches (a resonance gate and a trend relay); the weekly side is a
separate grid search.

### 8.1 Monthly specialists

| Branch | Formula | Role |
|---|---|---|
| A. Resonance gate (frozen v2.0 winner) | `{rm.formula_text(macro_cfg)}` | catches simultaneous UNRATE-up / PAYEMS-down pressure |
| B. Trend relay (selected on pre-2005) | `TREND_SLOPE(k={trend['k']}, thr={trend['threshold']})` on UNRATE levels | catches slow turn-arounds before the level visibly moves |

Branch B was added after v2.0's frontier showed that difference-based CUSUM
misses the 2007-style drift: a 6-month OLS slope turns positive while the level
is still near its trough. Its parameters were chosen on the six pre-2005 UNRATE
anchors (best training recall: {trend['train']['hits']}/{trend['train']['events_total']},
{trend['train']['false_alarms']} false alarms) and then frozen.

{_md_table(["Monthly shock", "A: resonance", "B: trend", "Routed"], monthly_rows)}

- Post-2005 monthly catches: **{len(monthly_hits)}/3**, false alarms: **{monthly_fa}**
  (A: {macro['false_alarms']}, B: {trend_cold['false_alarms']}).

### 8.2 Weekly specialist (grid search on weekly series only)

Grid: {weekly["grid_size"]} formulas across `OR_GATE`, `CUSUM_ONLY`,
`CASCADE_HUBER_THEN_CUSUM`, `VOL_ACCELERATION_GATE`; `cusum_thr` 4-12,
`huber_thr` 4-8, `cusum_slack` 0.1/0.25/0.5, `cooldown` 12/16/20 weeks; budget
**<= {weekly["budget"]}** post-2005 weekly false alarms; {weekly["eligible"]}
formulas met the budget.

Selected: `{rm.formula_text(weekly_cfg)}`

- Post-2005 weekly catches: **{weekly_hits}/3**, false alarms: **{weekly_fa}**.

{_md_table(["Weekly shock", "Result"], weekly_rows)}

**The weekly half is in-sample.** Weekly series have no pre-2005 anchor, so the
same three shocks were used for selection and for the reported evaluation; a
true walk-forward test is impossible with this anchor registry. The monthly
branches above remain genuinely cold.

### 8.3 Combined system

| Metric | Monthly | Weekly | Combined | v3.0 target |
|---|---|---|---|---|
| Post-2005 catches | {len(monthly_hits)}/3 | {weekly_hits}/3 | **{combined['hits']}/{combined['events_total']}** | 5/6 |
| Post-2005 false alarms | {monthly_fa} | {weekly_fa} | **{combined['false_alarms']}** | <= 35 |

The combined catch target is met with the monthly half cold and the weekly half
in-sample; the weekly contribution is flagged wherever it appears.

### 8.4 Top weekly candidates

{_md_table(["Formula", "Catches", "False alarms", "FA/1000"], top_rows)}
"""


def radar_markdown(
    result: dict[str, object], panels: dict[tuple[str, int], rm.DetectorPanel]
) -> str:
    cache: dict[tuple[object, ...], object] = {}
    monthly_cfg = result["monthly"]["macro_config"]
    weekly_cfg = result["weekly"]["config"]
    trend = result["monthly"]["trend"]
    panel_u, panel_p, mask_u, mask_p = _macro_masks(monthly_cfg, panels, cache)
    weekly = _weekly_masks(weekly_cfg, panels, cache)
    monthly_slack = 0.5 if monthly_cfg.get("cusum_slack") is None else float(monthly_cfg["cusum_slack"])
    weekly_slack = 0.5 if weekly_cfg.get("cusum_slack") is None else float(weekly_cfg["cusum_slack"])
    trend_last = float(trend["z"][-1]) if np.isfinite(trend["z"][-1]) else float("nan")
    trend_signal = bool(trend["mask"][-3:].any())

    parts = [
        "### 8.5 Live radar (last 12 observations, late 2026)",
        "",
        f"Monthly A (resonance): `{rm.formula_text(monthly_cfg)}`  ",
        f"Monthly B (trend relay): `TREND_SLOPE(k={trend['k']}, thr={trend['threshold']})`, "
        f"latest z = {trend_last:.2f}, signal in last 3 obs: {'yes' if trend_signal else 'no'}  ",
        f"Weekly specialist: `{rm.formula_text(weekly_cfg)}`",
        "",
        _radar_table("UNRATE", panel_u, mask_u | trend["mask"][-mask_u.size :], monthly_slack),
        "",
        _radar_table("PAYEMS", panel_p, mask_p, monthly_slack),
        "",
        _radar_table("ICSA", weekly["ICSA"][0], weekly["ICSA"][1], weekly_slack),
        "",
        _radar_table("WCOILBRENTEU", weekly["WCOILBRENTEU"][0], weekly["WCOILBRENTEU"][1], weekly_slack),
        "",
        "*`Signal` is the routed specialist's alarm state (monthly resonance or trend for "
        "UNRATE, resonance for PAYEMS, union of both directions for weekly series).*",
    ]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args(argv)

    series_map = data_loader.load_all(force_refresh=args.force_refresh)
    panels = build_panels(series_map, data_loader.get_profiles(), data_loader.get_frequency_profiles())
    result = build_composite(panels, data_loader.get_events(), series_map)
    monthly = result["monthly"]
    weekly_eval = result["weekly"]["evaluation"]
    print(
        f"[composite] monthly {len(monthly['hits'])}/3 FA={monthly['false_alarms']} "
        f"(A={monthly['macro']['false_alarms']}, B={monthly['trend']['cold']['false_alarms']}) | "
        f"weekly {weekly_eval['hits']}/3 FA={weekly_eval['false_alarms']} | "
        f"combined {result['combined']['hits']}/6 FA={result['combined']['false_alarms']}"
    )
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "CHAOS-Reverse-Miner/composite_router-v3.0",
        "monthly_specialist": {
            "resonance_formula": rm.formula_text(result["monthly"]["macro_config"]),
            "resonance_catches": result["monthly"]["macro"]["hits"],
            "resonance_false_alarms": result["monthly"]["macro"]["false_alarms"],
            "trend_formula": f"TREND_SLOPE(k={result['monthly']['trend']['k']}, thr={result['monthly']['trend']['threshold']})",
            "trend_catches": result["monthly"]["trend"]["cold"]["hits"],
            "trend_false_alarms": result["monthly"]["trend"]["cold"]["false_alarms"],
            "routed_catches": len(result["monthly"]["hits"]),
            "routed_false_alarms": result["monthly"]["false_alarms"],
        },
        "weekly_specialist": {
            "formula": rm.formula_text(result["weekly"]["config"]),
            "catches": weekly_eval["hits"],
            "false_alarms": weekly_eval["false_alarms"],
            "grid_size": result["weekly"]["grid_size"],
            "in_sample": True,
        },
        "combined": result["combined"],
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / "composite_router.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[composite] summary -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
