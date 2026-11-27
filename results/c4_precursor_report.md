# Cycle C4 - StateVector & Backward Precursor Discovery

Generated: `2026-11-27T19:22:38+00:00`

All features are continuous; every threshold below is an **empirical quantile of
the feature's own pre-2005 history**. No external level (`0.35`, `-0.22`, `-6.0`)
is hardcoded anywhere in `core/detectors.py` or this miner.

## 1. Pre-event windows

`results/c4_pre_event_windows.csv` contains every anchor's StateVector values
across `[anchor - 12, anchor + 2]` steps
(375 rows, 25 anchors).

## 2. Empirical calibration (pre-2005 only) and cold test

33 (source, feature, direction) combinations were calibrated;
thresholds come from the quantile grid (0.9, 0.95, 0.975, 0.99, 0.995, 0.999). Qualification requires at
least one training catch and `false_alarms_per_1000 <= 12.0`.

| Source | Feature | Dir | Quantile | Train catches | Train FA/1000 | Cold catches | Cold FA | Qualified |
|---|---|---|---|---|---|---|---|---|
| WCOILBRENTEU | ph_up | 1 | q0.995 | 1/1 | 0.00 | 0/1 | 7 | yes |
| WCOILBRENTEU | l1_slope_break | 1 | q0.999 | 1/1 | 0.00 | 0/1 | 5 | yes |
| PAYEMS | trend_slope_z | -1 | q0.995 | 1/6 | 0.00 | 1/2 | 0 | yes |
| PAYEMS | l1_slope_break | -1 | q0.995 | 1/6 | 1.26 | 0/2 | 2 | yes |
| UNRATE | trend_slope_z | 1 | q0.99 | 1/6 | 1.46 | 1/3 | 0 | yes |
| WCOILBRENTEU | trend_slope_z | 1 | q0.95 | 1/1 | 7.61 | 1/1 | 12 | yes |
| FUSION | icsa_payems_fusion | 1 | q0.95 | 2/12 | 6.76 | 3/5 | 1 | yes |
| ICSA | sahm_floor_displacement | 1 | q0.9 | 1/4 | 8.58 | 1/1 | 8 | yes |
| ICSA | trend_slope_z | 1 | q0.999 | 0/4 | 0.00 | 1/1 | 1 | no |
| ICSA | ph_down | 1 | q0.999 | 0/4 | 0.51 | 0/1 | 1 | no |
| ICSA | l1_slope_break | 1 | q0.999 | 0/4 | 0.51 | 1/1 | 2 | no |
| ICSA | ph_up | 1 | q0.999 | 0/4 | 1.01 | 1/1 | 0 | no |
| WCOILBRENTEU | sahm_floor_displacement | 1 | q0.995 | 0/1 | 1.09 | 0/1 | 2 | no |
| WCOILBRENTEU | ph_down | 1 | q0.99 | 0/1 | 1.09 | 0/1 | 3 | no |
| PAYEMS | drawdown | -1 | q0.995 | 0/6 | 1.26 | 1/2 | 0 | no |
| PAYEMS | drawdown_norm | -1 | q0.99 | 0/6 | 1.26 | 0/2 | 2 | no |
| UNRATE | sahm_floor_displacement | 1 | q0.999 | 0/6 | 1.46 | 1/3 | 0 | no |
| UNRATE | ph_up | 1 | q0.999 | 0/6 | 1.46 | 1/3 | 0 | no |
| UNRATE | ph_down | 1 | q0.995 | 0/6 | 1.46 | 0/3 | 2 | no |
| UNRATE | l1_slope_break | 1 | q0.999 | 0/6 | 1.46 | 1/3 | 1 | no |
| WCOILBRENTEU | trend_slope_z | -1 | q0.975 | 0/0 | 5.43 | 0/1 | 7 | no |
| ICSA | drawdown | 1 | q0.95 | 3/4 | 12.12 | 1/1 | 15 | no |
| ICSA | drawdown_norm | 1 | q0.95 | 3/4 | 12.12 | 1/1 | 15 | no |
| WCOILBRENTEU | drawdown | -1 | q0.95 | 0/0 | 7.61 | 0/1 | 14 | no |
| WCOILBRENTEU | drawdown_norm | -1 | q0.95 | 0/0 | 8.70 | 0/1 | 10 | no |
| WCOILBRENTEU | l1_slope_break | -1 | q0.99 | 0/0 | 9.78 | 0/1 | 8 | no |
| WCOILBRENTEU | drawdown | 1 | q0.9 | 1/1 | 19.57 | 1/1 | 24 | no |
| WCOILBRENTEU | drawdown_norm | 1 | q0.9 | 1/1 | 19.57 | 1/1 | 24 | no |
| PAYEMS | sahm_floor_displacement | -1 | q0.9 | 1/6 | 18.96 | 1/2 | 2 | no |
| UNRATE | drawdown | 1 | q0.9 | 6/6 | 29.28 | 3/3 | 5 | no |
| UNRATE | drawdown_norm | 1 | q0.9 | 6/6 | 29.28 | 3/3 | 5 | no |
| PAYEMS | ph_up | -1 | q0.9 | 6/6 | 48.04 | 2/2 | 16 | no |
| PAYEMS | ph_down | -1 | q0.9 | 2/6 | 69.53 | 1/2 | 16 | no |

