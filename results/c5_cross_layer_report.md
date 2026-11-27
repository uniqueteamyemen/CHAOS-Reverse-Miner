# Cycle C5 - Cross-Layer Reverse Engineering

Eight layers, one causal rule. Thresholds are pre-2005 quantiles of each
feature's own history; no external level is hardcoded.

## 1. Backward reverse-mining map (pre-2005 anchors only)

Training anchors: `LTCM_RUSSIA_1998, DOTCOM_CRASH_2000`.

| Layer | Series | Feature | Threshold | Quantile | Anchors fired | Mean offset (weeks) |
|---|---|---|---|---|---|---|
| OIL | WCOILBRENTEU | drawdown_norm | 9.529612 | q0.95 | 1 | -7.0 |
| CLAIMS | ICSA | trend_slope_z | 1.951578 | q0.95 | 2 | -5.5 |
| LIQUIDITY | T10Y2Y | trend_slope_z | 2.758293 | q0.95 | 1 | -4.0 |
| VOLATILITY | VIXCLS | ph_up | 0.488298 | q0.995 | 1 | 0.0 |
| FIN_CONDITIONS | NFCI | drawdown_norm | 0.0 | q0.95 | 2 | 1.0 |
| CREDIT | BAA10Y | l1_slope_break | 0.101577 | q0.95 | 2 | 1.5 |
| LABOR_UNRATE | UNRATE | ph_up | 0.748436 | q0.995 | 0 | None |
| LABOR_PAYEMS | PAYEMS | ph_down | 0.012506 | q0.995 | 0 | None |

Chosen cascade: **K = 6 of 8** layers inside a
**4-week** causal window. Training coverage
2/2; pre-2005 quiet
alarm rate 0.401 per year.

### Layer-break order

The table above is sorted by mean offset; negative means the layer fires before
the anchor. This is the backward-derived signature: which layer breaks first,
and by how many weeks.

## 2. Forward causal cold replay (2005-2026, zero lookahead)

Signature frozen before this replay: K=6, window=4w.

| Anchor | Date | Result | Alarm date | Weeks before | Peak layers (4w) | Layers fired |
|---|---|---|---|---|---|---|
| EURO_DEBT_US_DOWNGRADE_2011 | 2011-08-05 | miss | None | None | 5/8 | VOLATILITY, FIN_CONDITIONS, CLAIMS |
| HY_ENERGY_CRUNCH_2015 | 2015-12-11 | hit | 2015-10-02 | -10 | 6/8 | FIN_CONDITIONS, OIL |
| SVB_BANKING_STRESS_2023 | 2023-03-10 | miss | None | None | 0/8 | - |

Cold catches: **1/3**. False alarms:
**22** over 21.69 years
(1.014 per year).

### 2014-2015 credit test

The HY-energy crunch (2015-12-11) is the case where Brent alone failed. Whether
the credit layer carried it is visible in the `layers_fired` column above.

### Verdict, stated without tuning

- **EURO_DEBT_US_DOWNGRADE_2011**: missed. Peak simultaneous layers in the 4-week window was 5/8 (needed 6); layers active: VOLATILITY, FIN_CONDITIONS, CLAIMS.

- **HY_ENERGY_CRUNCH_2015**: caught on 2015-10-02 (-10 weeks before the anchor). Layers active in the window: FIN_CONDITIONS, OIL.

- **SVB_BANKING_STRESS_2023**: missed. Peak simultaneous layers in the 4-week window was 0/8 (needed 6); layers active: none.

- The **credit layer did not fire** in the 2015 window; the catch came from financial conditions and oil. The credit layer therefore did not add information for this episode.

- False alarms: 22 episodes in 21.69 years (1.014/year). This is comparable to the v1.0 budget but was not pre-registered for C5.

No threshold was changed after seeing these results: the cold replay is a
holdout, not a tuning set.

### Live state, late 2026

**QUIET** as of 2026-09-11; layers firing in the last
4 weeks: none.

## 3. Data caveat

FRED's official endpoint returns `BAMLH0A0HYM2` only from 2023-11-28 (ICE
licence restriction), so the historical credit layer uses `BAA10Y` (Moody's Baa
minus 10Y, from 1986) as a documented proxy. HY OAS remains available for the
live cross-check only. This is a property of the source, not an estimate.
