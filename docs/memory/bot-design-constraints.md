---
name: bot-design-constraints
description: Decisiones de diseño y restricciones del usuario para el bot de trading Python
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 7105db91-4d88-44ad-9344-e5482051cadb
---

Restricciones que el usuario fijó para el bot (`bot/`), a respetar en cambios futuros:

- **NO** implementar cierre de emergencia / stop de cesta por drawdown.
- **NO** poner S/L catastrófico en las posiciones.
- **Mantener magic numbers hardcodeados** por ahora (no mover a config/DB).
- **OP2 (Hedge Lock) = CONGELAR la pérdida.** Ningún mecanismo debe cerrar la cobertura salvo: (a) cierres parciales del Healer, o (b) cuando el saldo neto TOTAL de la cesta (ganadoras+perdedoras) sea positivo. Por eso Profit Banking lleva freeze guard (`if basket_net<=0: return`).
- **Healer** debe hacer cierres parciales con su 90% acumulado, sin esperar a que un solo ganador cubra toda la pérdida, y sin descartar ganadores intermedios.
- **Healer solo ampu­ta en cestas profundas: `core_count >= healer_min_core` (default 3, o sea OP3/OP4+).** Con OP1 sola la cobertura correcta es el Hedge Lock (congela en ESTADO 1), NO la amputación; con OP1+Hedge (core==2) el hedge ya congela. El gate sale antes de tocar el cursor para preservar el presupuesto. Implementado en commit `210b099` (`_check_healing`); ver [[op1-red-closes-healer-vs-reset]].

**Why:** el bot venía dejando perdedoras desnudas abiertas indefinidamente (ver [[bot-760-root-cause]]); el usuario quiere que la cobertura se respete y que el healer realmente cure, pero sin stops duros que corten la estrategia martingala.

**How to apply:** cualquier mecanismo que cierre posiciones individuales debe verificar que no desarma una cobertura de un perdedor abierto. Auditorías de fidelidad: comparar contra `original_MQL5.txt` y reportar inconsistencias antes de aplicar; el usuario distingue "port infiel" de "rediseño deliberado".

Usuario opera el bot en MetaTrader 5 real (XAUUSD+, magic 100100), config vive en tabla PostgreSQL `bot_config`. Lee comments de MT5 para identificar tipos de operación.
