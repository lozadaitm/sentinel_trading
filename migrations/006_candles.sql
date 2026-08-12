-- ==================================================================
-- 006_candles.sql  |  Velas OHLC para la grafica del dashboard
-- ==================================================================
-- Contexto: el dashboard quiere mostrar una grafica de precio con las
-- posiciones (abiertas e historicas) marcadas encima, estilo MetaTrader
-- minimalista. Supabase no tenia datos de precio: esta tabla los aporta.
--
-- El bot (SOLO el proceso m15, una fuente por cuenta) publica:
--   - backfill de ~400 velas M15 al arrancar (+ poda de >30 dias), y
--   - upsert de las 2 ultimas velas (la en formacion + la previa) en cada
--     heartbeat (~15 s). Ver bot/main.py y bot/db.py::upsert_candles.
--
-- `ts` es la hora de APERTURA de la vela en hora del servidor MT5 tratada
-- como UTC — el mismo criterio que bot_positions.open_time/close_time, asi
-- que velas y marcadores de posiciones quedan alineados entre si.
--
-- ESTADO: PENDIENTE de aplicar en Supabase (SQL Editor). Idempotente.
-- ==================================================================

CREATE TABLE IF NOT EXISTS public.bot_candles (
    user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    symbol     TEXT NOT NULL,
    timeframe  TEXT NOT NULL DEFAULT 'M15',
    ts         TIMESTAMPTZ NOT NULL,      -- apertura de la vela
    open       DOUBLE PRECISION NOT NULL,
    high       DOUBLE PRECISION NOT NULL,
    low        DOUBLE PRECISION NOT NULL,
    close      DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, symbol, timeframe, ts)
);

ALTER TABLE public.bot_candles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS bot_candles_owner_select ON public.bot_candles;
CREATE POLICY bot_candles_owner_select ON public.bot_candles
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

-- Realtime: la vela en formacion se refresca en cada heartbeat.
DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.bot_candles;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_object THEN NULL;
END $$;
