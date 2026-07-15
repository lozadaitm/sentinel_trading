# Addendum — nueva tabla `bot_positions` (histórico + vivo de posiciones)

> **Cómo usar este archivo.** Es un **prompt aditivo** para la instancia de Claude Code que ya está
> construyendo el dashboard de Sentinel (a partir de `docs/dashboard-handoff.md`). No repite todo
> el contexto original — solo lo nuevo desde entonces. Pégaselo tal cual.

---

## PROMPT

Desde el handoff original se agregó una tabla nueva en Supabase, **`bot_positions`**, ya
desplegada en producción (no es un placeholder). Es aditiva: no cambia nada de lo que ya
construiste, pero mejora varias métricas y habilita features nuevas. Intégrala así:

### Contrato de `bot_positions`

Tabla principal de posiciones — histórico completo + estado vivo. RLS `FOR SELECT` al dueño (el
upsert lo hace el bot con service-role, igual que las demás).

- `id` BIGSERIAL, `user_id` UUID, `ticket` BIGINT (ticket de MT5), `symbol` TEXT.
- `position_type` TEXT: `BUY` | `SELL`.
- `op_type` TEXT: `ENTRADA | PROTECCION | RECOVERY | RESCATE | GRINDER | OPERACION` — misma
  taxonomía que `bot_logs.log_type` para las aperturas.
- `comment` TEXT — comment original de MT5 (p.ej. `"SMC Buy Entry"`, `"Sentinel Op 4 (L2)"`).
- `lots`, `open_price`, `open_time` (TIMESTAMPTZ), `sl`, `tp`, `current_price` DOUBLE PRECISION.
- `profit`, `swap` DOUBLE PRECISION — **mientras `status='OPEN'`** son el P&L flotante actual;
  **cuando `status='CLOSED'`** son el P&L **total realizado** de esa posición (suma correcta de
  cierres parciales previos del Healer/Unwind, no solo el último tramo).
- `status` TEXT: `OPEN` | `CLOSED`.
- `close_price` DOUBLE PRECISION, `close_time` TIMESTAMPTZ — solo si `status='CLOSED'`.
- `updated_at` TIMESTAMPTZ. `UNIQUE(user_id, ticket)`.

El bot upsertea cada posición abierta en cada heartbeat (~15 s) y, al detectar que un ticket ya no
existe en MT5, la marca `CLOSED` con el cierre reconstruido del historial de deals de esa posición
(precio y hora exactos del cierre final, P&L total). Al arrancar, el bot también reconcilia
posiciones que quedaron `OPEN` en la tabla pero se cerraron mientras estaba apagado.

**No reemplaza `bot_logs`** — el feed de eventos sigue siendo la fuente para ver HEALER/UNWIND/
VCB/SYSTEM/ERROR con contexto narrativo. `bot_positions` es el registro estructurado, uno por
ticket, pensado para listados y métricas.

### Qué construir / actualizar

1. **Panel "Posiciones abiertas" (vivo)** — si hoy solo muestras el contador
   `bot_state.open_positions`, agrega el detalle: tabla de `bot_positions WHERE status='OPEN'`
   (ticket, tipo BUY/SELL, `op_type`, lotes, precio apertura, precio actual, P&L flotante
   `profit+swap`, SL/TP, tiempo abierta). Suscripción Realtime a INSERT/UPDATE.
2. **Histórico de posiciones (nuevo)** — tabla paginada de `bot_positions WHERE status='CLOSED'`,
   ordenable por `close_time` DESC, filtrable por `op_type`/`position_type`/rango de fechas.
   Columnas sugeridas: ticket, tipo, `op_type`, lotes, apertura (precio+hora), cierre (precio+hora),
   P&L final (`profit+swap`).
3. **Actualiza el mapeo de métricas** (si ya las implementaste con `bot_logs`, migra a
   `bot_positions` — es más simple y ya no depende de parsear texto):
   - **Operaciones totales** → `COUNT(*)` en `bot_positions` (antes: contar aperturas en
     `bot_logs`).
   - **% win/loss** → entre las filas `status='CLOSED'`: ganadoras = `profit + swap > 0`, resto
     perdedoras/breakeven. Es una comparación numérica directa sobre columnas reales — **ya no
     hace falta** parsear `bot_logs.message` (p.ej. `f1=-17.52`) para esto.
   - **Operaciones abiertas live**: el contador rápido sigue siendo `bot_state.open_positions`;
     para el **detalle por posición** usa `bot_positions WHERE status='OPEN'` (punto 1).
   - Ganancias totales $ y balance en uso **no cambian de fuente** (siguen en `bot_state`).

### Realtime

Pídele al operador que confirme que `bot_positions` está en la publicación `supabase_realtime`
(Database → Replication en el dashboard de Supabase, o `ALTER PUBLICATION supabase_realtime ADD
TABLE public.bot_positions;`). Sin eso, el panel de posiciones abiertas necesitaría polling.

### Qué NO hacer

- No elimines el feed de `bot_logs` ni la curva de balance basada en él — siguen siendo la fuente
  para eventos del sistema (HEALER, UNWIND, VCB, ERROR, etc.) que `bot_positions` no cubre.
- No asumas que `profit`/`swap` en una fila `OPEN` es el P&L final — es flotante, cambia cada
  heartbeat.
- No inventes columnas fuera de las listadas arriba.
