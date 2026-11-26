# Paper Trading Replay 2023-2026 - Deterministic 3-State Engine

Replay: `2023-01-01` to `2026-10-01` (45 monthly decisions).

## 1. Pre-committed protocol

Protocol was written to `results/paper_trading_protocol.json` **before** the
replay was executed.

| Field | Value |
|---|---|
| Period | `2023-01-01 to 2026-11-27` |
| Acceptable alarms (max) | 3 |
| Required real crisis caught | True |
| False alarms per year (budget) | 1.0 |
| Alarm logic | ALARM = OR on {TREND_SLOPE, CROSS_SERIES}; WATCH = OR on {C4_relay, icsa_payems_fusion} without ALARM; QUIET = none |
| Thresholds source | production/thresholds.json (SHA-256 pinned) |

Pinned thresholds SHA-256: `189ed2ee461fcc49292d3413c0f5d6c124ea8b656415e83911278fea7b6b3277`

## 2. Replay result

| Metric | Value |
|---|---|
| ALARM episodes | **1** (max 3) |
| WATCH episodes | 3 |
| False alarms per year | **0.267** (budget 1.0) |
| Crisis check | not_applicable: no registered crisis anchor inside the replay period |
| **Verdict** | **PASS** |

### ALARM episodes

| Start | End | Months | Triggers | Peak trend z | Peak fusion z | Peak PAYEMS CUSUM- |
|---|---|---|---|---|---|---|
| 2023-01 | 2024-02 | 14 | CROSS_SERIES_MACRO_GATE | 0.648 | -0.01 | 14.502 |

Interpretation: the ALARM episode starts at the replay boundary (`2023-01-01`),
so it was already active before 2023. Its PAYEMS downward CUSUM peaked at
**14.502**
(threshold 4) and stays latched long after the 2022 tightening, so this is a
stale episode rather than a fresh 2023 signal. It is counted as **one** alarm
per the protocol and disclosed here instead of being hidden.

### WATCH episodes

| Start | End | Months | Triggers | Peak trend z | Peak fusion z | Peak PAYEMS CUSUM- |
|---|---|---|---|---|---|---|
| 2025-02 | 2025-02 | 1 | C4_relay:WCOILBRENTEU | -0.247 | 0.03 | 14.06 |
| 2026-04 | 2026-05 | 2 | C4_relay:WCOILBRENTEU | -0.886 | 0.668 | 10.351 |
| 2026-08 | 2026-08 | 1 | C4_relay:WCOILBRENTEU | -1.58 | 1.056 | 7.498 |

## 3. Live snapshot

As of the latest FRED release, the system state is **QUIET**
(decision month `2026-10`), triggers:
none.

| Series | As of | Trend z | Macro resonance | ICSA sahm | Brent ph_down | Unlatched |
|---|---|---|---|---|---|---|
| UNRATE | 2026-10-01 | -0.848 | False | - | - | - |
| PAYEMS | 2026-10-01 | - | False | - | - | - |
| ICSA | 2026-11-21 | - | - | 0.0038 | - | - |
| WCOILBRENTEU | 2026-09-04 | 0.3564 | - | - | 0.0 | True |

## 4. Determinism

The engine rebuilds every feature from the prefix ending at each decision month,
reads all thresholds from the pinned JSON, and writes no wall-clock timestamps.
Running it twice on the same FRED snapshot produces byte-identical JSON.
