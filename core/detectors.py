"""Continuous, non-frozen, rolling-baseline detectors.

Design goal: fix the failure mode of static-baseline gating (a 1966 median/MAD
frozen once and reused for decades, which saturates every later shock).  Every
statistic here is recomputed inside a *rolling* window, so the baseline adapts.

All detectors operate on the first difference of the level series
(``np.diff``).  That stationarises non-stationary series such as ``PAYEMS``
(headcount levels only grow) and keeps the accumulators from saturating.

Definitions implemented
-----------------------
1. ``diffs`` -- first difference of the raw series, date-aligned to the *later*
   observation (the difference is realised on the later date).
2. ``huber_cusum`` -- rolling median and rolling MAD over ``window``; robust
   standardisation ``z = (x - med) / (1.4826 * MAD)``; Huber psi-clipping with
   ``c = 1.345``; two-sided CUSUM accumulators with slack ``k = 0.5``.
3. ``raw_cusum`` -- identical standardisation but *without* psi-clipping, so
   large innovations are not truncated.  Faster / noisier change detector.
4. ``volatility_ratio`` -- rolling std (6 steps) / rolling std (40 steps);
   captures "silent" pre-crisis compression or expansion in the residual noise.

All accumulators are returned as plain numpy arrays aligned with ``dates``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

HUBER_C = 1.345
MAD_SCALE = 1.4826
CUSUM_SLACK = 0.5
VOL_SHORT = 6
VOL_LONG = 40
EPS = 1e-8


def first_difference(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Return (dates, diffs) for the first difference of ``series``."""
    values = series.to_numpy(dtype=float)
    dates = series.index.to_numpy()
    if values.size < 2:
        return dates[:0], np.empty(0, dtype=float)
    return dates[1:], np.diff(values)


def rolling_median_mad(values: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling median and rolling MAD (median absolute deviation from the
    rolling median).  Early positions are ``NaN`` (min_periods = window)."""
    series = pd.Series(values)
    median = series.rolling(window, min_periods=window).median()
    mad = (series - median).abs().rolling(window, min_periods=window).median()
    return median.to_numpy(), mad.to_numpy()


def robust_scale(mad: np.ndarray) -> np.ndarray:
    """1.4826 * MAD floored at EPS so a flat window cannot divide by zero."""
    return np.maximum(mad * MAD_SCALE, EPS)


def _cusum(innovations: np.ndarray, slack: float) -> tuple[np.ndarray, np.ndarray]:
    """Two-sided CUSUM driven by pre-standardised innovations.

    ``pos`` accumulates upward pressure, ``neg`` downward pressure; both reset
    at zero.  NaN innovations (warm-up) contribute nothing.
    """
    pos = np.zeros_like(innovations, dtype=float)
    neg = np.zeros_like(innovations, dtype=float)
    up = 0.0
    down = 0.0
    for i, value in enumerate(innovations):
        if not np.isfinite(value):
            pos[i] = up
            neg[i] = down
            continue
        up = max(0.0, up + value - slack)
        down = max(0.0, down - value - slack)
        pos[i] = up
        neg[i] = down
    return pos, neg


def huber_cusum(
    values: np.ndarray, window: int, c: float = HUBER_C, slack: float = CUSUM_SLACK
) -> tuple[np.ndarray, np.ndarray]:
    """Huber-clipped rolling-MAD CUSUM accumulators (positive, negative)."""
    median, mad = rolling_median_mad(values, window)
    standardised = (values - median) / robust_scale(mad)
    clipped = np.clip(standardised, -c, c)
    return _cusum(clipped, slack)


def raw_cusum(
    values: np.ndarray, window: int, slack: float = CUSUM_SLACK
) -> tuple[np.ndarray, np.ndarray]:
    """Un-clipped rolling-MAD CUSUM accumulators (positive, negative)."""
    median, mad = rolling_median_mad(values, window)
    standardised = (values - median) / robust_scale(mad)
    return _cusum(standardised, slack)


def volatility_ratio(
    values: np.ndarray, short: int = VOL_SHORT, long: int = VOL_LONG
) -> np.ndarray:
    """short-horizon rolling std / long-horizon rolling std."""
    series = pd.Series(values)
    short_std = series.rolling(short, min_periods=short).std(ddof=1)
    long_std = series.rolling(long, min_periods=long).std(ddof=1)
    ratio = short_std / long_std.replace(0.0, np.nan)
    return ratio.to_numpy(dtype=float)


@dataclass
class DetectorPanel:
    """Pre-computed detector surface for one series and one window size."""

    series_id: str
    window: int
    dates: np.ndarray
    values: np.ndarray
    huber_pos: np.ndarray
    huber_neg: np.ndarray
    cusum_pos: np.ndarray
    cusum_neg: np.ndarray
    vol_ratio: np.ndarray
    meta: dict[str, object] = field(default_factory=dict)


def build_panel(series: pd.Series, window: int) -> DetectorPanel:
    dates, values = first_difference(series)
    huber_pos, huber_neg = huber_cusum(values, window)
    cusum_pos, cusum_neg = raw_cusum(values, window)
    ratio = volatility_ratio(values)
    return DetectorPanel(
        series_id=str(series.name),
        window=window,
        dates=dates,
        values=values,
        huber_pos=huber_pos,
        huber_neg=huber_neg,
        cusum_pos=cusum_pos,
        cusum_neg=cusum_neg,
        vol_ratio=ratio,
        meta={
            "n": int(values.size),
            "start": str(pd.Timestamp(dates[0]).date()) if dates.size else None,
            "end": str(pd.Timestamp(dates[-1]).date()) if dates.size else None,
            "huber_c": HUBER_C,
            "mad_scale": MAD_SCALE,
            "cusum_slack": CUSUM_SLACK,
            "vol_short": VOL_SHORT,
            "vol_long": VOL_LONG,
        },
    )


def build_panels(
    series_map: dict[str, pd.Series], windows: tuple[int, ...] = (40, 60)
) -> dict[tuple[str, int], DetectorPanel]:
    panels: dict[tuple[str, int], DetectorPanel] = {}
    for series_id, series in series_map.items():
        for window in windows:
            panels[(series_id, window)] = build_panel(series.rename(series_id), window)
    return panels
