---
name: news-exporter-python
description: Exportador de calendario en Python (Forex Factory -> tiempo de servidor) que alimenta el NewsGuard; reemplaza el EA MQL5 en el VPS
metadata:
  type: project
---

Fix de 2026-08-13. Los bots logueaban `NewsGuard: calendario no disponible en
...\Common\Files\news_calendar.json` porque **ningun proceso producia ese JSON**:
el productor de diseño era el EA `scripts/CalendarExporter.mq5`, que un humano
debia colgar en un grafico de cada terminal ([[newsguard-phase1]]).

**Por que no el EA MQL5 en este VPS:** colgar el EA headless es fragil — el
auto-arranque via INI de `[StartUp]` NO adjunta el EA de forma fiable en build
6090 (el terminal se relanza solo con conflictos de puerto MCP), y en los clones
portables la carpeta `MQL5` del clon no comparte la Common que leen los bots.

**Fix — `scripts/news_exporter.py`** (proceso Python, como los bots):
- Descarga `https://nfs.faireconomy.media/ff_calendar_thisweek.json` (JSON
  publico; fechas ISO con offset -> UTC exacto; UA de navegador por el 403/1010
  de Cloudflare). Solo `thisweek` existe (nextweek da 404) y basta: news.py solo
  bloquea dentro de [T-pre, T+post], pre max 2h, y la semana rola el fin de semana.
- Convierte cada evento al epoch del SERVIDOR (lo que compara el bot:
  `self.now = tick.time`). El offset servidor<->UTC se deriva EN VIVO atacando en
  solo lectura un terminal MT5 ya corriendo (`mt5.initialize(path=...)` como 2º
  cliente — probado inocuo para el bot dueño), asi sigue el DST solo. Respaldo:
  zona `NEWS_SERVER_TZ` (default Europe/Bucharest = GMT+2/+3 como Vantage).
- Escribe el MISMO esquema que el EA (`{generated_at, server_time, events:[{time,
  currency, importance, name}]}`) en la carpeta Common -> **bot/news.py y la
  regresion [16] NO cambian**. Vantage server = UTC+3 en verano (medido +10800s).
- Bucle cada `NEWS_REFRESH_MINUTES` (15); `--once` para scheduler. Nunca muere:
  ante error (p.ej. 429 transitorio) conserva el JSON previo y reintenta.

**Un solo exportador para todas las instancias** (escribe la ruta comun que leen
todos los procesos Python via su %APPDATA%). Lo lanza `start_all.ps1` en FASE 4
(mata primero cualquiera previo) y lo mata `restart_all.ps1` (match ampliado a
`scripts\.news_exporter`). Idempotente y desacoplado de que instancia este activa.

**Sigue valiendo el fail-safe permisivo:** si el JSON falta/stale, NewsGuard no
bloquea y loguea una alerta deduplicada. Con `news_enforce=false` (default) es
observacion pura. El EA MQL5 queda como alternativa valida (mismo esquema).
