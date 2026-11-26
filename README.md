# CHAOS-Reverse-Miner

> **Post-mortem-first detector discovery for economic and structural time series.**
> Start from what actually happened, walk backwards, and keep only the detector
> formulas that would have fired on time without crying wolf.

This is an **isolated** research repository. It has no link, submodule, fork
relationship, or shared workflow with `uniqueteamyemen/CHAOS` and never writes to
it.

**بالعربية:** هذا مستودع مستقل تماماً عن مستودع `CHAOS`. يبدأ المحرك من نقاط
الأزمات التاريخية المعروفة ويمشي **إلى الخلف** ليبحث عن تركيبات كواشف رياضية
كانت ستُنبّه قبل الحدث أو معه، مع معاقبة الإنذارات الكاذبة في فترات الهدوء.
كل الأرقام في `results/` محسوبة من بيانات FRED الرسمية، ولا يوجد أي رقم مُقدَّر
أو مُختلق.

---

## 1. Why "reverse"?

Forward-only gating (fire when a single threshold is crossed) is easy to tune and
easy to fool. This engine inverts the workflow:

1. **Anchors** — a fixed registry of 20 historical crisis / shock dates
   (12 specified anchors + 8 PAYEMS cycle peaks).
2. **Backward lookback** — for each anchor, open a window of `lead_w` steps
   *before* the anchor, plus a 2-step grace period after it.
3. **Grid search** — evaluate hundreds of detector formulas (Huber-MAD CUSUM,
   raw z-CUSUM, OR gates, cascades, volatility gates) over rolling baselines.
4. **Scoring** — reward catching the anchor inside the window, punish every
   distinct alarm wave that fires when nothing happens.
5. **Blind validation** — freeze the winners, then test on shocks the miner
   never saw (strict temporal split at 2005).

The point is not to fit history. The point is to ask: *if we had run this detector
in 1969, 1973, 1980, ... would it have helped, and how often would it have been
wrong?*

## 2. Repository layout

```text
CHAOS-Reverse-Miner/
├── README.md
├── requirements.txt
├── .gitignore
├── .github/
│   └── workflows/
│       └── run_reverse_mining.yml
├── core/
│   ├── __init__.py
│   ├── data_loader.py
│   ├── detectors.py
│   └── reverse_miner.py
├── stress_test/
│   ├── __init__.py
│   └── blind_validator.py
└── results/
    ├── .gitkeep
    ├── reverse_mining_leaderboard.csv
    ├── top_formulas.json
    └── blind_holdout_report.md
```

`data/` is a **local cache only** (git-ignored). The workflow downloads the raw
FRED CSVs on every run, so the repository stays small while the raw-byte
fingerprints in `results/` keep the results auditable.

## 3. Data provenance

| Series | FRED ID | Description | Frequency |
|---|---|---|---|
| Unemployment rate | `UNRATE` | Civilian unemployment rate | monthly |
| Nonfarm payrolls | `PAYEMS` | All employees, total nonfarm | monthly |
| Initial claims | `ICSA` | Initial claims, seasonally adjusted | weekly |
| Brent crude | `WCOILBRENTEU` | Crude oil, Brent, Europe | monthly |

Access method: the **official public FRED CSV endpoint**, no API key:

```text
https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>
```

The task brief abbreviated these endpoints to `https://stlouisfed.org`; the
implementation uses the canonical `fred.stlouisfed.org/graph/fredgraph.csv`
endpoint, which is the same official public source.

Nothing is ever synthesised. If FRED is unreachable and no local cache exists,
`data_loader` raises and the job fails instead of inventing a series.

Every run records the SHA-256 of each raw CSV (plus row count and date span) in
`results/top_formulas.json` and in the blind report, so any published number can
be traced back to the exact bytes that produced it:

```
source -> raw bytes -> parsing -> differencing -> detectors -> grid -> score -> results/
```

## 4. Detectors (`core/detectors.py`)

All detectors run on the **first difference** of the level series. That
stationarises series such as `PAYEMS` (a headcount that only grows) and prevents
the accumulator saturation that static baselines suffer from.

| # | Detector | Definition |
|---|---|---|
| 1 | `diffs` | `np.diff(x)`, aligned to the later observation |
| 2 | `huber_cusum` | rolling median + rolling MAD (`window` = 40 / 60); `z = (x - med) / (1.4826 * MAD)`; Huber clipping `c = 1.345`; two-sided CUSUM with slack `k = 0.5` |
| 3 | `raw_cusum` | same standardisation, **no** clipping — faster, noisier change detector |
| 4 | `vol_ratio` | `std(6) / std(40)` — silent pre-crisis compression or expansion |

Combinations scored by the miner:

| Combination | Trigger rule |
|---|---|
| `HUBER_ONLY` | `huber_cusum >= huber_thr` |
| `CUSUM_ONLY` | `raw_cusum >= cusum_thr` |
| `OR_GATE` | either of the two |
| `CASCADE_HUBER_THEN_CUSUM` | Huber fires, CUSUM must confirm within 4 steps |
| `VOL_ACCELERATION_GATE` | `vol_ratio >= vol_thr` **and** Huber confirms |

Directions are respected: events with sign `+1` read the positive accumulator,
sign `-1` (PAYEMS peaks, the 2014 oil decline) read the negative one.

## 5. Anchor registry

