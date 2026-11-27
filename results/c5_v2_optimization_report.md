# Cycle C5 v2 - Daily Grid and Signature Optimisation

## Method

- Fast layers (`T10Y2Y`, `BAA10Y`, `VIXCLS`) stay on the **daily** grid with no
  weekly bucket and no extra lag.
- Slow layers keep realistic release lags: monthly +1 month, weekly +1 week,
  carried forward onto the daily grid.
- Thresholds remain pre-2005 quantiles of each feature's own history; the two
  backward anchors (1998, 2000) only choose which feature/quantile per layer.
- **Disclosure:** all nine stations are used for the variant comparison, so the
  winning formula is an optimised in-sample signature, not a blind result.

## Layer thresholds

| Layer | Series | Grid | Feature | Threshold | Quantile | Train coverage |
|---|---|---|---|---|---|---|
| LIQUIDITY | T10Y2Y | daily | trend_slope_z | 2.099815 | q0.95 | 1 |
| CREDIT | BAA10Y | daily | ph_up | 0.148751 | q0.99 | 1 |
| VOLATILITY | VIXCLS | daily | ph_up | 0.162138 | q0.95 | 2 |
| FIN_CONDITIONS | NFCI | weekly | drawdown_norm | 0.0 | q0.95 | 1 |
| LABOR_UNRATE | UNRATE | weekly | ph_up | 0.758614 | q0.9999 | 0 |
| LABOR_PAYEMS | PAYEMS | weekly | l1_slope_break | 0.329539 | q0.9999 | 0 |
| CLAIMS | ICSA | weekly | trend_slope_z | 1.951578 | q0.95 | 2 |
| OIL | WCOILBRENTEU | weekly | ph_up | 0.432256 | q0.9999 | 0 |

## Variant comparison (top 12)

| Formula | Catches | Mean lead (days) | Quiet alarms | Alarms/year |
|---|---|---|---|---|
| weighted >= 4, 10d window | 9/9 | 15.0 | 493 | 13.437 |
| K=3 of 8, 10d window | 9/9 | 15.0 | 497 | 13.546 |
| weighted >= 5, 20d window | 9/9 | 15.8 | 550 | 14.99 |
| K=4 of 8, 20d window | 9/9 | 15.8 | 551 | 15.018 |
| weighted >= 3, 10d window | 9/9 | 16.9 | 586 | 15.972 |
| weighted >= 4, 20d window | 9/9 | 16.9 | 646 | 17.607 |
| K=3 of 8, 20d window | 9/9 | 16.9 | 651 | 17.743 |
| weighted >= 3, 20d window | 9/9 | 17.0 | 750 | 20.442 |
| weighted >= 6, 10d window | 8/9 | 16.8 | 416 | 11.338 |
| weighted >= 5, 10d window | 8/9 | 16.8 | 420 | 11.447 |
| K=4 of 8, 10d window | 8/9 | 16.8 | 421 | 11.475 |
| weighted >= 6, 20d window | 8/9 | 17.8 | 547 | 14.909 |

## Best signature (max recall)

`weighted >= 4, 10d window` - catches **9/9**,
mean lead 15.0 days, 13.437 quiet
alarms/year.

| Anchor | Date | Result | Alarm date | Lead (days) |
|---|---|---|---|---|
| LTCM_RUSSIA_1998 | 1998-08-17 | hit | 1998-07-28 | 20 |
| DOTCOM_CRASH_2000 | 2000-03-24 | hit | 2000-03-06 | 18 |
| NBER_2007_PEAK | 2007-12-01 | hit | 2007-11-12 | 19 |
| GFC_LEHMAN_2008 | 2008-09-15 | hit | 2008-08-26 | 20 |
| EURO_DEBT_US_DOWNGRADE_2011 | 2011-08-05 | hit | 2011-07-18 | 18 |
| HY_ENERGY_CRUNCH_2015 | 2015-12-11 | hit | 2015-12-10 | 1 |
| NBER_2020_PEAK | 2020-02-01 | hit | 2020-01-13 | 19 |
| UKRAINE_OIL_2022 | 2022-02-24 | hit | 2022-02-04 | 20 |
| SVB_BANKING_STRESS_2023 | 2023-03-10 | hit | 2023-03-10 | 0 |

## Balanced alternative (lower noise)

`K=6 of 8, 10d window` - catches
**7/9**
at 9.512 alarms/year, but
misses **HY_ENERGY_CRUNCH_2015, SVB_BANKING_STRESS_2023**.
Because it drops the two stations this cycle was meant to fix, it is reported as
an alternative rather than recommended.

## Live state

**QUIET** as of 2026-09-11 under the max-recall
formula.

## Deployment verdict

The daily grid fixes the two v1.0 misses (2011 and SVB 2023), but at 13.4 quiet
alarms/year it is roughly 13x the v1.0 budget. No variant on this grid combines
9/9 recall with a deployable noise level, so none is promoted into
`production/`. The C5 v1 weekly result (1/3 cold) remains the out-of-sample
reference, and this optimisation is explicitly in-sample.

## Caveat

Because the nine stations drive the comparison, the winning formula's catch
count is in-sample by construction. The honest out-of-sample reference remains
C5 v1: 1/3 cold anchors with the weekly 6-of-8 signature. Any deployment should
pre-register the chosen formula before the next unseen station.
