---
name: logging-taxonomy-and-ticket
description: Taxonomía de log_types de bot_logs, columna ticket, y dónde se loguea cada evento (aperturas en broker, cierres en strategy)
metadata:
  node_type: memory
  type: reference
---

Sistema de logging del bot (`bot/logger.py` multi-sink → consola + CSV + `bot_logs` DB + buffer TUI). Firma: `Logger.write(log_type, message, price, lots, balance, ticket=0)`; los sinks reciben `(log_type, message, ts, price, lots, balance, ticket)`.

**Columna `ticket`** (BIGINT, default 0) en `bot_logs` y en el CSV — commit del 2026-07-07. 0 = evento sin posición asociada. Aplicada a la DB viva con `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

**Dónde se loguea cada evento:**
- **Aperturas** → centralizadas en `broker.market_order` (`_log_open`), NO en strategy. Antes eran silenciosas (por eso el análisis forense del reporte MT5 tuvo que reconstruir los opens). log_type por comment: `ENTRADA` (SMC), `PROTECCION` (Hedge Lock), `RECOVERY` (Op3), `RESCATE` (Sentinel Op 4), `GRINDER`, o `OPERACION` (fallback). Órdenes rechazadas → `ERROR` con retcode. El ticket = `res.order`.
- **Cierres** → en strategy con contexto: `HEALER` (ticket del peor leg), `UNWIND` (best_pos), `GRINDER` (TimeStop), y `_close_all` emite una traza por-leg `CIERRE` (ticket + PnL de cada leg) además del resumen `EXITO` (sin ticket, es agregado del ciclo).
- **Otros sin ticket:** `SYSTEM`, `VCB`, `SHADOW`.

**Caveat schema drift:** la tabla `bot_logs` VIVA tiene la columna de tiempo como `ts` (timestamptz, default now()), pero `schema.sql` la declara `created_at`. `db.insert_log` lista columnas explícitas y deja el timestamp por default, así que funciona con cualquiera de los dos nombres. Si algún día se re-despliega desde `schema.sql` habrá `created_at`; tenerlo presente al consultar. Un deploy fresco desde `schema.sql` ya incluye `ticket`.
