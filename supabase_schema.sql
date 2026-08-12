-- ==================================================================
-- supabase_schema.sql  |  Hyper Grinder v20.0 — despliegue en Supabase
-- ------------------------------------------------------------------
-- Multi-tenencia: user_id (UUID) = auth.users.id. Cada usuario/cuenta
-- Vantage corre su propia instancia del bot (un proceso por usuario).
--
-- Seguridad:
--   - El bot usa la SERVICE_ROLE key -> bypassa RLS (y filtra por user_id).
--   - El frontend usa el JWT del usuario -> RLS lo restringe a sus filas.
--
-- Correr en: Supabase Dashboard > SQL Editor (idempotente, IF NOT EXISTS).
-- ==================================================================

-- ==================================================================
-- bot_instances  |  Una fila por usuario/cuenta. Interruptor maestro.
--   is_active  -> lo togglea el USUARIO (frontend):
--                   true  = prendido operando (abre + gestiona)
--                   false = apagado / close-only (no abre; gestiona y cierra)
--   bot_status -> lo escribe el BOT (RUNNING / CLOSE_ONLY / FLAT / ERROR / STOPPED)
--   last_heartbeat -> lo escribe el BOT cada N s (online/offline en la UI)
-- ==================================================================
CREATE TABLE IF NOT EXISTS public.bot_instances (
    user_id        UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    bot_id         TEXT        NOT NULL DEFAULT 'm15',  -- 'm15' Sentinel | 'm5' Grinder
    label          TEXT,                                    -- "Cuenta Vantage #123"
    account_login  BIGINT,                                  -- login MT5/Vantage (informativo)
    is_active      BOOLEAN     NOT NULL DEFAULT false,      -- señal ON/OFF del usuario
    bot_status     TEXT        NOT NULL DEFAULT 'STOPPED',  -- lo reporta el bot
    last_heartbeat TIMESTAMPTZ,                             -- lo reporta el bot
    -- Cierre forzado: lo setea el USUARIO (dashboard, junto con is_active=false);
    -- el BOT lo lee en el refresh de control, cierra todas sus posiciones
    -- (asumiendo el flotante) y lo resetea a false. Ver migrations/004.
    force_close    BOOLEAN     NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, bot_id)
);

