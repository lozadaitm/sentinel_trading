-- ==================================================================
-- migrations.sql  |  Hyper Grinder v20.0
-- Migraciones incrementales para aplicar sobre una BD ya desplegada.
-- TODO idempotente (IF NOT EXISTS): seguro de re-ejecutar.
--
-- Deploy desde cero  -> usa schema.sql (incluye estas tablas ya).
-- Deploy ya existente -> aplica este archivo:  psql "$DATABASE_URL" -f migrations.sql
-- ==================================================================

-- ------------------------------------------------------------------
-- 2026-06-16 | bot_logs: persiste cada write() del Logger via sink.
--             Ver bot/db.py::insert_log y bot/main.py (logger.sink).
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.bot_logs (
    id          BIGSERIAL    PRIMARY KEY,
    user_id     UUID             NOT NULL,
    symbol      TEXT             NOT NULL,
    log_type    TEXT             NOT NULL,
    message     TEXT             NOT NULL,
    price       DOUBLE PRECISION DEFAULT 0.0,
    lots        DOUBLE PRECISION DEFAULT 0.0,
    balance     DOUBLE PRECISION DEFAULT 0.0,
    created_at  TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_logs_user_symbol_ts
    ON public.bot_logs (user_id, symbol, created_at DESC);

-- ------------------------------------------------------------------
-- 2026-06-17 | OP12: piso verde del trail de cesta para OP1 sola.
--             Si el cierre del trail caeria bajo min_green con una sola
--             posicion core (core_count == 1), se DESARMA y la entrada
--             vuelve al Hedge Lock en vez de realizar una perdida. Con
--             >=2 legs el banking de cesta queda intacto.
--             Ver bot/strategy.py::on_tick (MONITOR DE SALIDA).
-- ------------------------------------------------------------------
ALTER TABLE public.bot_config
    ADD COLUMN IF NOT EXISTS min_green_profit DOUBLE PRECISION DEFAULT 3.0;

-- ------------------------------------------------------------------
-- 2026-06-18 | Fork A: respiro/supervivencia ante volatilidad anomala.
--   VCB (circuit breaker de volatilidad) + tope de exposicion neta +
--   cap de legs de Op4 + gate de estructura H4 para recovery/rescate.
--   Evita el apilado de martingala contra una noticia/manipulacion que
--   casi quema la cuenta el 2026.06.17 21:00. Ver bot/strategy.py.
-- ------------------------------------------------------------------
ALTER TABLE public.bot_config
    ADD COLUMN IF NOT EXISTS use_vol_breaker      BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS vcb_atr_mult         DOUBLE PRECISION DEFAULT 2.8,
    ADD COLUMN IF NOT EXISTS max_net_lots         DOUBLE PRECISION DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS max_rescue_legs      INTEGER          DEFAULT 3,
    ADD COLUMN IF NOT EXISTS use_recovery_h4_gate BOOLEAN          DEFAULT true;

-- ------------------------------------------------------------------
-- 2026-06-19 | Healer: gate de profundidad de cesta.
--   El Healer solo ampu­ta cuando hay >= healer_min_core posiciones core
--   (OP3/OP4+). Con OP1 sola (core==1) la cobertura es el Hedge Lock, no
--   la amputacion; con OP1+Hedge (core==2) el hedge ya congela. Antes el
--   Healer amputaba a cualquier core y realizaba en rojo entradas que aun
--   debian cubrirse. Ver bot/strategy.py::_check_healing.
-- ------------------------------------------------------------------
ALTER TABLE public.bot_config
    ADD COLUMN IF NOT EXISTS healer_min_core INTEGER DEFAULT 3;

-- ------------------------------------------------------------------
-- 2026-07-07 | bot_logs: ticket de la operacion + logging mas verbose.
--   Se persiste el ticket MT5 de cada evento con posicion asociada
--   (aperturas: ENTRADA/PROTECCION/RECOVERY/RESCATE/GRINDER; cierres:
--   HEALER/UNWIND/GRINDER/CIERRE). 0 para eventos sin ticket (SYSTEM/VCB/EXITO).
--   Ver bot/logger.py, bot/db.py::insert_log, bot/broker.py::market_order.
-- ------------------------------------------------------------------
ALTER TABLE public.bot_logs
    ADD COLUMN IF NOT EXISTS ticket BIGINT DEFAULT 0;

-- ------------------------------------------------------------------
-- 2026-07-15 | bot_state: snapshot en vivo (1 fila por usuario) para el
--   dashboard web. El bot lo upsertea en cada heartbeat (balance, equity,
--   margen usado/libre, PnL flotante, posiciones abiertas, saldo inicial).
--   RLS: el usuario solo LEE su fila; el UPSERT lo hace el bot (service-role).
--   Ver bot/db.py::report_state / get_state_initial_balance, bot/main.py.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.bot_state (
    user_id         UUID        PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    symbol          TEXT,
    balance         DOUBLE PRECISION,
    equity          DOUBLE PRECISION,
    margin_used     DOUBLE PRECISION,
    margin_free     DOUBLE PRECISION,
    floating_pnl    DOUBLE PRECISION,
    open_positions  INTEGER,
    initial_balance DOUBLE PRECISION,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.bot_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS bot_state_owner_select ON public.bot_state;
CREATE POLICY bot_state_owner_select ON public.bot_state
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());
