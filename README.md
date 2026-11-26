# CHAOS-Reverse-Miner

![Run Reverse Mining](https://github.com/uniqueteamyemen/CHAOS-Reverse-Miner/actions/workflows/run_reverse_mining.yml/badge.svg)

> **Post-mortem-first detector discovery for economic and structural time series.**
> Start from what actually happened, walk backwards, and keep only the detector
> formulas that would have fired on time without crying wolf.

Isolated research repository — no fork, submodule, or workflow link to
`uniqueteamyemen/CHAOS`, and it never writes there.

**بالعربية:** النسخة 3.0 + دورة C4. أضفنا مراسي أسبوعية موثّقة قبل 2005 فصار
المتخصّص الأسبوعي **بارداً فعلياً** بدل in-sample، وأضفنا خمسة أبعاد مستمرة
(`StateVector`) تُعاير عتباتها من تاريخ الميزة نفسه (كمّيات مئوية) دون أي عتبة
خارجية مُثبّتة. النتيجة المركّبة: **5/6 بـ35 إنذاراً**. حدث `OIL_DECLINE_2014`
يبقى خارج المتناول بنيوياً لأنه مرساة عند القمة لا قبلها، وهذا مُعلن.

---

## 1. What changed in Cycle C4

| Item | v3.0 | C4 |
|---|---|---|
| Weekly training | in-sample (3 shocks) | **5 pre-2005 anchors** (`ICSA` 1973/1980/1990/2001, Brent Gulf War 1990) |
| Weekly cold result | 2/3 (in-sample) | CUSUM grammar 0/3; **C4 relay 2/3 cold** |
| StateVector | 5 detectors | + `sahm_floor_displacement`, `ph_up`/`ph_down`, `drawdown`/`drawdown_norm`, `l1_slope_break`, `icsa_payems_fusion` |
| Thresholds | fixed grids | **empirical quantiles** of each feature's pre-2005 history |
| Brent latch | CUSUM stays latched | Page-Hinkley decays on mean reversion |
| Combined | 5/6 @33 (weekly in-sample) | **5/6 @35, both halves cold** |

## 2. Repository layout

```text
CHAOS-Reverse-Miner/
├── README.md
├── requirements.txt
├── .gitignore
├── .github/workflows/run_reverse_mining.yml
├── core/
│   ├── data_loader.py          # FRED access, profiles, 25 anchors (20 + 5 weekly)
│   ├── detectors.py            # detectors + C4 StateVector features
│   ├── reverse_miner.py        # 4,572-formula policy grid
│   ├── composite_router.py     # monthly + weekly + C4 relay router, live radar
│   └── c4_precursor_miner.py   # windows, calibration, precedence, diagnostics
├── stress_test/
│   └── blind_validator.py      # pre-2005 mining, cold test, radar
├── production/
│   ├── thresholds.json         # pinned thresholds (SHA-256 logged)
│   └── chaos_engine.py         # 3-state deterministic engine + causal replay
└── results/
    ├── reverse_mining_leaderboard.csv
    ├── top_formulas.json
    ├── blind_holdout_report.md
    ├── composite_router.json
    ├── c4_precursor_report.md
    ├── c4_state_vector_summary.json
    ├── c4_pre_event_windows.csv
    ├── paper_trading_protocol.json
    ├── paper_trading_2023_2026.md
    └── live_engine_state.json
```

## 3. Data and anchors

| Series | FRED ID | Transform | Frequency |
|---|---|---|---|
| Unemployment rate | `UNRATE` | first difference | monthly, 945 obs |
| Nonfarm payrolls | `PAYEMS` | log-return | monthly, 1,054 obs |
| Initial claims | `ICSA` | log-return | weekly, 3,125 obs |
| Brent crude | `WCOILBRENTEU` | log-return | weekly, 2,052 obs |

Official endpoint, no key:

```text
https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>
```

**25 anchors**: the 12 specified + 8 PAYEMS peaks + 5 pre-2005 weekly anchors
added by C4. The weekly dates come from the C4 directive and were cross-checked
against the raw prints (ICSA rises into 1973/1980/1990/2001; Brent jumps
20.9 → 26.4 in the week of 1990-08-02). SHA-256 of every raw CSV is recorded in
the outputs.

## 4. Detectors

Continuous, rolling, never frozen: Huber-MAD CUSUM, raw z-CUSUM (slack
0.1/0.25/0.5), volatility ratio, momentum acceleration, OLS trend slope.

### C4 StateVector (no hardcoded alarm levels)

| Feature | Definition |
|---|---|
| `sahm_floor_displacement` | monthly `MA3 - min(MA3, 12m)`; weekly `MA4 / min(MA4, 52w) - 1` |
| `ph_up`, `ph_down` | one-sided Page-Hinkley with EWMA mean `alpha=0.1`, decay on reversion |
| `drawdown` | `(price - rolling_max(W)) / rolling_max(W)` |
| `drawdown_norm` | `drawdown / rolling_std(log_return, W)` |
| `l1_slope_break` | `|slope_recent - slope_prior| / MAD(diff)`, `W` = 24m / 44w |
| `icsa_payems_fusion` | month-end max of weekly ICSA 4-week surge × `-log(PAYEMS_t/PAYEMS_{t-3})`, robust z |

No `0.35`, `-0.22` or `-6.0` is hardcoded. The C4 miner takes each threshold as a
quantile of the feature's own pre-2005 distribution and tests it cold.

## 5. Results

### Global leaderboard (25 anchors)

| Rank | Formula | Catches | FA | Score |
|---|---|---|---|---|
| 1 | `OR_GATE(slow, medium, huber=4, cusum=20, slack=0.5)` | 10/25 (40 %) | 38 | -21.80 |
| 2 | `OR_GATE(slow, long, huber=4, cusum=20, slack=0.5)` | 10/25 | 39 | -23.43 |

### v2.0 blind test (single formula)

Champion cold: **3/6 with 8 alarms**. Recall-first profile: **5/6 with 192
alarms** — the measured frontier still shows the single-formula trade-off.

### v3.0 + C4 composite (all branches cold)

| Branch | Training | Cold catches | Cold FA |
|---|---|---|---|
| Monthly A: resonance gate | frozen v2 winner, pre-2005 | 2/3 | 5 |
| Monthly B: trend relay `k=6, thr=2` | pre-2005 UNRATE | 3/3 | 2 |
| **Monthly routed (A ∪ B)** | | **3/3** | **7** |
| Weekly CUSUM grammar | 5 pre-2005 weekly anchors | 0/3 | 5 |
| **C4 relay** (qualified pre-2005 features, OR-ed per series) | pre-2005 quantiles | **2/3** | **28** |
| **Combined (routed)** | | **5/6** | **35** |

The CUSUM weekly specialist is kept as a **negative result** (0/3 cold) and is
not routed. The relay catches `COVID_ICSA_2020` (ICSA `sahm_floor_displacement`)
and `UKRAINE_OIL_2022` (Brent `ph_up`/`l1_slope_break`/`trend_slope_z`), missing
`OIL_DECLINE_2014`.

### C4 precursor findings

- `icsa_payems_fusion` cold: **3/5** monthly events with **1** alarm — the
  strongest new dimension.
- `drawdown` on Brent: **0/1** on 2014 with 14 alarms; `drawdown_norm`: 0/1 with
  10. No budget-compliant threshold exists for `ph_down` on Brent declines.
- Page-Hinkley unlatches Brent late 2026 (`ph_down` → 0) while the v3 CUSUM
  stays above threshold. That is the intended fix.
- Precedence: `drawdown_norm` precedes `sahm_floor_displacement` in 3/3 shared
  anchors (mean 3.3 steps); `ph_down` precedes `trend_slope_z` in 2/2.

## 6. Why 2014 cannot be caught cold

`OIL_DECLINE_2014` is anchored at the pre-decline **peak**. A decline detector
must fire before the drop or within the 2-step grace after the anchor; the drop
starts at the anchor. Drawdown at the peak is ~0, so it cannot fire; calibration
confirms 0/1 with 14 alarms. This is a property of the anchor, not of the
detector, and it is reported rather than engineered around.

## 7. Live radar (late 2026)

- **Brent: latched on CUSUM, reset on Page-Hinkley.** CUSUM− ≈ 20 (threshold 4)
  while `ph_down` returns to 0 after the July rebound. The relay's oil branch is
  therefore not re-firing on the stale June sell-off.
- **UNRATE, PAYEMS, ICSA: quiet** over the last three observations.

## 8. Production engine (deterministic 3-state)

`production/chaos_engine.py` runs a single causal replay and a live snapshot.
Every threshold is read from `production/thresholds.json` (SHA-256 pinned in the
output); nothing is hardcoded in Python.

| State | Rule |
|---|---|
| `ALARM` | `TREND_SLOPE(UNRATE)` OR `CROSS_SERIES_MACRO_GATE` |
| `WATCH` | `icsa_payems_fusion` OR C4 weekly relay, without ALARM |
| `QUIET` | neither |

The replay (`2023-01-01` → `2026-10`, 45 monthly decisions) rebuilds each feature
from the prefix ending at the decision month: zero lookahead.

| Metric | Result |
|---|---|
| ALARM episodes | **1** (protocol max 3) |
| WATCH episodes | 3 |
| False alarms per year | **0.267** (budget 1.0) |
| Verdict | **PASS** |
| Live state (2026-10) | **QUIET** |

The single ALARM is one continuous 14-month episode starting at the replay
boundary, driven by a latched PAYEMS downward CUSUM (peak 14.5, threshold 4)
rather than a fresh 2023 signal; it is counted once and disclosed in
`results/paper_trading_2023_2026.md`. The pre-committed protocol lives in
`results/paper_trading_protocol.json`; the machine state in
`results/live_engine_state.json`. Two consecutive runs produce byte-identical
JSON (verified by SHA-256).

## 9. Reproduce locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    POSIX: source .venv/bin/activate
pip install -r requirements.txt

python -m core.reverse_miner
python -m core.composite_router
python -m core.c4_precursor_miner
python -m stress_test.blind_validator
python -m production.chaos_engine
```

## 10. CI

`run_reverse_mining.yml` runs on push to `main`, on `workflow_dispatch`, and on
a daily schedule (`0 14 * * *`): checkout → Python 3.11 → install → miner →
composite router → C4 miner → blind validator → production engine → artifact
upload → commit changed `results/` back to `main` with `[skip ci]`.

## 11. Limitations

1. 25 anchors, 6 post-2005; a sanity check, not statistical proof.
2. Weekly training is thin: 5 anchors, 4 of them ICSA. Brent has a single
   pre-2005 anchor (Gulf War), so its cold oil result is weakly trained.
3. The C4 relay OR-combines every qualified feature per series; it was not
   selected on cold performance, but a wider feature set raises the multiple-
   testing surface.
4. Anchors use official event dates; live data arrives revised.
5. No transaction costs, covariates or regime model: this is a detector-mining
   benchmark, not a trading system.

## 12. Status

| Item | State |
|---|---|
| v2.0 engine + 4,572-formula search | implemented, executed |
| v3.0 composite router | implemented, executed |
| C4 StateVector (5 dimensions) | implemented, executed |
| C4 miner (windows, calibration, precedence, diagnostics) | implemented, executed |
| Weekly specialist truly cold | yes (`in_sample: false`) |
| CI pipeline | verified green |
| Combined 5/6 @ <=35 | met (5/6, 35) |
| 2014 cold catch | not achieved - structural, documented |
| Production engine (pinned thresholds, 3 states) | implemented, deterministic |
| 2023-2026 paper-trading protocol | PASS (1 alarm, 0.267 FA/year) |