-- ==================================================================
-- account_settings  |  Preferencias a nivel CUENTA (1 fila por usuario).
--   profit_target_pct -> % de ganancia objetivo sobre la base; al alcanzarlo
--     (equity vs base) el bot apaga todas las instancias del usuario
--     (close-only), envia email y estampa target_reached_at (claim atomico,
--     dedup entre m15/m5). El usuario re-arma limpiando target_reached_at
--     al guardar un objetivo nuevo. Ver migrations/004_profit_target.sql.
--   initial_deposit -> monto de fondeo declarado; NULL = usar el
--     bot_state.initial_balance auto-capturado de MT5 al primer arranque.
-- ==================================================================
CREATE TABLE IF NOT EXISTS public.account_settings (
    user_id           UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    initial_deposit   DOUBLE PRECISION,
    profit_target_pct DOUBLE PRECISION,
    target_reached_at TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ==================================================================
-- bot_config  |  Parametros de estrategia por (user_id, symbol).
--   Soporta varios simbolos por usuario (aspiracion futura); hoy 1.
-- ==================================================================
CREATE TABLE IF NOT EXISTS public.bot_config (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 UUID         NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    symbol                  TEXT         NOT NULL,
    bot_id                  TEXT         NOT NULL DEFAULT 'm15',  -- motor dueño de esta fila
    is_active               BOOLEAN      NOT NULL DEFAULT true,   -- incluye/excluye este simbolo
    status                  TEXT         NOT NULL DEFAULT 'INACTIVE', -- observacional (compat)
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- 0. Filtros de mercado
    inp_max_spread          INTEGER          DEFAULT 350,

    -- 1. Gestion de riesgo base
    base_risk               DOUBLE PRECISION DEFAULT 5000.0,
    base_lots               DOUBLE PRECISION DEFAULT 0.12,
    max_entry_lots          DOUBLE PRECISION DEFAULT 1.00,
    max_recovery_lots       DOUBLE PRECISION DEFAULT 2.00,

    -- 2. Salida maestra
    use_basket_close        BOOLEAN          DEFAULT true,
    basket_percent          DOUBLE PRECISION DEFAULT 0.15,
    commission_per_lot      DOUBLE PRECISION DEFAULT 6.0,
    use_dynamic_retrace     BOOLEAN          DEFAULT true,
    retrace_atr_mult        DOUBLE PRECISION DEFAULT 0.1,
    fixed_retrace           DOUBLE PRECISION DEFAULT 2.0,
    min_green_profit        DOUBLE PRECISION DEFAULT 3.0,

    -- 3. Cobertura (Sentinel)
    use_dynamic_hedge       BOOLEAN          DEFAULT true,
    hedge_dist              INTEGER          DEFAULT 350,
    hedge_atr_mult          DOUBLE PRECISION DEFAULT 2.0,

    -- 4. Healer inteligente
    use_healer              BOOLEAN          DEFAULT true,
    healer_balance_bias     BOOLEAN          DEFAULT true,
    healer_min_core         INTEGER          DEFAULT 3,
    use_unwind_mode         BOOLEAN          DEFAULT true,
    unwind_atr_mult         DOUBLE PRECISION DEFAULT 2.0,
    unwind_money_floor      DOUBLE PRECISION DEFAULT 30.0,

    -- 5. Bio-Reactor (Smart Grinder)
    use_grinder             BOOLEAN          DEFAULT true,
    grinder_lots            DOUBLE PRECISION DEFAULT 0.05,
    grinder_time_stop       INTEGER          DEFAULT 45,
    grinder_adx_trend       INTEGER          DEFAULT 30,
    grinder_rsi_ob          INTEGER          DEFAULT 70,
    grinder_rsi_os          INTEGER          DEFAULT 30,
    grinder_use_trail       BOOLEAN          DEFAULT true,
    grinder_trail_start     INTEGER          DEFAULT 50,
    grinder_trail_dist      INTEGER          DEFAULT 20,
    grinder_turbo_trig      INTEGER          DEFAULT 150,
    grinder_turbo_dist      INTEGER          DEFAULT 50,

    -- 6. Filtros de entrada (core)
    use_pullback            BOOLEAN          DEFAULT true,
    atr_entry_distance      DOUBLE PRECISION DEFAULT 2.5,
    use_h4_struct           BOOLEAN          DEFAULT true,
    entry_rsi_max           INTEGER          DEFAULT 75,
    entry_rsi_min           INTEGER          DEFAULT 25,

    -- 7. Trailing fluido (Sentinel)
    use_smart_trail         BOOLEAN          DEFAULT true,
    trail_activate          DOUBLE PRECISION DEFAULT 1.0,
    trail_dist_atr          DOUBLE PRECISION DEFAULT 1.5,
    use_op3_trail           BOOLEAN          DEFAULT true,
    op3_start_atr           DOUBLE PRECISION DEFAULT 0.60,
    op3_base_dist_atr       DOUBLE PRECISION DEFAULT 0.40,
    op3_turbo_trigger_atr   DOUBLE PRECISION DEFAULT 3.0,
    op3_turbo_dist_atr      DOUBLE PRECISION DEFAULT 1.2,

    -- 8. Cerebro recuperacion
    use_m5_confirm          BOOLEAN          DEFAULT true,
    min_tech_wait           INTEGER          DEFAULT 60,

    -- 9. Modo rescate Sentinel
    use_rescue_mode         BOOLEAN          DEFAULT true,
    rescue_atr_mult         DOUBLE PRECISION DEFAULT 4.0,
    rescue_rsi              INTEGER          DEFAULT 70,
    rescue_target_pct       DOUBLE PRECISION DEFAULT 0.10,
    dd_percent_l2           DOUBLE PRECISION DEFAULT 3.0,
    dd_percent_l3           DOUBLE PRECISION DEFAULT 8.0,

    -- 9b. Supervivencia / respiro ante volatilidad anomala (Fork A)
    use_vol_breaker         BOOLEAN          DEFAULT true,
    vcb_atr_mult            DOUBLE PRECISION DEFAULT 2.8,
    max_net_lots            DOUBLE PRECISION DEFAULT 1.0,
    max_rescue_legs         INTEGER          DEFAULT 3,
    use_recovery_h4_gate    BOOLEAN          DEFAULT true,

    -- 10. Otros
    fast_ma                 INTEGER          DEFAULT 9,
    atr_period              INTEGER          DEFAULT 14,
    close_friday            BOOLEAN          DEFAULT true,
    friday_hour             INTEGER          DEFAULT 20,
    monday_start_hour       INTEGER          DEFAULT 10,
    enable_file_log         BOOLEAN          DEFAULT true,
    log_file_name           TEXT             DEFAULT 'Gold_HyperGrinder_v20',
    cooldown_seconds        INTEGER          DEFAULT 10,

    UNIQUE (user_id, symbol, bot_id)
);

-- ==================================================================
-- bot_logs  |  Persistencia de cada write() del Logger (sink en db.py).
--   Insercion best-effort en lote desde el bot (service-role).
-- ==================================================================
CREATE TABLE IF NOT EXISTS public.bot_logs (
    id          BIGSERIAL    PRIMARY KEY,
    user_id     UUID             NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    bot_id      TEXT             NOT NULL DEFAULT 'm15',
    symbol      TEXT             NOT NULL,
    log_type    TEXT             NOT NULL,
    message     TEXT             NOT NULL,
    price       DOUBLE PRECISION DEFAULT 0.0,
    lots        DOUBLE PRECISION DEFAULT 0.0,
    balance     DOUBLE PRECISION DEFAULT 0.0,
    ticket      BIGINT           DEFAULT 0,
    created_at  TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_logs_user_symbol_ts
    ON public.bot_logs (user_id, symbol, created_at DESC);

-- ==================================================================
-- bot_state  |  Snapshot en vivo (1 fila por usuario) para el dashboard.
--   Lo escribe el BOT en cada heartbeat (upsert, service-role).
--   balance/equity/margin_* vienen de mt5.account_info(); open_positions
--   de broker.positions(); initial_balance se fija una sola vez al arrancar
--   (se reusa entre restarts). Ver bot/db.py::report_state.
-- ==================================================================
CREATE TABLE IF NOT EXISTS public.bot_state (
    user_id         UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    bot_id          TEXT        NOT NULL DEFAULT 'm15',
    symbol          TEXT,
    balance         DOUBLE PRECISION,
    equity          DOUBLE PRECISION,
    margin_used     DOUBLE PRECISION,
    margin_free     DOUBLE PRECISION,
    floating_pnl    DOUBLE PRECISION,
    open_positions  INTEGER,
    -- Base del objetivo de ganancia: capturado de MT5 en la primera corrida y
    -- AJUSTADO despues por depositos/retiros (deals BALANCE/CREDIT) para que
    -- la ganancia medida sea solo la del trading. Ver migrations/005.
    initial_balance DOUBLE PRECISION,
    -- Ancla de reconciliacion de flujos: ticket del ultimo deal de
    -- deposito/retiro ya incorporado a initial_balance.
    last_flow_ticket BIGINT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, bot_id)
);

-- ==================================================================
-- bot_positions  |  Tabla principal de posiciones (historico + vivo).
--   El bot upsertea cada abierta en cada heartbeat (status=OPEN, precio y
--   P&L flotante frescos); al desaparecer de MT5 se marca CLOSED con el
--   cierre reconstruido del historial de deals (precio, hora, P&L total
--   incl. cierres parciales del Healer/Unwind). Solo user_id/ticket/status
--   son NOT NULL: un cierre puede upsertear sin repetir los datos de
--   apertura. Ver bot/db.py::upsert_positions, bot/main.py.
-- ==================================================================
CREATE TABLE IF NOT EXISTS public.bot_positions (
    id             BIGSERIAL   PRIMARY KEY,
    user_id        UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    bot_id         TEXT        NOT NULL DEFAULT 'm15',
    ticket         BIGINT      NOT NULL,
    symbol         TEXT,
    position_type  TEXT,                    -- BUY | SELL
    op_type        TEXT,                    -- ENTRADA | PROTECCION | RECOVERY | RESCATE | GRINDER | OPERACION
    comment        TEXT,                    -- comment original de MT5 (p.ej. "SMC Buy Entry")
    lots           DOUBLE PRECISION,
    open_price     DOUBLE PRECISION,
    open_time      TIMESTAMPTZ,
    sl             DOUBLE PRECISION,
    tp             DOUBLE PRECISION,
    current_price  DOUBLE PRECISION,        -- solo vivo (mientras status=OPEN)
    profit         DOUBLE PRECISION,        -- flotante si OPEN, total realizado si CLOSED
    swap           DOUBLE PRECISION,
    status         TEXT        NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED
    close_price    DOUBLE PRECISION,
    close_time     TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, ticket)
);

