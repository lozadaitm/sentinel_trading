---
name: healer-comment-on-close-deal
description: El comment "Healer Amputacion" vive en el deal de CIERRE (OUT), nunca en una posición abierta; por eso no aparece como op abierta
metadata:
  node_type: memory
  type: reference
---

El Healer NO renombra la operación que amputa. `_apply_healing` llama a `close_partial(worst, lots, "Healer Amputacion")` (`bot/strategy.py:454`), y ese string va en `request["comment"]` de una **orden de cierre** (`_close_volume`, `bot/broker.py:110`). Por lo tanto:

- La posición amputada **conserva su comment de apertura** (SMC / Hedge Lock / Recovery / Op 4). Es inmutable.
- `"Healer Amputacion"` aparece SOLO en el **deal de salida** (`Direction=out`) que realiza el cierre parcial/total.

Verificado en `ReportHistory-25556247.xlsx`: los **55** deals con comment "Healer Amputacio" son **todos `Direction=out`**, ninguno `in`. Por eso NUNCA verás una posición ABIERTA con comment Healer — el recuerdo de "ops abiertas con ese comentario" es en realidad haber visto los deals de cierre en el historial. La traza confiable del evento sigue siendo la fila `HEALER | Amputacion Tactica` del log de la DB. Relacionado: [[op1-red-closes-healer-vs-reset]].
