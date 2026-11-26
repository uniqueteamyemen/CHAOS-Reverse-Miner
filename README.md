# CHAOS-Reverse-Miner

![Run Reverse Mining](https://github.com/uniqueteamyemen/CHAOS-Reverse-Miner/actions/workflows/run_reverse_mining.yml/badge.svg)

> **Post-mortem-first detector discovery for economic and structural time series.**
> Start from what actually happened, walk backwards, and keep only the detector
> formulas that would have fired on time without crying wolf.

Isolated research repository — no fork, submodule, or workflow link to
`uniqueteamyemen/CHAOS`, and it never writes there.

**بالعربية:** مستودع مستقل تماماً. النسخة 2.0 تُصلح عيب تشبّع حقيقياً في الكاشفات،
وتضيف تحويلات مقياسية (عوائد لوغاريتمية)، ونوافذ واعية بالتردد الشهري/الأسبوعي،
وكاشف تسارع الزخم، ورنيناً عمالياً بين البطالة والتوظيف. كل الأرقام محسوبة من
بيانات FRED الرسمية، والتقرير الأعمى يُعلن بصراحة أن هدفَي 5/6 و«≤40 إنذاراً»
غير قابلين للتحقق معاً على هذه البيانات، ويعرض الحدّ الفاصل المقيس.

---

## 1. What v2.0 changes

v1.0 worked but had three engineering defects that v2.0 fixes:

| # | v1.0 defect | v2.0 fix |
|---|---|---|
| 1 | **Scale inflation** — raw differences of growing series made 2020s shocks 100× the 1970s ones | `PAYEMS`, `ICSA`, `WCOILBRENTEU` now use log-returns; `UNRATE` keeps first differences |
| 2 | **Frequency mismatch** — monthly and weekly series shared window 40 / cooldown 6 | window policies `fast/medium/slow` = 18/24/36 months or 36/44/52 weeks; gaps 3/6 steps; cooldowns 6/12 |
| 3 | **Siloed signals** — every series fired alone | cross-series labour resonance (UNRATE up + PAYEMS down within ±2 months) and the new momentum-acceleration cascade |

It also fixes a **critical detector bug** found while profiling v1.0: when more
than half of a rolling window shares the same rounded value, the MAD is exactly
zero and z-scores explode to ~10⁷. The CUSUM then saturates permanently — the
UNRATE accumulator reached **1.4×10⁸** and never reset, which is where most of
v1.0's "false alarms" came from. The scale is now floored at 0.25× the rolling
standard deviation and z is capped at ±40.

## 2. Repository layout

```text
CHAOS-Reverse-Miner/
├── README.md
├── requirements.txt
├── .gitignore
├── .github/workflows/run_reverse_mining.yml
├── core/
│   ├── __init__.py
│   ├── data_loader.py      # FRED access, series/frequency profiles, 20 anchors
│   ├── detectors.py        # log-returns, rolling Huber/z-CUSUM, vol ratio, acceleration
│   └── reverse_miner.py    # 4,572-formula policy grid, resonance gate, scoring
├── stress_test/
│   ├── __init__.py
│   └── blind_validator.py  # pre-2005 mining, cold post-2005 test, live radar
└── results/
    ├── reverse_mining_leaderboard.csv
    ├── top_formulas.json
    └── blind_holdout_report.md
```

## 3. Data provenance

| Series | FRED ID | Transform | Frequency |
|---|---|---|---|
| Unemployment rate | `UNRATE` | first difference | monthly (945 obs) |
| Nonfarm payrolls | `PAYEMS` | log-return | monthly (1,054 obs) |
| Initial claims | `ICSA` | log-return | weekly (3,125 obs) |
| Brent crude | `WCOILBRENTEU` | log-return | weekly (2,052 obs) |

Official public endpoint, no key:

```text
https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>
```

If FRED is unreachable and no cached copy exists, the loader raises; it never
fabricates a series. Every run stores the SHA-256, row count and date span of
each raw CSV in `results/top_formulas.json` and in the blind report.

## 4. Detectors

All detectors run on the conditioned series (log-return or difference) with a
rolling, never-frozen baseline:

| Detector | Definition |
|---|---|
| `huber_cusum` | rolling median + MAD, z floored and capped, Huber clipping `c=1.345`, two-sided CUSUM slack `k` |
| `raw_cusum` | same z, **no** clipping, slack searched over `{0.1, 0.25, 0.5}` |
| `vol_ratio` | `std(window/6) / std(window)` |
| `accel_z` | EMA(3) of the conditioned series, its first difference, robustly standardised |

Combinations searched (4,572 formulas):

`HUBER_ONLY`, `CUSUM_ONLY`, `OR_GATE`, `AND_GATE`,
`CASCADE_HUBER_THEN_CUSUM`, `VOL_ACCELERATION_GATE`, `ACCEL_PLUS_HUBER`,
`DUAL_SCALE_CASCADE`, `CROSS_SERIES_MACRO_GATE`.

`CROSS_SERIES_MACRO_GATE` fires when upward UNRATE pressure and downward PAYEMS
pressure confirm each other within ±2 months.

## 5. Search geometry and scoring

| Dimension | Grid |
|---|---|
| Window policy | `fast` 18m/36w, `medium` 24m/44w, `slow` 36m/52w |
| Lookback policy | `short` 4m/6w, `medium` 6m/12w, `long` 12m/24w |
| Huber threshold | 4, 6, 8, 12, 16 |
| CUSUM threshold | 2, 3, 4, 6, 10, 20 |
| CUSUM slack | 0.1, 0.25, 0.5 |
| Volatility threshold | 1.5, 2.0, 2.5 |
| Acceleration threshold | 1.0, 1.5, 2.0, 2.5 |
| Episode cooldown | 6 months / 12 weeks |
| **Formulas** | **4,572** |

```
engineering_score = catch_rate_pct - 15 * false_alarms_per_1000
false_alarms_per_1000 = 1000 * false_alarms / exposure_bars
```

Two guards are applied and disclosed:

1. **Quiet-period definition.** The 12 months (24 weeks) after every anchor are
   excluded: an alarm during a crisis is late confirmation, not crying wolf.
2. **Qualification.** `hits >= 1`, `false_alarms_per_1000 <= 12`, and
   `catch_rate_pct >= 40`. A formula that never fires is not a detector and is
   left unranked.

The 40 % catch floor exists because the raw score still rewards near-silence:
with ~78 years of exposure one alarm costs about a third of a catch. Both the
score and the guard are written into `top_formulas.json`.

## 6. Results

### Global leaderboard (all anchors, `results/reverse_mining_leaderboard.csv`)

| Rank | Formula | Catches | FA | FA/1000 | Score |
|---|---|---|---|---|---|
| 1 | `OR_GATE(window=slow, lead=medium, huber=4, cusum=20, slack=0.5)` | 9/20 (45 %) | 39 | 4.23 | -18.43 |
| 2 | `OR_GATE(window=slow, lead=long, huber=4, cusum=20, slack=0.5)` | 9/20 | 40 | 4.34 | -20.06 |

### Blind holdout (pre-2005 only → post-2005 cold test)

- **Noise-budgeted champion**: `CROSS_SERIES_MACRO_GATE(fast/long, huber=4,
  cusum=4, slack=0.5)` — training 8/12 (66.7 %), cold **2/6 with 5 false alarms**.
- **Recall-first profile**: `CUSUM_ONLY(slow, medium, cusum=2, slack=0.25)` —
  training 12/12, cold **5/6 with 192 false alarms**.

The measured frontier (same family, threshold varied) is the central result:

| CUSUM threshold | Cold catches | Cold alarms |
|---|---|---|
| 2 | 5/6 | 192 |
| 3 | 4/6 | 145 |
| 4 | 4/6 | 111 |
| 6 | 4/6 | 73 |
| 10 | 3/6 | 36 |
| 20 | 2/6 | 6 |

**The two v2.0 targets are mutually exclusive on this data.** 5/6 is reachable
only by accepting ~192 alarms; staying under 40 alarms caps recall at 3/6. No
formula in the 4,572-formula search satisfies both, and this repository does not
claim otherwise.

### v1.0 vs v2.0

| Metric | v1.0 | v2.0 |
|---|---|---|
| Detector scale | raw diffs, saturating MAD bug | log-returns, floored scale, z-capped |
| Formulas searched | 384 | 4,572 |
| Global best catches | 4/20 | 9/20 |
| Blind champion | 4/6 @ 89 alarms | 2/6 @ **5** alarms (noise-first) |
| Blind recall-first | — | 5/6 @ 192 alarms |
| Both targets met | no | **no (proven trade-off)** |

v1.0's 4/6 was partly an artefact of the saturation bug: alarms fired almost
everywhere. v2.0 can reproduce 4/6 with 73 alarms, but not 5/6 under 40.

## 7. Live radar (2025–2026)

`results/blind_holdout_report.md` ends with the last 12 observations of all four
series, showing the exact Huber and CUSUM accumulators, volatility ratio,
acceleration z, and whether the frozen champion fires. As of the committed
snapshot (UNRATE through 2026-10, ICSA through 2026-11-21, Brent through
2026-09-04) **no series is firing**.

## 8. Reproduce locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    POSIX: source .venv/bin/activate
pip install -r requirements.txt

python -m core.reverse_miner            # ~2-3 min, writes leaderboard + JSON
python -m stress_test.blind_validator   # ~2 min, writes blind report
```

## 9. CI

`.github/workflows/run_reverse_mining.yml` runs on push to `main` and on
`workflow_dispatch`: checkout → Python 3.11 → install → miner → blind validator
→ artifact upload → commit changed `results/` back to `main` with `[skip ci]`.

## 10. Limitations

1. **Sample size.** 20 anchors, 6 in the holdout. Six binary outcomes are a
   sanity check, not statistical proof.
2. **Untrained weekly series.** ICSA and Brent have no pre-2005 anchor, so their
   thresholds are inherited from monthly evidence; their cold behaviour is
   genuinely out-of-sample but untuned.
3. **Release lags.** Anchors use official event dates; live data arrives revised.
4. **The score's calibration.** A 0.8-or-15-point penalty per alarm is not
   scale-free across 78 years of monthly data, which is why the champion rule is
   stated explicitly in the report instead of being inferred from the score.
5. **Not a trading system.** No costs, covariates or regime model.

## 11. Status

| Item | State |
|---|---|
| v2.0 code + frequency-aware grammar | implemented |
| Local full run on real FRED data | executed |
| 4,572-formula search | executed |
| Blind cold test + live radar | executed |
| CI pipeline | verified green |
| 5/6 catch **and** ≤40 alarms | **not achieved — measured trade-off documented** |
