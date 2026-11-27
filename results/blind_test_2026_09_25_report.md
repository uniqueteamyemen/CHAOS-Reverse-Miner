# Blind Structural-Break Test - 2026-09-25

All columns are anonymous. The detector read only
`data/blind_matrix_2026_09_25.csv`; no external news, prices or events after the
cutoff were consulted, and no asset identity appears in this report.

## 1. State at the 2026-09-25 close

| Column | State | ph_up | ph_down | drawdown_norm | trend_slope_z | l1_slope_break |
|---|---|---|---|---|---|---|
| S_01 | NORMAL | 90.1168 | 30.4019 | -0.972 | 0.018 | 0.001 |
| S_02 | PRE_BREAK_TENSION | 0.1168 | 0.0031 | 0.0 | 1.106 | 0.001 |
| S_03 | PRE_BREAK_TENSION | 0.1511 | 0.0 | -14.476 | -0.062 | 0.001 |
| S_04 | PRE_BREAK_TENSION | 0.0917 | 0.0 | -18.714 | -0.672 | 0.001 |
| S_05 | NORMAL | 0.5379 | 1.3086 | -10.43 | -0.133 | 0.001 |
| S_06 | NORMAL | 0.0056 | 0.0036 | -17.258 | -0.004 | 0.003 |
| S_07 | NORMAL | 2807.1622 | 1570.7542 | -8.578 | 0.099 | 0.004 |
| S_08 | PRE_BREAK_TENSION | 0.0323 | 0.0 | -22.428 | -1.671 | 0.002 |

Blind Co-Fracture Index: **0** columns in an active
break, **4** in pre-break tension.

## 2. Q3 2026 timeline (2026-07-01 to 2026-09-25)

| Column | State | Start | End | Days |
|---|---|---|---|---|
| S_01 | PRE_BREAK_TENSION | 2026-08-04 | 2026-08-04 | 1 |
| S_02 | PRE_BREAK_TENSION | 2026-09-24 | 2026-09-25 | 2 |
| S_03 | PRE_BREAK_TENSION | 2026-07-01 | 2026-07-10 | 7 |
| S_03 | PRE_BREAK_TENSION | 2026-07-22 | 2026-07-28 | 5 |
| S_03 | PRE_BREAK_TENSION | 2026-08-27 | 2026-09-25 | 18 |
| S_04 | PRE_BREAK_TENSION | 2026-09-25 | 2026-09-25 | 1 |
| S_07 | PRE_BREAK_TENSION | 2026-07-01 | 2026-07-15 | 11 |
| S_07 | PRE_BREAK_TENSION | 2026-07-22 | 2026-07-28 | 5 |
| S_07 | PRE_BREAK_TENSION | 2026-08-12 | 2026-08-12 | 1 |
| S_08 | PRE_BREAK_TENSION | 2026-07-22 | 2026-09-25 | 48 |

## 3. Structural reading for October-December 2026

Regime at the cutoff: **localised stress**
(co-fracture 0, 20-day mean
0.0, 20-day change
0.0).

| Column | State | Direction | Break days (20d) | Tension days (20d) |
|---|---|---|---|---|
| S_08 | PRE_BREAK_TENSION | downside break risk | 0 | 20 |
| S_03 | PRE_BREAK_TENSION | upside break risk | 0 | 17 |
| S_02 | PRE_BREAK_TENSION | upside break risk | 0 | 2 |
| S_04 | PRE_BREAK_TENSION | upside break risk | 0 | 1 |

| Month | Structural regime | Candidate columns |
|---|---|---|
| October 2026 | localised stress | S_08, S_03, S_02 |
| November 2026 | localised stress | S_08, S_03, S_02 |
| December 2026 | localised stress | S_08, S_03 |

**Caveat:** structural scenario conditional on pre-cutoff data; not a forecast or a probability. The months are conditional readings of the
cutoff stress state and its recent slope, not predictions with probabilities.

## 4. Method

- Page-Hinkley accumulators on daily changes (upward and downward).
- 60-day OLS slope z-score on the level.
- 252-day drawdown normalised by rolling volatility.
- L1 slope-break magnitude.
- Every threshold is a **causal** rolling 3-year quantile shifted one day
  (Q98/Q95 upward, Q02 downward), starting from 2004.
- State = UPWARD_BREAK when the upward pair fires, DOWNWARD_BREAK when the
  downward pair fires, PRE_BREAK_TENSION when only part of a pair fires.
