# v1.0 Audit - Release Lag and Out-of-Sample Segments

Thresholds were **not** touched. Same pinned JSON, same decision rule.

Pinned thresholds SHA-256: `189ed2ee461fcc49292d3413c0f5d6c124ea8b656415e83911278fea7b6b3277`

| Segment | Lag | ALARM | WATCH | FA/year | Verdict |
|---|---|---|---|---|---|
| 2015-2022 (out-of-sample) | zero | 3 | 4 | 0.379 | **PASS** |
| 2015-2022 (out-of-sample) | release-lag | 3 | 2 | 0.379 | **PASS** |
| 2023-2026 (protocol period) | zero | 1 | 3 | 0.267 | **PASS** |
| 2023-2026 (protocol period) | release-lag | 1 | 2 | 0.267 | **PASS** |

## 2015-2022 (out-of-sample)

### zero lag - PASS

| Start | End | Months | Triggers | Peak trend z | Peak PAYEMS CUSUM- |
|---|---|---|---|---|---|
| 2018-12 | 2019-02 | 3 | TREND_SLOPE | 3.802 | 1.284 |
| 2020-03 | 2021-06 | 16 | CROSS_SERIES_MACRO_GATE, TREND_SLOPE | 19.594 | 32.813 |
| 2022-08 | 2022-12 | 5 | CROSS_SERIES_MACRO_GATE | 0.786 | 14.495 |

### release-lag lag - PASS

| Start | End | Months | Triggers | Peak trend z | Peak PAYEMS CUSUM- |
|---|---|---|---|---|---|
| 2019-01 | 2019-03 | 3 | TREND_SLOPE | 3.802 | 1.284 |
| 2020-04 | 2021-07 | 16 | CROSS_SERIES_MACRO_GATE, TREND_SLOPE | 19.594 | 32.813 |
| 2022-09 | 2022-12 | 4 | CROSS_SERIES_MACRO_GATE | 0.786 | 13.693 |


## 2023-2026 (protocol period)

### zero lag - PASS

| Start | End | Months | Triggers | Peak trend z | Peak PAYEMS CUSUM- |
|---|---|---|---|---|---|
| 2023-01 | 2024-02 | 14 | CROSS_SERIES_MACRO_GATE | 0.648 | 14.502 |

### release-lag lag - PASS

| Start | End | Months | Triggers | Peak trend z | Peak PAYEMS CUSUM- |
|---|---|---|---|---|---|
| 2023-01 | 2024-03 | 15 | CROSS_SERIES_MACRO_GATE | 0.729 | 14.502 |

## Failure analysis

Every audited run passed its alarm and false-alarm budgets.

## Method notes

1. Release lag shifts each series index forward: monthly +1 month, weekly
   +1 week. The decision calendar stays the same, so a decision at `t` can
   only use values released by `t`.
2. The 2015-2022 segment is out-of-sample with respect to threshold
   selection (all thresholds were fixed on pre-2005 anchors), but the
   protocol budgets were written for 2023-2026; applying them here is a
   conservative audit, not a new pre-registration.
3. No threshold was re-tuned after seeing any of these results.
