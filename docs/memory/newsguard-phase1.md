---
name: newsguard-phase1
description: NewsGuard Fase 1 — bloqueo duro por rojas USD vía exportador MQL5; arranca en modo observación (news_enforce=false)
metadata:
  type: project
---

Fase 1 del filtro de noticias (11-ago-2026): bloqueo duro alrededor de noticias
de importancia HIGH de USD (lo que mueve al oro), SIN escala continua todavía.
La escala 0-1 (NRI: reducción de lote, bloqueo suave con override) es Fase 2 y
solo se construirá si los datos de la observación la justifican.

**Why:** el paquete MetaTrader5 de Python NO expone el calendario económico
(es API exclusiva de MQL5). El dato entra por archivo:
`scripts/CalendarExporter.mq5` corre como EA en cualquier gráfico del terminal
y vuelca el calendario a `%APPDATA%\MetaQuotes\Terminal\Common\Files\
news_calendar.json` (carpeta COMÚN: un exportador sirve a todas las instancias;
override con env `NEWS_CALENDAR_PATH`). Timestamps en TimeTradeServer(), la
misma base que `tick.time` — sin zonas horarias.

**How to apply:**
- `bot/news.py` (NewsGuard): ventana [T−pre, T+post] por evento; clusters
  cubiertos por unión de ventanas solapadas (bloqueo hasta T+post del ÚLTIMO).
  Fail-safe PERMISIVO CON ALERTA deduplicada (archivo roto ≠ bot mudo).
- Ventanas asimétricas por bot (el criterio es "¿mi posición seguirá viva en
  T?"): M15 pre=2h (una OP1 llega al dato sin colchón), M5 pre=60min (TimeStop
  45'). Post=1h ambos. El M5 además CIERRA su scalp vivo desde T−30min
  (`news_flatten`) porque su SL es saltable por gap; el M15 nunca cierra por
  noticias (su hedge aguanta por diseño).
- Solo frena aperturas aditivas (patrón spread/VCB): gestión nunca se congela.
  Excepción defensiva: el rescate L3-CRÍTICO del M15 abre incluso en ventana.
- **`news_enforce=false` por default = MODO OBSERVACIÓN**: loguea (log_type
  `NOTICIAS`) las aperturas dentro de ventana con tag [observacion] sin
  bloquear nada. Tras 2-4 rojas observadas, activar con flip de `news_enforce`
  en bot_config (Supabase), sin redeploy. Rollback = mismo flip.
- Complementa al VCB ([[bot-design-constraints]]): VCB reactivo (la vela ya
  explotó), NewsGuard predictivo (está agendada).

Regresión: sección [16] de `python -m tests.test_m5_and_budget`.
Requisito operativo: compilar y colgar CalendarExporter.mq5 en el terminal
(instrucciones en el header del .mq5); sin él, el guard queda permisivo y
loguea ERROR una vez.
