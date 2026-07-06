---
name: healer-budget-accumulates-across-cycles
description: El presupuesto del Healer suma verdes de TODOS los ciclos previos (cursor congelado en core<3), por eso amputa OP1 en rojo tras rachas ganadoras
metadata:
  node_type: memory
  type: project
---

Por qué "cuando el bot ha ganado muchas veces la OP1 termina cerrando una OP1 en pérdida" (observación del usuario, confirmada).

`_check_healing` (`bot/strategy.py:374`) hace `return` ANTES de tocar el cursor `last_healed_ticket` cuando `core_count < healer_min_core` (3). Diseñado así para "preservar presupuesto". Efecto colateral: mientras el bot corre en core 0/1/2 (rachas de ciclos OP1 que cierran en verde), el cursor queda **congelado** y el presupuesto (suma de TODOS los deals verdes OUT nuevos: cierres de ciclo, bankings, scalps del grinder) **se acumula sin techo**.

La primera vez que una cesta llega a core 3, el Healer ve ese presupuesto gigante de golpe → `lots_to_close = (budget*0.90)/(diff_points*tick_value)` es enorme → amputa un chunk grande del peor leg (a veces la propia OP1) realizando una **pérdida grande permanente**. En el reporte: amputación única de **-2124.96** el 06-15, y 11 amputaciones de legs SMC (OP1). El piso verde `min_green_profit` (OP12) que protege la OP1 desnuda del trail SOLO aplica en `core_count==1`; el Healer (core>=3) NO tiene ese piso.

Falla de fondo: los verdes ya fueron realizados al balance en ciclos previos; usarlos otra vez como "presupuesto" para realizar pérdidas nuevas es devolver ganancias pasadas — convierte una pérdida flotante que el hedge YA congelaba en una permanente. Alimenta el cascade de [[healer-unwind-hedge-cascade]]. Contexto de diseño del Healer: [[bot-design-constraints]].

**IMPLEMENTADO (C):** el presupuesto del Healer se acota al ciclo. Al iniciar un ciclo (`_close_all` o `core_count==0`) se marca `_healer_needs_rebaseline`; en el primer pase con core>=3, `_check_healing` rebasa `last_healed_ticket` al ticket máximo actual (descartando verdes de ciclos previos) y no ampu­ta ese pase. Así el presupuesto arranca en ~0 cada ciclo y solo suma verdes generados DENTRO del ciclo (bankings del Unwind + scalps del grinder). Se acabó el dump cross-ciclo. Combinar con el freeze cycle-total del Unwind (B) en [[healer-unwind-hedge-cascade]].
