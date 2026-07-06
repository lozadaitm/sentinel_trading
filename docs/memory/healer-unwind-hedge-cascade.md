---
name: healer-unwind-hedge-cascade
description: Causa raíz de los blowups por stop-out — Healer + Unwind desarman el hedge justo al formarse OP3, dejando la cesta desnuda contra la tendencia
metadata:
  node_type: memory
  type: project
---

Análisis del reporte `ReportHistory-25556247.xlsx` (2026-06-11 → 07-06) + `experimento_1_logs.csv`. La versión actual del bot sufrió **2 blowups por STOP-OUT del bróker** (deals con comment `[so ...]`): **06-17** (balance 10,832 → 48) y **06-30** (8,697 → 569). 10 deals de stop-out, **-20,119 realizados**. Aparte, el Healer realizó **-10,322** en 55 amputaciones. Causa raíz encadenada:

**1. El hedge es SIEMPRE el leg extremo y es atacado por sus dos puntas.** El Hedge Lock solo existe cuando OP1 está roja → el hedge es la verde → es el objetivo prioritario de Unwind (banca la mejor verde). Si en cambio es la más roja, es el objetivo del Healer (amputa la peor). Como es contra-direccional, casi siempre es un extremo.

**2. El Healer rompe el freeze del Unwind.** En `on_tick` corre `_check_healing()` (línea 767) ANTES de `_profit_banking()` (768). El Healer amputa la peor roja → eso saca flotante negativo de la cesta → el neto sube > 0 → se abre el gate `if basket_net<=0: return` del Unwind → Unwind banca la verde (el hedge). Resultado en UN tick: se realiza la peor roja Y se cierra el hedge verde. Evidenciado el mismo segundo en que abre OP3 (ver punto 3).

**3. Disparo al formarse OP3 (core llega a 3).** El Healer solo corre con `core>=healer_min_core` (3). Justo cuando ESTADO 2 abre el Recovery (OP3), core pasa a 3 y el Healer activa por primera vez sobre esa cesta con un **presupuesto enorme acumulado** (ver [[healer-budget-accumulates-across-cycles]]). En 10 casos del reporte, un Hedge Lock se cerró 1-2s después de abrir un Recovery. Detalle 2026-06-26 08:39:55-56: abre Recovery Sell 0.40 → tick siguiente Healer amputa roja -563 + Unwind banca hedge verde +197 → OP1+OP3 quedan **desnudas**. 22 ventanas con OP1+OP3 abiertas SIN hedge (~20h totales; una de 14.9h el 06-18).

**4. Desenlace: martingala desnuda contra la tendencia → stop-out.** Sin hedge, la cesta direccional (OP1+OP3+rescates Op4, todas al mismo lado) corre contra la tendencia. El Recovery/Rescue sigue promediando (martingala) hasta que el margen llega a stop-out y el bróker liquida todo. El hedge, que era la única cobertura verde, ya había sido bancado.

**Tensión con el diseño ([[bot-design-constraints]]):** la restricción permite cerrar el hedge "cuando el saldo neto TOTAL sea positivo" (Unwind) y vía Healer. Justo esa dupla es el hueco: el Healer fabrica el "neto positivo" realizando rojo, y el Unwind lo usa para desarmar la cobertura. El freeze `basket_net<=0` no protege porque en tendencia el hedge verde crece y/o el Healer levanta el neto artificialmente.

**Hallazgo secundario:** `healer_balance_bias` está en DEFAULTS (`strategy.py:26`) pero **nunca se usa** en el código — parámetro muerto.

**IMPLEMENTADO (B + C):** El freeze del Unwind ahora evalúa el **P&L total del ciclo** = `self.cycle_realized + _basket_net_profit()` (antes solo el flotante). Como una amputación del Healer mueve dinero de flotante a realizado sin cambiar la suma, es **neutra para el freeze**: ya no puede levantar el neto para abrir el gate ni ese tick ni los siguientes → cascade rota de raíz. `cycle_realized` se acumula incrementalmente en Healer/Unwind/Grinder-timestop y se resetea al cerrar el ciclo (`_close_all` o `core_count==0`). Ver [[healer-budget-accumulates-across-cycles]] para C (presupuesto por ciclo). Nota: el Healer ahora también considera **swap** (peor leg y dimensionado por `profit+swap`, no solo `profit`).

**Vías NO implementadas (quedaron fuera):** (a) que Unwind nunca banque el leg de cobertura mientras exista un perdedor core desnudo del lado opuesto; (d) piso/floor al Healer para no amputar OP1/hedge en rojo grande. Chocan con "no stops duros" — pendientes de decisión.
