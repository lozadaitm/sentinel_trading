---
name: op1-red-closes-healer-vs-reset
description: Cómo distinguir cierres rojos de OP1 por Healer (diseño) vs reset manual de balance en el reporte MT5
metadata: 
  node_type: memory
  type: reference
  originSessionId: eec5bdfb-5990-4070-aa15-cc3324e15a11
---

OP1 (entrada desnuda "SMC Buy/Sell Entry") puede aparecer cerrada en ROJO en el ReportHistory MT5 por DOS causas que no son bug. Para diagnosticar, cruzar el reporte con la tabla de logs de la DB (tag en col 3, balance en col 7, timestamp +03 = mismo huso que el reporte).

**1. Healer (Amputación Táctica) — por diseño.** El Healer realiza la pérdida de la peor posición, financiada 90% con verdes recién bancadas (`_apply_healing`, cierra UNA peor pos por tick). En el log: fila `HEALER | Amputacion Tactica. Lotes: X | f1=<profit realizado>` con timestamp y volumen que matchean exacto el cierre del reporte. Es lo esperado (ver [[bot-design-constraints]]: healer cura con 90%).

**2. Reset manual de balance — NO es el bot.** Señales en el log: cierre del reporte SIN ningún evento EXITO/HEALER a esa hora + acto seguido `SYSTEM | INICIADO` con balance en número redondo (ej. 5000.00). La función "reset balance" del demo (Vantage) cierra TODAS las posiciones abiertas y fija el balance; las legs abiertas (OP1+Hedge+Recovery) se flatten en rojo como colateral. La estrategia nunca cierra una OP1 desnuda en rojo (OP12 desarma + freeze del hedge intactos).

Caso 2026-06-19: 4 OP1 rojas. 3 fueron Healer (match exacto en log: 05:40, 05:52:51 f1=-0.60, 06:02:11 f1=-17.52). 1 (la grande, -82.72 @01:13:54, junto a hedge -109.52 y recovery -78.98 = -271 net) fue el reset manual (restart 01:15:22 con balance→5000.00, sin evento de cierre). Recomendación: cerrar el book con el bot ANTES de resetear, o resetear sin posiciones abiertas, para no ensuciar el reporte con rojas "fantasma".
