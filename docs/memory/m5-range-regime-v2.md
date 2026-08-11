---
name: m5-range-regime-v2
description: la mejora de lateralizacion vive en el M5 (no en el M15); regimen ADX con histeresis + rango fractal M15 + vetos
metadata:
  type: project
---

Decisión del usuario (11-ago-2026): el comportamiento para mercado lateralizado
NO se añade al M15 — el Sentinel queda congelado tal cual. Toda la mejora se
implementó en el M5 (`bot/strategy_m5.py`), que ya era el especialista de rango
(Scalper por ADX bajo) y solo necesitaba inteligencia estructural.

**Why:** duplicar reversión-en-rango dentro del M15 habría creado exposición
correlacionada con el Scalper M5 en la misma cuenta, y fusionar los motores
revertiría la separación por magic que ya costó un bug (ver
[[healer-budget-accumulates-across-cycles]]). Los contratos de riesgo son
opuestos por diseño: M15 congela pérdidas con hedge ([[bot-design-constraints]]),
M5 realiza rápido con SL y TimeStop.

**How to apply:** las 4 piezas de la v2, todas gateadas por config (bot_config;
fallback en DEFAULTS de strategy_m5.py):

- `m5_trend_adx_exit` (24): histéresis del régimen. Surfer entra con ADX >=
  `m5_trend_adx` (30) y solo vuelve a Scalper bajo 24. Igualar ambos = clásico.
- `m5_use_range_struct` + `m5_range_edge_frac` (0.35): el fade solo cerca del
  extremo del rango M15 (`indicators.fractal_range`, aditivo — el M15 no lo
  usa). Sin fractales confirmados degrada al filtro clásico de banda.
- `m5_use_breakout_veto`: no revertir contra ruptura M15 confirmada
  (`check_m15_breakout`, vela cerrada; persiste mientras el cierre siga más
  allá del fractal roto).
- `m5_surfer_yield`: el Surfer cede si el M15 lleva exposición neta del mismo
  lado (lee MT5 por magic vía `gov.peer_magics`, sin DB — mismo canal que
  [[multi-bot-margin-governance]]).

Regresión: sección [15] de `python -m tests.test_m5_and_budget`.