CREATE INDEX IF NOT EXISTS idx_bot_positions_user_status
    ON public.bot_positions (user_id, status);
CREATE INDEX IF NOT EXISTS idx_bot_positions_user_open_time
    ON public.bot_positions (user_id, open_time DESC);

-- ==================================================================
-- bot_candles  |  Velas OHLC para la grafica del dashboard.
--   Las publica SOLO el proceso m15 (una fuente por cuenta): backfill de
--   ~400 velas M15 al arrancar (+ poda >30 dias) y upsert de las 2 ultimas
--   en cada heartbeat. `ts` = apertura de la vela en hora del servidor MT5
--   tratada como UTC (mismo criterio que bot_positions.open_time: velas y
--   marcadores quedan alineados). Ver migrations/006_candles.sql.
-- ==================================================================
CREATE TABLE IF NOT EXISTS public.bot_candles (
    user_id    UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    symbol     TEXT NOT NULL,
    timeframe  TEXT NOT NULL DEFAULT 'M15',
    ts         TIMESTAMPTZ NOT NULL,
    open       DOUBLE PRECISION NOT NULL,
    high       DOUBLE PRECISION NOT NULL,
    low        DOUBLE PRECISION NOT NULL,
    close      DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, symbol, timeframe, ts)
);

-- ==================================================================
-- Row Level Security. El service-role bypassa TODO esto automaticamente;
-- estas politicas aplican al frontend (rol authenticated con su JWT).
-- ==================================================================
ALTER TABLE public.bot_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bot_config    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bot_logs      ENABLE ROW LEVEL SECURITY;

