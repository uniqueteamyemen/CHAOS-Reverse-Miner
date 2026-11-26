"""Continuous, non-frozen, frequency-aware detectors (v2.0).

v2.0 engineering changes
-----------------------
* **Scale-invariant transforms.**  `PAYEMS`, `ICSA` and `WCOILBRENTEU` are
  modelled as log-returns (`diff(log(x))`), so a 3% shock in 1974 and a 3%
  shock in 2020 produce the same innovation.  `UNRATE` stays on first
  differences because it is already a rate and can sit at zero.
* **Frequency-aware windows.**  Monthly series use 18/24/36-month baselines,
  weekly series 36/52-week baselines.  Volatility ratios and accelerations are
  parameterised by the same window, so monthly and weekly signals are not
  compared on a mismatched horizon.
* **Momentum acceleration.**  An EMA(3) of the transformed series is
  differenced and robustly standardised; slow pre-recession deterioration shows
  up here before it shows up in the level break.
* **Rolling, never frozen.**  Every median, MAD and threshold is recomputed
  inside its own window, eliminating the static-baseline saturation of the
  original design.

All arrays are aligned with ``dates`` and have identical length.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

HUBER_C = 1.345
MAD_SCALE = 1.4826
CUSUM_SLACK = 0.5
ACCEL_SPAN = 3
Z_CAP = 40.0
EPS = 1e-8


# --------------------------------------------------------------------------- #
# Transformations
# --------------------------------------------------------------------------- #
def transform_series(series: pd.Series, method: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (dates, transformed values) for one series.

    ``diff``       - first difference (used for UNRATE, a rate in percent)
    ``log_return`` - first difference of the log (relative change)
    """
    values = series.to_numpy(dtype=float)
    if values.size < 2:
        raise ValueError(f"{series.name}: need at least 2 observations")
    if method == "diff":
        transformed = np.diff(values)
    elif method == "log_return":
        if np.any(values <= 0):
            raise ValueError(f"{series.name}: log_return requires strictly positive values")
        transformed = np.diff(np.log(values))
    else:
        raise ValueError(f"unknown transform {method!r}")
    dates = series.index.to_numpy()[1:]
    return dates, transformed