## 3. Lead-lag precedence

Probability that feature A fires strictly earlier than feature B, over anchors
where both fire:

| A (earlier) | B (later) | n anchors | P(A before B) | Mean lead (steps) |
|---|---|---|---|---|
| drawdown_norm | sahm_floor_displacement | 3 | 1.00 | +3.3 |
| ph_down | trend_slope_z | 2 | 1.00 | +3.5 |
| ph_down | sahm_floor_displacement | 1 | 1.00 | +2.0 |
| ph_down | drawdown | 1 | 1.00 | +2.0 |
| ph_down | l1_slope_break | 1 | 1.00 | +6.0 |
| drawdown_norm | trend_slope_z | 5 | 0.80 | +2.4 |
| drawdown | sahm_floor_displacement | 4 | 0.75 | +2.5 |
| ph_down | ph_up | 4 | 0.75 | +3.5 |
| drawdown | trend_slope_z | 6 | 0.67 | +1.8 |
| drawdown | l1_slope_break | 3 | 0.67 | +0.7 |
| drawdown_norm | ph_up | 3 | 0.67 | +0.7 |
| drawdown_norm | l1_slope_break | 3 | 0.67 | +0.7 |
| drawdown | ph_up | 4 | 0.50 | -3.0 |
| ph_up | trend_slope_z | 5 | 0.40 | +3.2 |
| trend_slope_z | sahm_floor_displacement | 3 | 0.33 | +0.3 |

## 4. Targeted diagnostics

### 4.1 OIL_DECLINE_2014 (cold, direction -1)

| Feature | Threshold | Cold catches | Cold FA | FA/1000 | Events hit |
|---|---|---|---|---|---|
| drawdown | 0.430325 | 0/1 | 14 | 12.4 | - |
| drawdown_norm | 9.377692 | 0/1 | 10 | 8.8 | - |

The 2014 anchor sits at the *pre-decline peak*, so a precursor must fire before
the drop starts; the only admissible window is `[anchor-12, anchor+2]` weeks.
Neither drawdown variant catches it cold, and both exceed the quiet-era budget
while trying. Features missing from this table (for example `ph_down` on Brent)
had **no quantile whose pre-2005 quiet rate stayed within 12/1000**, so no
budget-compliant threshold exists at all - that is reported as uncalibrated
rather than tuned until it fits.

### 4.2 Brent latch, late 2026

v3.0 weekly formula: `CASCADE_HUBER_THEN_CUSUM(window=fast, lead=short, huber_thr=4.0, cusum_thr=4.0, cusum_slack=0.1)` (as of 2026-09-04).

| Series (last 12 weeks) | Values |
|---|---|
| v3 CUSUM- | [22.611, 25.215, 26.517, 25.285, 23.045, 20.853, 21.447, 21.924, 21.163, 20.934, 21.6, 20.296] |
| C4 PH down | [0.381, 0.413, 0.399, 0.288, 0.13, 0.0, 0.052, 0.088, 0.032, 0.016, 0.063, 0.0] |

- CUSUM is still above its alarm threshold: **True**.
- PH down returned near zero (unlatched): **True**.

This is the intended fix: Page-Hinkley decays on mean reversion, so the oil
branch resets instead of staying permanently latched as the CUSUM does.

## 5. Verdict

- The five new dimensions are computed causally and calibrated without external
  thresholds.
- Cold performance is reported as measured; no feature is promoted into the
  routed system unless it clears the same noise budget as v3.0.
