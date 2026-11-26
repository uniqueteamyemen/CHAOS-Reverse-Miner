# Blind Holdout Report - CHAOS-Reverse-Miner v2.0

Generated: `2026-11-26T20:10:20+00:00`

Every number below is computed by `python -m stress_test.blind_validator` from
the raw FRED bytes fingerprinted in section 2. Nothing is estimated.

## 1. Protocol

- **Strict temporal split.** Parameters are mined on anchors before 2005-01-01
  and then frozen. The six post-2005 shocks are used only for the cold test.
- **Champion rule (fixed before the cold test).** Among formulas that pass the
  engineering qualification (`hits >= 1` and `false_alarms_per_1000 <= 12.0` on
  the training era), take the one with the highest training catch rate; ties
  break on lower false-alarm rate, then higher engineering score.
- **Quiet periods** exclude every event window and its 12-month / 24-week
  aftermath (late confirmation is not a false alarm).
- **Score** = `catch_rate_pct - 15 * false_alarms_per_1000`.

| Split | Definition | Anchors |
|---|---|---|
| Train (mined) | anchors before 2005-01-01 | 12 |
| Blind holdout | named post-2005 shocks | 6 |

## 2. Data provenance

| Series | Start | End | Rows | SHA-256 (prefix) |
|---|---|---|---|---|
| UNRATE | 1948-01-01 | 2026-10-01 | 945 | `fb669461abc34e00...` |
| PAYEMS | 1939-01-01 | 2026-10-01 | 1054 | `968f9252d00246e2...` |
| ICSA | 1967-01-07 | 2026-11-21 | 3125 | `0de31121f29da6ba...` |
| WCOILBRENTEU | 1987-05-15 | 2026-09-04 | 2052 | `72a5e568a787f6ba...` |

## 3. Frozen candidates (selected on pre-2005 only)

| Formula | Train catches | Train catch rate | Train FA/1000 | Holdout catches | Holdout FA |
|---|---|---|---|---|---|
| CROSS_SERIES_MACRO_GATE(window=fast, lead=long, huber_thr=4.0, cusum_thr=4.0, cusum_slack=0.5, resonance<=2mo) | 8/12 | 66.7% | 10.25 | 2/6 | 5 |
| CROSS_SERIES_MACRO_GATE(window=fast, lead=long, huber_thr=6.0, cusum_thr=4.0, cusum_slack=0.5, resonance<=2mo) | 8/12 | 66.7% | 10.25 | 2/6 | 5 |
| CROSS_SERIES_MACRO_GATE(window=fast, lead=long, huber_thr=8.0, cusum_thr=4.0, cusum_slack=0.5, resonance<=2mo) | 8/12 | 66.7% | 10.25 | 2/6 | 5 |

## 4. Cold holdout results

The champion catches **2/6** shock(s)
with **5** false alarms in the post-2005 era.
Best cold catch among the three frozen candidates: **2/6**.

### Per-event matrix

| Candidate | NBER_2007_PEAK | GFC_LEHMAN_2008 | OIL_DECLINE_2014 | NBER_2020_PEAK | COVID_ICSA_2020 | UKRAINE_OIL_2022 |
|---|---|---|---|---|---|---|
| CROSS_SERIES_MACRO_GATE(window=fast, lead=long, huber_thr=4.0, cusum_thr=4.0, cusum_slack=0.5, resonance<=2mo) | miss | hit (-4) | miss | hit (+1) | miss | miss |
| CROSS_SERIES_MACRO_GATE(window=fast, lead=long, huber_thr=6.0, cusum_thr=4.0, cusum_slack=0.5, resonance<=2mo) | miss | hit (-4) | miss | hit (+1) | miss | miss |
| CROSS_SERIES_MACRO_GATE(window=fast, lead=long, huber_thr=8.0, cusum_thr=4.0, cusum_slack=0.5, resonance<=2mo) | miss | hit (-4) | miss | hit (+1) | miss | miss |

## 5. Catch / noise frontier (recall-first family, CUSUM threshold varied)

Recall-first profile (highest training catch): `CUSUM_ONLY(window=slow, lead=medium, cusum_thr=2.0, cusum_slack=0.25)`
- cold result: **5/6** catches,
  **192** false alarms.

