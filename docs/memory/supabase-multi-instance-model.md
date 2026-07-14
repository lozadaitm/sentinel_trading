---
name: supabase-multi-instance-model
description: Migracion a Supabase + multi-instancia por usuario; semantica de is_active (close-only)
metadata:
  type: project
---

El bot corre en Supabase (cloud) con multi-tenencia: **un proceso = un usuario = una cuenta
Vantage = un terminal MT5**. `user_id` (UUID) = `auth.users.id`.

- **Conexion:** `bot/db.py` usa `supabase-py` (REST) con la **service-role key** (bypassa RLS) y
  filtra por `USER_ID`. RLS (definida en `supabase_schema.sql`) solo protege el frontend.
- **Identidad por instancia:** via entorno/`.env` (`USER_ID`, `SYMBOL`, `MAGIC_NUMBER`, y
  `MT5_PATH/LOGIN/SERVER/PASSWORD` para `mt5.initialize()`). Un `.env` por instancia en
  `instances/`; se arranca con `scripts/run_instance.ps1` (o `launch_all.ps1`). Defaults de
  compat en `bot/config.py`.
- **Interruptor maestro = tabla `bot_instances`** (una fila por usuario). El **usuario** togglea
  `is_active`; el **bot** escribe `bot_status` (RUNNING/CLOSE_ONLY/FLAT/ERROR/STOPPED) y
  `last_heartbeat`.

**Semantica de `is_active` (NO es pause duro):**
- `true`  = prendido operando (abre nuevas + gestiona todo).
- `false` = apagado / **close-only**: NO abre nada nuevo pero SIGUE gestionando y cerrando lo
  abierto hasta quedar plano. Implementado con el flag `engine.close_only`, que se suma a los 4
  gates de apertura de `bot/strategy.py` (ENTRADA, recovery OP3, `_check_rescue` OP4,
  `_run_grinder` OP11), igual que `spread_high`/`vol_breaker`. **Hedge Lock (OP2), trailing,
  healer, profit banking y cierres siguen corriendo** (una OP1 abierta nunca queda desnuda).
  Por eso `on_tick()` corre siempre; nunca se hace pause+continue. Alineado con
  [[bot-design-constraints]].

**Logging cloud:** `db.log_sink` encola cada `write()` y un hilo flusher hace INSERT por lotes en
`bot_logs` (no bloquea el motor; un INSERT REST son 50-200 ms). El CSV local queda como respaldo.
Los logs llevan `user_id`+`symbol` (ver [[logging-taxonomy-and-ticket]]).

**Resiliencia:** `load_config`/`get_instance` cachean la ultima fila buena; ante hipo de red
reusan la cache en vez de tumbar el trading.
