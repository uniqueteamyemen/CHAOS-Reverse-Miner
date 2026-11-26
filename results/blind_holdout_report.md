# Blind Holdout Report - CHAOS-Reverse-Miner

Generated: `2026-11-26T18:52:30+00:00`

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

| Split | Definition | Anchors |
|---|---|---|
| Train (mined) | anchors before 2005-01-01 | 12 |
| Blind holdout | named post-2005 shocks | 6 |
| Holdout (all) | every registered anchor >= 2005-01-01 | 8 |

### Train anchors (12)

```
NBER_1969_PEAK
NBER_1973_PEAK
NBER_1980_PEAK
NBER_1981_PEAK
NBER_1990_PEAK
NBER_2001_PEAK
PAYEMS_PEAK_1969
PAYEMS_PEAK_1973
PAYEMS_PEAK_1980
PAYEMS_PEAK_1981
PAYEMS_PEAK_1990
PAYEMS_PEAK_2001
```

### Blind holdout anchors (6)

```
NBER_2007_PEAK
GFC_LEHMAN_2008
NBER_2020_PEAK
COVID_ICSA_2020
OIL_DECLINE_2014
UKRAINE_OIL_2022
```

## 2. Data provenance (SHA-256 of the exact raw bytes used)

| Series | Start | End | Rows | SHA-256 (prefix) |
|---|---|---|---|---|
| UNRATE | 1948-01-01 | 2026-10-01 | 945 | `fb669461abc34e00...` |
| PAYEMS | 1939-01-01 | 2026-10-01 | 1054 | `968f9252d00246e2...` |
| ICSA | 1967-01-07 | 2026-11-21 | 3125 | `0de31121f29da6ba...` |
| WCOILBRENTEU | 1987-05-15 | 2026-09-04 | 2052 | `72a5e568a787f6ba...` |

## 3. Training leaderboard (pre-2005 only)

| Rank | Formula | Hits | Hit rate | False alarms | Score |
|---|---|---|---|---|---|
| 1 | CUSUM_ONLY(window=40, lead=3, cusum_thr=6.0) | 5/12 | 41.67% | 44 | 6.47 |
| 2 | CUSUM_ONLY(window=40, lead=6, cusum_thr=6.0) | 5/12 | 41.67% | 44 | 6.47 |
| 3 | CUSUM_ONLY(window=40, lead=12, cusum_thr=6.0) | 5/12 | 41.67% | 44 | 6.47 |

Top 3 frozen candidates: B1, B2, B3.

## 4. Blind holdout results (parameters frozen on pre-2005)

| Candidate | Formula | Hits | Hit rate | False alarms | Score |
|---|---|---|---|---|---|
| B1 | CUSUM_ONLY(window=40, lead=3, cusum_thr=6.0) | 4/6 | 66.67% | 89 | -4.53 |
| B2 | CUSUM_ONLY(window=40, lead=6, cusum_thr=6.0) | 4/6 | 66.67% | 89 | -4.53 |
| B3 | CUSUM_ONLY(window=40, lead=12, cusum_thr=6.0) | 4/6 | 66.67% | 89 | -4.53 |

Best blind score: **-4.53**; mean holdout hits across the
3 frozen candidates: **4.00 / 6**.

### Per-event blind matrix

| Candidate | NBER_2007_PEAK | GFC_LEHMAN_2008 | OIL_DECLINE_2014 | NBER_2020_PEAK | COVID_ICSA_2020 | UKRAINE_OIL_2022 |
|---|---|---|---|---|---|---|
| B1 | miss | hit (+0 steps) | miss | hit (-2 steps) | hit (+0 steps) | hit (-1 steps) |
| B2 | miss | hit (+0 steps) | miss | hit (-2 steps) | hit (+0 steps) | hit (-1 steps) |
| B3 | miss | hit (+0 steps) | miss | hit (-2 steps) | hit (+0 steps) | hit (-1 steps) |

`steps` is negative when the alarm fired *before* the anchor (the intended
precursor behaviour) and positive when it fired up to 2 steps after.

## 5. Reference: globally mined formulas (NOT blind - optimistic)

The formulas below come from `results/top_formulas.json`, mined on the full
timeline, so their holdout anchors were part of their own search. These numbers
are **contaminated by selection** and are shown only to contrast in-sample
optimism with the honest, frozen-parameter results above.

| Formula | Definition | Hits | Hit rate | False alarms | Score |
|---|---|---|---|---|---|
| F1 | CASCADE_HUBER_THEN_CUSUM(window=40, lead=12, huber_thr=4.0, cusum_thr=10.0) | 1/6 | 16.67% | 7 | 11.07 |
| F2 | CASCADE_HUBER_THEN_CUSUM(window=60, lead=3, huber_thr=8.0, cusum_thr=20.0) | 0/6 | 0.00% | 2 | -1.60 |
| F3 | CASCADE_HUBER_THEN_CUSUM(window=60, lead=6, huber_thr=8.0, cusum_thr=20.0) | 0/6 | 0.00% | 2 | -1.60 |

## 6. Verdict

- Blind recall of the frozen candidates: **B1 4/6, B2 4/6, B3 4/6**.
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