This is the measured trade-off. It answers the v2.0 target directly: can the
formula reach 5/6 without crying wolf?

| CUSUM threshold | Cold catches | Cold false alarms | FA per 1000 |
|---|---|---|---|
| 2.0 | 5/6 | 192 | 52.4 |
| 3.0 | 4/6 | 145 | 39.6 |
| 4.0 | 4/6 | 111 | 30.3 |
| 6.0 | 4/6 | 73 | 19.9 |
| 10.0 | 3/6 | 36 | 9.8 |
| 20.0 | 2/6 | 6 | 1.6 |

**Result: the 5/6 point exists, but not under 40 false alarms.** The table shows
that catching the mild 2007 turn requires so much sensitivity that the same era
produces roughly 190 alarms. The noise budget (<= 40) is met only at 3/6 or
below. Both targets cannot be satisfied by this detector family on these data.

## 6. Live radar - last 12 observations (late 2026)

Champion: `CROSS_SERIES_MACRO_GATE(window=fast, lead=long, huber_thr=4.0, cusum_thr=4.0, cusum_slack=0.5, resonance<=2mo)`

Current status: UNRATE: quiet (last 3 obs), PAYEMS: quiet (last 3 obs), ICSA: quiet (last 3 obs), WCOILBRENTEU: quiet (last 3 obs).

### UNRATE

| Date | Transform value | Huber+ | CUSUM+ | Vol ratio | Accel z | Signal |
|---|---|---|---|---|---|---|
| 2025-11-01 | +0.10000 | 0.69 | 0.70 | 0.51 | 0.42 | - |
| 2025-12-01 | -0.10000 | 0.00 | 0.00 | 0.98 | -1.15 | - |
| 2026-01-01 | -0.10000 | 0.00 | 0.00 | 1.02 | -0.38 | - |
| 2026-02-01 | +0.10000 | 0.17 | 0.17 | 1.02 | 1.43 | - |
| 2026-03-01 | -0.10000 | 0.00 | 0.00 | 0.99 | -0.52 | - |
| 2026-04-01 | +0.00000 | 0.00 | 0.00 | 0.88 | 0.35 | - |
| 2026-05-01 | +0.00000 | 0.00 | 0.00 | 0.51 | 0.21 | - |
| 2026-06-01 | -0.10000 | 0.00 | 0.00 | 0.51 | -0.46 | - |
| 2026-07-01 | -0.10000 | 0.00 | 0.00 | 0.51 | -0.08 | - |
| 2026-08-01 | +0.00000 | 0.00 | 0.00 | 0.52 | 0.95 | - |
| 2026-09-01 | +0.10000 | 0.17 | 0.17 | 0.97 | 1.64 | - |
| 2026-10-01 | -0.10000 | 0.00 | 0.00 | 0.95 | -1.05 | - |

### PAYEMS

| Date | Transform value | Huber- | CUSUM- | Vol ratio | Accel z | Signal |
|---|---|---|---|---|---|---|
| 2025-11-01 | +0.00026 | 1.09 | 13.73 | 1.34 | -1.17 | - |
| 2025-12-01 | -0.00011 | 1.11 | 13.75 | 1.06 | -0.04 | - |
| 2026-01-01 | +0.00101 | 0.00 | 12.20 | 0.98 | -1.56 | - |
| 2026-02-01 | -0.00098 | 0.84 | 13.46 | 1.53 | 1.86 | - |
| 2026-03-01 | +0.00135 | 0.00 | 11.60 | 1.85 | -2.01 | - |
| 2026-04-01 | +0.00093 | 0.00 | 10.35 | 1.78 | -0.23 | - |
| 2026-05-01 | +0.00040 | 0.00 | 9.77 | 0.70 | 0.53 | - |
| 2026-06-01 | +0.00020 | 0.00 | 9.36 | 0.62 | 0.46 | - |
| 2026-07-01 | -0.00006 | 0.04 | 9.40 | 0.38 | 0.63 | - |
| 2026-08-01 | +0.00117 | 0.00 | 7.50 | 1.01 | -1.60 | - |
| 2026-09-01 | +0.00038 | 0.00 | 6.90 | 0.97 | 0.18 | - |
| 2026-10-01 | +0.00104 | 0.00 | 5.46 | 0.64 | -1.01 | - |

