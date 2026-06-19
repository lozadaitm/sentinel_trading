---
name: bot-760-root-cause
description: Causa raíz del -$760 flotante del bot Python (Profit Banking desarma el hedge) y los fixes aplicados
metadata: 
  node_type: memory
  type: project
  originSessionId: 7105db91-4d88-44ad-9344-e5482051cadb
---

Bot Python (`bot/strategy.py`) = port 1:1 del EA MQL5 `M15 Gold Sentinel HyperGrinder v20.0` (`original_MQL5.txt`). Máquina de estados por nº de posiciones core (0/1/2/3+) + subsistemas transversales (Healer, Profit Banking, Grinder trailing).

**Síntoma observado (jun 2026):** muchos cierres chicos en verde (Op1, Hedge Locks, Op3) mientras Op1+Op2 quedaban abiertas con -$760 flotante >36h.

**Causa raíz:** `_profit_banking` (Unwind) cierra posiciones **ganadoras** individuales (`if profit<=0: continue`) y nunca perdedoras. Cuando el Hedge Lock (cobertura) entraba en verde, lo bancaba → desarmaba la cobertura → el perdedor quedaba desnudo y volvía a sangrar → re-hedge → loop. Las 3 salidas de cesta (Basket Trail, Rescue, Viernes) exigen net>=0, así que una cesta en rojo **nunca** se cerraba. Esto existía idéntico en el MQL5 (no fue bug del port).

**Auditoría de fidelidad:** los 9 redflags A–I resultaron **ports fieles** (existen igual en MQL5). El único bug real del port era el healer usando `datetime.now()` (local) en vez de tiempo de servidor.

**Fixes aplicados en esta corrida** (ver [[bot-design-constraints]]): freeze guard en Profit Banking (no toca nada si `basket_net<=0`); healer acumula TODOS los ganadores nuevos + tiempo servidor + parcial con 90%; trailing de cesta armado (RF-B); `core_count` excluye grinder del estado (RF-I); rescate `>=3` + L3 "abre ahora"; grinder anti-churn/vol-gate; comisión round-trip; `_is_grinder` por comment. Pendiente: paridad de indicadores (aproximaciones pandas vs nativos MT5).