The 12 specified anchors, plus PAYEMS cycle peaks (`-1`) on the same NBER peak
dates — 20 anchors in total:

```
NBER_1969_PEAK   NBER_1973_PEAK   NBER_1980_PEAK   NBER_1981_PEAK
NBER_1990_PEAK   NBER_2001_PEAK   NBER_2007_PEAK   GFC_LEHMAN_2008
NBER_2020_PEAK   COVID_ICSA_2020  OIL_DECLINE_2014 UKRAINE_OIL_2022
PAYEMS_PEAK_1969 1973 1980 1981 1990 2001 2007 2020
```

## 6. Search space

| Dimension | Grid |
|---|---|
| Rolling window | `40`, `60` |
| Lookback `lead_w` | `3`, `6`, `12` steps |
| Huber threshold | `4, 6, 8, 12, 16` |
| CUSUM threshold | `6, 10, 15, 20` |
| Volatility threshold | `1.5, 2.0, 2.5` |
| Cooldown (episode dedup) | `6` steps |
| Grace after anchor | `2` steps |
| **Formulas evaluated** | **384** |

## 7. Scoring — and an honest caveat

The task specification defines:

```
Pragmatic Score = hit_rate_pct - 0.8 * false_alarms
```

where `hit_rate_pct = 100 * hits / events` and `false_alarms` is the number of
distinct alarm waves (6-step cooldown) outside every event window. That formula
is implemented **exactly**, and `score` in the leaderboard is that number.

Two guards are applied, and both are disclosed rather than hidden:

1. **Eligibility.** A formula that never fires scores `0` and would beat every
   active detector on a 78-year timeline. Such formulas are marked
   `eligible = false` and left unranked. Only formulas with at least one hit are
   ranked.
2. **Diagnostic column.** `alarm_rate_per_1000` normalises the false-alarm count
   by exposure, and `score_normalized = hit_rate_pct - 0.8 * alarm_rate_per_1000`
   is reported for comparison. It is **not** used for ranking.

Why the caveat matters, with numbers from this repository: over the full
timeline the best active formula scores **-11.2** (4/20 hits, 39 false alarms),
while a formula that never fires scores **0**. The specified score charges the
same 0.8 per alarm whether the quiet stretch is 78 years or 5, so it is not
comparable across different time splits — see the blind report, where the
pre-2005 training winner scores **+6.47** on its own (shorter) window. The
leaderboard faithfully ranks by the specified objective; the normalised column
exists so the trade-off can be judged on a per-opportunity basis.

## 8. Results snapshot

Produced by the runs recorded in `results/` (refresh on every push):

**Global leaderboard** (`results/reverse_mining_leaderboard.csv`, 384 rows):

| Rank | Formula | Hits | Hit rate | False alarms | Score |
|---|---|---|---|---|---|
| 1 | `CASCADE_HUBER_THEN_CUSUM(window=40, lead=12, huber=4, cusum=10)` | 4/20 | 20 % | 39 | -11.2 |
| 2 | `CASCADE_HUBER_THEN_CUSUM(window=60, lead=3, huber=8, cusum=20)` | 1/20 | 5 % | 21 | -11.8 |
| 3 | `CASCADE_HUBER_THEN_CUSUM(window=60, lead=6, huber=8, cusum=20)` | 1/20 | 5 % | 21 | -11.8 |

**Blind holdout** (`results/blind_holdout_report.md`): mined on pre-2005 anchors
only, frozen, then tested on six post-2005 shocks — **4/6 hits** (GFC 2008,
COVID peak, COVID claims, Ukraine oil), 89 false alarms in the post-2005 era,
score -4.53. `2007` and the 2014 oil decline were missed. Both the misses and the
negative score are reported unchanged.

## 9. Reproduce locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    POSIX: source .venv/bin/activate
pip install -r requirements.txt

python -m core.reverse_miner        # downloads data/, writes leaderboard + JSON
python -m stress_test.blind_validator  # writes results/blind_holdout_report.md
```

Optional flags: `--force-refresh` (re-download FRED), `--results-dir`,
`--top-n`.

## 10. Continuous integration

`.github/workflows/run_reverse_mining.yml` runs on every push to `main` and on
`workflow_dispatch`:

1. checkout
2. Python 3.11 + `pip install -r requirements.txt`
3. `python -m core.reverse_miner`
4. `python -m stress_test.blind_validator`
5. upload `results/` as an artifact
6. commit changed files in `results/` back to `main` (`[skip ci]`)

The commit-back step makes the workflow idempotent: if the numbers are unchanged
it commits nothing.

## 11. Limitations

- **Small sample.** 20 anchors, 6 of them in the blind holdout. This is a
  benchmark, not statistical proof.
- **No release lags.** Anchors use official event dates; a live system would only
  see revised or delayed releases.
- **Coarse grid.** Thresholds are discrete; the leaderboard is a discrete
  optimum, not a global one.
- **Retrospective.** The engine describes what *would have* fired, not what a
  trading or policy system should do.
- **Shared shocks.** GFC and COVID are entangled in several series; a hit there
  is not evidence of an independent precursor mechanism.

## 12. Status

| Item | State |
|---|---|
| Structured repository + code | implemented |
| Local end-to-end run on real FRED data | executed, outputs committed |
| GitHub Actions workflow | defined; first run verification in progress |
| Blind holdout | executed, 4/6 hits, reported as-is |