# --------------------------------------------------------------------------- #
# Rolling robust baseline
# --------------------------------------------------------------------------- #
def rolling_median_mad(values: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling median and rolling MAD of deviations from that median."""
    series = pd.Series(values)
    median = series.rolling(window, min_periods=window).median()
    mad = (series - median).abs().rolling(window, min_periods=window).median()
    return median.to_numpy(), mad.to_numpy()


def robust_scale(mad: np.ndarray, std: np.ndarray | None = None) -> np.ndarray:
    """Robust scale with a variance floor.

    A rolling MAD can be exactly zero when more than half of a window shares the
    same rounded value (very common with 0.1-step monthly releases).  Dividing by
    ``max(MAD, EPS)`` then produces z-scores around 1e7 and permanently saturates
    the CUSUM.  Flooring at a quarter of the rolling standard deviation keeps the
    scale honest without reintroducing outlier sensitivity.
    """
    scale = np.maximum(mad * MAD_SCALE, EPS * 10)
    if std is not None:
        scale = np.maximum(scale, 0.25 * np.nan_to_num(std, nan=0.0))
    return scale


def robust_standardize(
    values: np.ndarray, window: int
) -> tuple[np.ndarray, np.ndarray]:
    """(median, z) where z uses the floored robust scale; z is capped at ±Z_CAP."""
    series = pd.Series(values)
    median = series.rolling(window, min_periods=window).median().to_numpy()
    mad = (series - pd.Series(median)).abs().rolling(window, min_periods=window).median().to_numpy()
    std = series.rolling(window, min_periods=window).std(ddof=1).to_numpy()
    z = (values - median) / robust_scale(mad, std)
    return median, np.clip(z, -Z_CAP, Z_CAP)


def _cusum(innovations: np.ndarray, slack: float) -> tuple[np.ndarray, np.ndarray]:
    """Two-sided CUSUM of pre-standardised innovations; resets at zero."""
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
    """Huber-clipped rolling-MAD CUSUM accumulators."""
    _, z = robust_standardize(values, window)
    return _cusum(np.clip(z, -c, c), slack)


def raw_cusum(
    values: np.ndarray, window: int, slack: float = CUSUM_SLACK
) -> tuple[np.ndarray, np.ndarray]:
    """Un-clipped rolling-MAD CUSUM accumulators (z capped at ±Z_CAP)."""
    _, z = robust_standardize(values, window)
    return _cusum(z, slack)


def volatility_ratio(values: np.ndarray, window: int) -> np.ndarray:
    """short-horizon rolling std / window rolling std (default short = window/6)."""
    short = max(3, int(round(window / 6)))
    series = pd.Series(values)
    short_std = series.rolling(short, min_periods=short).std(ddof=1)
    long_std = series.rolling(window, min_periods=window).std(ddof=1)
    return (short_std / long_std.replace(0.0, np.nan)).to_numpy(dtype=float)


def momentum_acceleration(values: np.ndarray, window: int, span: int = ACCEL_SPAN) -> np.ndarray:
    """Robust z-score of the rate of change of an EMA-smoothed series.

    EMA(span) kills single-print noise; its first difference is the momentum
    acceleration; dividing by the rolling MAD of that acceleration makes the
    signal comparable across decades and frequencies.
    """
    ema = pd.Series(values).ewm(span=span, adjust=False).mean().to_numpy()
    accel = np.concatenate([[np.nan], np.diff(ema)])
    _, z = robust_standardize(accel, window)
    return z


def trend_slope_z(series: pd.Series, k: int, baseline: int = 24) -> np.ndarray:
    """Robust z-score of the rolling OLS slope of the *level* series.

    A k-step slope turns positive while the level is still near its trough, so it
    catches slow turn-arounds (2007, 2001) that a difference-based CUSUM only
    sees after the level has already moved.  The slope is standardised against a
    rolling median/MAD of its own history (baseline window), with the same
    variance floor as the other detectors.
    """
    values = series.to_numpy(dtype=float)
    n = values.size
    slopes = np.full(n, np.nan)
    if n >= k and k >= 2:
        x = np.arange(k, dtype=float)
        xc = x - x.mean()
        denominator = float(np.dot(xc, xc))
        for i in range(k - 1, n):
            slopes[i] = float(np.dot(xc, values[i - k + 1 : i + 1]) / denominator)
    slope_series = pd.Series(slopes)
    median = slope_series.rolling(baseline, min_periods=baseline).median()
    mad = (slope_series - median).abs().rolling(baseline, min_periods=baseline).median()
    std = slope_series.rolling(baseline, min_periods=baseline).std(ddof=1)
    z = (slopes - median.to_numpy()) / robust_scale(
        mad.to_numpy(), std.to_numpy()
    )
    return np.clip(z, -Z_CAP, Z_CAP)


# --------------------------------------------------------------------------- #
# Panel
# --------------------------------------------------------------------------- #
@dataclass
class DetectorPanel:
    """Full detector surface for one series, one transform and one window."""

    series_id: str
    window: int
    frequency: str
    transform: str
    dates: np.ndarray
    values: np.ndarray
    z: np.ndarray
    huber_pos: np.ndarray
    huber_neg: np.ndarray
    vol_ratio: np.ndarray
    accel_z: np.ndarray
    meta: dict[str, object]


def cusum_from_z(z: np.ndarray, slack: float) -> tuple[np.ndarray, np.ndarray]:
    """Two-sided CUSUM on an already-standardised series (cached by the miner)."""
    return _cusum(z, slack)


def build_panel(
    series: pd.Series, window: int, frequency: str, transform: str
) -> DetectorPanel:
    dates, values = transform_series(series.rename(series.name), transform)
    _, z = robust_standardize(values, window)
    huber_pos, huber_neg = _cusum(np.clip(z, -HUBER_C, HUBER_C), CUSUM_SLACK)
    ratio = volatility_ratio(values, window)
    accel = momentum_acceleration(values, window)
    return DetectorPanel(
        series_id=str(series.name),
        window=window,
        frequency=frequency,
        transform=transform,
        dates=dates,
        values=values,
        z=z,
        huber_pos=huber_pos,
        huber_neg=huber_neg,
        vol_ratio=ratio,
        accel_z=accel,
        meta={
            "n": int(values.size),
            "start": str(pd.Timestamp(dates[0]).date()) if dates.size else None,
            "end": str(pd.Timestamp(dates[-1]).date()) if dates.size else None,
            "transform": transform,
            "frequency": frequency,
            "huber_c": HUBER_C,
            "mad_scale": MAD_SCALE,
            "cusum_slack": CUSUM_SLACK,
            "accel_span": ACCEL_SPAN,
            "z_cap": Z_CAP,
        },
    )


def build_panels(
    series_map: dict[str, pd.Series],
    profiles: dict[str, dict[str, object]],
    frequency_profiles: dict[str, dict[str, object]] | None = None,
) -> dict[tuple[str, int], DetectorPanel]:
    """Build every (series, window) panel needed by the mining grid.

    ``profiles`` carries each series' frequency and transform; window lists come
    from ``frequency_profiles`` unless a series overrides them.
    """
    if frequency_profiles is None:
        from core import data_loader as _data_loader

        frequency_profiles = _data_loader.get_frequency_profiles()
    panels: dict[tuple[str, int], DetectorPanel] = {}
    for series_id, series in series_map.items():
        profile = profiles[series_id]
        frequency = str(profile["frequency"])
        windows = profile.get("windows") or frequency_profiles[frequency]["windows"]
        for window in windows:  # type: ignore[union-attr]
            panels[(series_id, int(window))] = build_panel(
                series.rename(series_id),
                int(window),
                frequency,
                str(profile["transform"]),
            )
    return panels


# --------------------------------------------------------------------------- #
# C4 StateVector: continuous features, no hardcoded alarm thresholds
# --------------------------------------------------------------------------- #
@dataclass
class StateVector:
    series_id: str
    frequency: str
    dates: np.ndarray
    features: dict[str, np.ndarray]
    meta: dict[str, object]


PH_ALPHA = 0.1


def _rolling_slope(values: np.ndarray, window: int) -> np.ndarray:
    n = values.size
    out = np.full(n, np.nan)
    if n < window or window < 2:
        return out
    x = np.arange(window, dtype=float)
    xc = x - x.mean()
    denominator = float(np.dot(xc, xc))
    for i in range(window - 1, n):
        chunk = values[i - window + 1 : i + 1]
        if not np.all(np.isfinite(chunk)):
            continue
        out[i] = float(np.dot(xc, chunk) / denominator)
    return out


def sahm_floor_displacement(series: pd.Series, frequency: str) -> np.ndarray:
    """MA3 above its 12m floor (monthly) or MA4 / 52w floor - 1 (weekly)."""
    levels = pd.Series(series.to_numpy(dtype=float))
    if frequency == "monthly":
        short = levels.rolling(3, min_periods=3).mean()
        floor = short.rolling(12, min_periods=12).min()
        return (short - floor).to_numpy()
    short = levels.rolling(4, min_periods=4).mean()
    floor = short.rolling(52, min_periods=52).min()
    return (short / floor.replace(0.0, np.nan) - 1.0).to_numpy()


def page_hinkley(values: np.ndarray, alpha: float = PH_ALPHA) -> tuple[np.ndarray, np.ndarray]:
    """One-sided Page-Hinkley accumulators with EWMA mean and decay on reversion."""
    n = values.size
    up = np.zeros(n)
    down = np.zeros(n)
    mean = np.nan
    for i, value in enumerate(values):
        if not np.isfinite(value):
            up[i] = up[i - 1] if i else 0.0
            down[i] = down[i - 1] if i else 0.0
            continue
        mean = value if not np.isfinite(mean) else alpha * value + (1.0 - alpha) * mean
        deviation = value - mean
        previous_up = up[i - 1] if i else 0.0
        previous_down = down[i - 1] if i else 0.0
        up[i] = max(0.0, (1.0 - alpha) * previous_up + deviation)
        down[i] = max(0.0, (1.0 - alpha) * previous_down - deviation)
    return up, down


def drawdown_features(series: pd.Series, frequency: str) -> tuple[np.ndarray, np.ndarray]:
    levels = series.to_numpy(dtype=float)
    window = 12 if frequency == "monthly" else 52
    frame = pd.Series(levels)
    peak = frame.rolling(window, min_periods=window).max()
    drawdown = (frame - peak) / peak.replace(0.0, np.nan)
    ratio = frame / frame.shift(1)
    log_return = np.log(ratio.where(ratio > 0))
    volatility = log_return.rolling(window, min_periods=window).std(ddof=1)
    normalised = drawdown / volatility.replace(0.0, np.nan)
    return drawdown.to_numpy(), normalised.to_numpy()


def l1_slope_break(values: np.ndarray, frequency: str) -> np.ndarray:
    """|slope_recent - slope_prior| / MAD(diff) over the trailing window."""
    window = 24 if frequency == "monthly" else 44
    half = max(3, window // 2)
    recent = _rolling_slope(values, half)
    prior = _rolling_slope(values, half)
    shifted = np.full(values.size, np.nan)
    shifted[half:] = prior[:-half]
    magnitude = np.abs(recent - shifted)
    differences = pd.Series(np.diff(values, prepend=np.nan))
    median = differences.rolling(window, min_periods=window).median()
    mad = (differences - median).abs().rolling(window, min_periods=window).median()
    std = differences.rolling(window, min_periods=window).std(ddof=1)
    return magnitude / robust_scale(mad.to_numpy(), std.to_numpy())


def build_series_state(series: pd.Series, frequency: str, transform: str) -> StateVector:
    dates, conditioned = transform_series(series.rename(series.name), transform)
    sahm = sahm_floor_displacement(series, frequency)[1:]
    drawdown, drawdown_norm = drawdown_features(series, frequency)
    drawdown, drawdown_norm = drawdown[1:], drawdown_norm[1:]
    ph_up, ph_down = page_hinkley(conditioned)
    slope_break = l1_slope_break(conditioned, frequency)
    trend = trend_slope_z(series, 6 if frequency == "monthly" else 12, 24)[1:]
    return StateVector(
        series_id=str(series.name),
        frequency=frequency,
        dates=dates,
        features={
            "sahm_floor_displacement": sahm,
            "ph_up": ph_up,
            "ph_down": ph_down,
            "drawdown": drawdown,
            "drawdown_norm": drawdown_norm,
            "l1_slope_break": slope_break,
            "trend_slope_z": trend,
        },
        meta={
            "frequency": frequency,
            "transform": transform,
            "ph_alpha": PH_ALPHA,
            "n": int(conditioned.size),
            "start": str(pd.Timestamp(dates[0]).date()) if dates.size else None,
            "end": str(pd.Timestamp(dates[-1]).date()) if dates.size else None,
        },
    )


def build_fusion_state(icsa: pd.Series, payems: pd.Series) -> StateVector:
    """Monthly ICSA 4-week surge aligned with a 3-month PAYEMS contraction."""
    icsa_four = icsa.rolling(4, min_periods=4).mean()
    baseline = icsa_four.rolling(52, min_periods=52).median()
    surge = icsa_four / baseline.replace(0.0, np.nan) - 1.0
    surge_month = surge.resample("ME").max()
    pay_month = payems.resample("ME").last()
    contraction = -np.log(pay_month / pay_month.shift(3))
    joined = pd.concat(
        [surge_month.rename("surge"), contraction.rename("contraction")], axis=1
    ).dropna()
    raw = joined["surge"] * joined["contraction"]
    median = raw.rolling(36, min_periods=24).median()
    mad = (raw - median).abs().rolling(36, min_periods=24).median()
    std = raw.rolling(36, min_periods=24).std(ddof=1)
    fusion = (raw - median) / robust_scale(mad.to_numpy(), std.to_numpy())
    return StateVector(
        series_id="FUSION",
        frequency="monthly",
        dates=joined.index.to_numpy(),
        features={"icsa_payems_fusion": np.clip(fusion.to_numpy(), -Z_CAP, Z_CAP)},
        meta={"components": ["ICSA_4w_surge", "-PAYEMS_3m_log_return"], "n": int(len(joined))},
    )


def build_state_vectors(
    series_map: dict[str, pd.Series],
    profiles: dict[str, dict[str, object]],
) -> dict[str, StateVector]:
    vectors: dict[str, StateVector] = {}
    for series_id, series in series_map.items():
        profile = profiles[series_id]
        vectors[series_id] = build_series_state(
            series.rename(series_id), str(profile["frequency"]), str(profile["transform"])
        )
    if "ICSA" in series_map and "PAYEMS" in series_map:
        vectors["FUSION"] = build_fusion_state(series_map["ICSA"], series_map["PAYEMS"])
    return vectors
