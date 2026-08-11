---
name: newsguard-phase2-nri
description: Fase 2 del NewsGuard (NRI 0-1) — diseño completo acordado, pendiente; solo se construye si la observación de Fase 1 la justifica
metadata:
  type: project
---

Diseño acordado (11-ago-2026) de la Fase 2 del filtro de noticias: el **NRI
(News Risk Index)**, escala continua 0.0→1.0 que complementa el bloqueo duro
de [[newsguard-phase1]]. NO implementada. **Precondición para abordarla:**
revisar los logs `NOTICIAS` de la Fase 1 tras varias rojas (NFP/CPI/FOMC) y
confirmar que las bandas intermedias aportan — si casi todo el daño venía de
las rojas, la Fase 2 puede no hacer falta (decisión explícita del usuario:
mejor descubrirlo con datos que construirla por adelantado).

**Cálculo por evento** (fuente: mismo JSON del CalendarExporter, que ya
exporta LOW/MODERATE/HIGH de todas las divisas):
- severidad = impacto × peso_divisa × booster, capado a 1.0
- impacto MT5: HIGH 1.0 / MODERATE 0.5 / LOW 0.2
- peso divisa (el oro es un trade de USD): USD 1.0, EUR 0.35, CNY 0.2, resto 0.1
- booster ×1.3 por keyword "gold killer": NFP, CPI, FOMC, PCE, discurso del
  chair de la Fed (el JSON trae `name`)

**Perfil temporal:** rampa lineal desde T−120min, meseta plena T−30→T+45
(el post-noticia suele ser MÁS caótico que el pre), decaimiento a 0 en T+90.
NRI del instante = máximo de todas las curvas activas (los valles entre
noticias seguidas quedan puenteados por el máximo). Piso diario: rojas USD
hoy/mañana imponen un piso suave 0.15-0.25 que solo modula tamaño, nunca
bloquea (bloquear todo el día por una noticia de las 14:00 = bot ocioso).

**Bandas de consumo:**
- < 0.30: normal.
- 0.30-0.59: se opera con lote reducido ×(1 − 0.6·NRI) (un 0.45 ⇒ ~73% del lote).
- 0.60-0.99: bloqueo SUAVE de aperturas nuevas, salvo override estratégico.
  No toca recovery/rescate (defensa de ciclo vivo) ni la gestión.
- El bloqueo duro de Fase 1 sigue siendo un flag aparte (`hard`), innegociable
  — nunca mezclar "0.98 negociable" con "1.0 innegociable".

**Override estratégico (la parte que exige definición objetiva —** toda
entrada ya exige sus gates en verde, así que "bases sólidas" = confluencia
EXTRA, no el setup mínimo):
- M15: H4 alineada con la ruptura (no neutral) + pullback en el tercio más
  cercano a la EMA + VCB y spread limpios.
- M5: solo el Scalper con la estructura de rango v2 en verde
  ([[m5-range-regime-v2]]: fade en extremo fractal, sin ruptura en contra).
  El Surfer NO tiene override (perseguir tendencia pre-noticia es justo lo
  que el NRI evita).

**Calibración:** los umbrales (0.30/0.60, pesos, booster) se ajustan con los
logs `NOTICIAS` de Fase 1 cruzados con el resultado de los ciclos/scalps
abiertos en ventana. Loguear el valor del NRI en cada apertura desde el día
uno de la Fase 2. El arnés de backtest NO sirve para validar esto (bar-close,
sin spikes intra-vela); si hiciera falta histórico, MQL5 tiene
CalendarValueHistory retrospectivo.