### ICSA

| Date | Transform value | Huber+ | CUSUM+ | Vol ratio | Accel z | Signal |
|---|---|---|---|---|---|---|
| 2026-09-05 | +0.00000 | 0.00 | 0.00 | 0.60 | -0.03 | - |
| 2026-09-12 | -0.04445 | 0.00 | 0.00 | 0.74 | -0.69 | - |
| 2026-09-19 | +0.00000 | 0.00 | 0.00 | 0.44 | 0.39 | - |
| 2026-09-26 | +0.00504 | 0.00 | 0.00 | 0.44 | 0.32 | - |
| 2026-10-03 | -0.00504 | 0.00 | 0.00 | 0.43 | -0.02 | - |
| 2026-10-10 | +0.01005 | 0.00 | 0.00 | 0.44 | 0.29 | - |
| 2026-10-17 | -0.02532 | 0.00 | 0.00 | 0.46 | -0.54 | - |
| 2026-10-24 | +0.00000 | 0.00 | 0.00 | 0.29 | 0.15 | - |
| 2026-10-31 | -0.02073 | 0.00 | 0.00 | 0.34 | -0.30 | - |
| 2026-11-07 | +0.05599 | 0.84 | 0.97 | 0.68 | 1.48 | - |
| 2026-11-14 | +0.00000 | 0.34 | 0.47 | 0.68 | -0.42 | - |
| 2026-11-21 | -0.06652 | 0.00 | 0.00 | 0.91 | -1.53 | - |

### WCOILBRENTEU

| Date | Transform value | Huber+ | CUSUM+ | Vol ratio | Accel z | Signal |
|---|---|---|---|---|---|---|
| 2026-06-19 | -0.14629 | 0.00 | 3.58 | 1.14 | -1.95 | - |
| 2026-06-26 | -0.09540 | 0.00 | 0.38 | 0.97 | -0.03 | - |
| 2026-07-03 | -0.05485 | 0.00 | 0.00 | 0.85 | 0.87 | - |
| 2026-07-10 | +0.05077 | 0.63 | 0.63 | 1.01 | 2.43 | - |
| 2026-07-17 | +0.12303 | 1.48 | 2.27 | 1.33 | 2.37 | - |
| 2026-07-24 | +0.14760 | 2.32 | 3.86 | 1.55 | 1.54 | - |
| 2026-07-31 | -0.04784 | 1.13 | 2.67 | 1.29 | -2.48 | - |
| 2026-08-07 | -0.04201 | 0.05 | 1.59 | 1.15 | -1.01 | - |
| 2026-08-14 | +0.05157 | 0.21 | 1.75 | 1.02 | 0.79 | - |
| 2026-08-21 | +0.01810 | 0.00 | 1.38 | 1.04 | -0.06 | - |
| 2026-08-28 | -0.04862 | 0.00 | 0.12 | 0.98 | -0.91 | - |
| 2026-09-04 | +0.09922 | 0.70 | 0.82 | 0.77 | 1.39 | - |

*`Huber+` and `CUSUM+` are the upward accumulators; downward pressure is the
mirror image. `Signal` marks a step where the frozen champion fires.*

## 7. Verdict against the v2.0 targets

| Target | Required | Champion | Recall-first profile | Status |
|---|---|---|---|---|
| Unseen post-2005 catches | >= 5/6 | 2/6 | 5/6 | catch target met only by the noisy profile |
| False alarms (cold test) | <= 40 | 5 | 192 | noise target met only by the champion |

The champion is quiet but only catches 2/6; the recall-first
profile catches 5/6 but fires 192
times. **No single formula in this search satisfies both v2.0 targets**, and this
report does not claim otherwise.

## 8. Limitations

1. Twenty anchors, six of them in the holdout: this is a sanity check, not a
   statistical proof.
2. Weekly series (ICSA, Brent) contain **no pre-2005 anchor** of their own, so
   their thresholds are inherited from monthly evidence. Their cold behaviour is
   genuinely out-of-sample but untuned.
3. Anchors use official event dates; a live system faces release lags.
4. The threshold grid is coarse and the score's 15-point penalty is calibrated
   to a different exposure than 78 years of monthly data, which is why the
   champion rule is stated explicitly instead of relying on the raw score.