-- bot_instances: el usuario gestiona (lee/edita) SOLO su propia fila.
DROP POLICY IF EXISTS bot_instances_owner ON public.bot_instances;
CREATE POLICY bot_instances_owner ON public.bot_instances
    FOR ALL TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- account_settings: el usuario gestiona (lee/edita) SOLO su propia fila.
ALTER TABLE public.account_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_settings_owner ON public.account_settings;
CREATE POLICY account_settings_owner ON public.account_settings
    FOR ALL TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- bot_config: idem, solo sus filas.
DROP POLICY IF EXISTS bot_config_owner ON public.bot_config;
CREATE POLICY bot_config_owner ON public.bot_config
    FOR ALL TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- bot_logs: el usuario solo LEE sus logs (el INSERT lo hace el bot con service-role).
DROP POLICY IF EXISTS bot_logs_owner_select ON public.bot_logs;
CREATE POLICY bot_logs_owner_select ON public.bot_logs
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

-- bot_state: el usuario solo LEE su snapshot (el UPSERT lo hace el bot con service-role).
ALTER TABLE public.bot_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS bot_state_owner_select ON public.bot_state;
CREATE POLICY bot_state_owner_select ON public.bot_state
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

-- bot_positions: el usuario solo LEE sus posiciones (el UPSERT lo hace el bot con service-role).
ALTER TABLE public.bot_positions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS bot_positions_owner_select ON public.bot_positions;
CREATE POLICY bot_positions_owner_select ON public.bot_positions
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

-- bot_candles: el usuario solo LEE sus velas (el UPSERT lo hace el bot con service-role).
ALTER TABLE public.bot_candles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS bot_candles_owner_select ON public.bot_candles;
CREATE POLICY bot_candles_owner_select ON public.bot_candles
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());

-- ==================================================================
-- Alta de una instancia (ejecutar por usuario tras crearlo en Supabase Auth).
-- Reemplaza <USER_UUID> por el auth.users.id real y <SYMBOL> por el simbolo.
-- ------------------------------------------------------------------
-- INSERT INTO public.bot_instances (user_id, label, account_login, is_active)
-- VALUES ('<USER_UUID>', 'Cuenta Vantage', 0, false)
-- ON CONFLICT (user_id) DO NOTHING;
--
-- INSERT INTO public.bot_config (user_id, symbol, is_active, status)
-- VALUES ('<USER_UUID>', '<SYMBOL>', true, 'INACTIVE')
-- ON CONFLICT (user_id, symbol) DO NOTHING;
-- ==================================================================
