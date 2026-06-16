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
