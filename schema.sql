-- ==================================================================
-- schema.sql  |  Hyper Grinder v20.0 (port Python desde original_MQL5)
-- Tabla de configuracion/control del bot. TODOS los parametros del EA
-- MQL5 viven aqui. El bot los lee en cada ciclo; 'status' controla
-- arranque/pausa ('ACTIVE' = corre, cualquier otro = espera).
-- ==================================================================

CREATE TABLE IF NOT EXISTS public.bot_config (
    id                      BIGSERIAL PRIMARY KEY,
    user_id                 UUID         NOT NULL,
    symbol                  TEXT         NOT NULL,
    is_active               BOOLEAN      NOT NULL DEFAULT true,
    status                  TEXT         NOT NULL DEFAULT 'INACTIVE',
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

    UNIQUE (user_id, symbol)
);

-- ------------------------------------------------------------------
-- Fila default. Valores tomados 1:1 de los input del EA MQL5.
-- Ajusta user_id / symbol a tu broker antes de correr el bot.
-- ------------------------------------------------------------------
INSERT INTO public.bot_config (user_id, symbol, is_active, status)
VALUES ('81118671-d5ba-4d49-9fb3-4499b54a3d93', 'XAUUSD+', true, 'INACTIVE')
ON CONFLICT (user_id, symbol) DO NOTHING;

-- ==================================================================
-- bot_logs  |  Persistencia de cada write() del Logger (sink en main.py).
-- Insercion best-effort: si falla, NO debe tumbar el trading (ver db.insert_log).
-- ==================================================================
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

-- Consulta tipica: ultimos logs de un bot concreto.
CREATE INDEX IF NOT EXISTS idx_bot_logs_user_symbol_ts
    ON public.bot_logs (user_id, symbol, created_at DESC);
