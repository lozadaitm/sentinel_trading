-- ==================================================================
-- 002_bot_id.sql  |  Multi-bot sobre una misma cuenta MT5
-- ==================================================================
-- Contexto: un usuario corre DOS motores contra la misma cuenta:
--   bot_id = 'm15'  -> Sentinel HyperGrinder (entradas, hedge, recovery, rescate)
--   bot_id = 'm5'   -> Grinder SmartCut (scalper independiente)
--
-- Hoy bot_instances y bot_state tienen user_id como PRIMARY KEY, y bot_config
-- es UNIQUE (user_id, symbol). Con dos procesos del mismo usuario y el mismo
-- simbolo, ambos leen/escriben la MISMA fila: se pisan config, heartbeat y
-- snapshot cada 15 s. Esta migracion añade el discriminador `bot_id` y mueve
-- las claves a compuestas.
--
-- COMPATIBILIDAD: todas las filas existentes quedan como 'm15' via DEFAULT, que
-- es exactamente lo que hoy son. El dashboard web que no filtre por bot_id
-- seguira viendo los datos del M15 sin cambios hasta que arranque el M5.
--
-- ESTADO: APLICADA a sentinel-platform (ttlfzaihkjdxkryrqnmk) el 2026-08-05
--   como migracion `bot_id_multi_bot_and_margin_governance`.
--   Las 5 tablas quedaron backfilleadas a 'm15' (1 instancia, 1 config,
--   1 state, 223 positions, 632 logs). Verificado: PKs compuestas activas,
--   RLS intacta en las 5 tablas, sin FKs que bloquearan el DROP CONSTRAINT.
--
-- ORDEN DE DESPLIEGUE:
--   1. Aplicar esta migracion.
--   2. Desplegar el bot (bot/db.py ya filtra por bot_id).
--   3. Actualizar el dashboard para filtrar/agrupar por bot_id.
--   4. Recien entonces arrancar la instancia M5.
-- ==================================================================

BEGIN;

-- ------------------------------------------------------------------
-- 1. Columna bot_id en las cinco tablas
-- ------------------------------------------------------------------
ALTER TABLE public.bot_instances
    ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT 'm15';

ALTER TABLE public.bot_config
    ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT 'm15';

ALTER TABLE public.bot_state
    ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT 'm15';

ALTER TABLE public.bot_positions
    ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT 'm15';

ALTER TABLE public.bot_logs
    ADD COLUMN IF NOT EXISTS bot_id TEXT NOT NULL DEFAULT 'm15';

-- ------------------------------------------------------------------
-- 2. bot_instances: PK (user_id) -> PK (user_id, bot_id)
--    Cada bot tiene su propio interruptor is_active y su propio heartbeat.
-- ------------------------------------------------------------------
ALTER TABLE public.bot_instances
    DROP CONSTRAINT IF EXISTS bot_instances_pkey;
ALTER TABLE public.bot_instances
    ADD CONSTRAINT bot_instances_pkey PRIMARY KEY (user_id, bot_id);

-- ------------------------------------------------------------------
-- 3. bot_state: PK (user_id) -> PK (user_id, bot_id)
--    Nota: balance/equity/margin_* son de la CUENTA (compartidos), pero
--    open_positions e initial_balance son por bot. El dashboard debe sumar
--    open_positions y tomar los de cuenta de cualquiera de las dos filas.
-- ------------------------------------------------------------------
ALTER TABLE public.bot_state
    DROP CONSTRAINT IF EXISTS bot_state_pkey;
ALTER TABLE public.bot_state
    ADD CONSTRAINT bot_state_pkey PRIMARY KEY (user_id, bot_id);

-- ------------------------------------------------------------------
-- 4. bot_config: UNIQUE (user_id, symbol) -> UNIQUE (user_id, symbol, bot_id)
--    Permite una fila de parametros por motor sobre el mismo simbolo.
-- ------------------------------------------------------------------
ALTER TABLE public.bot_config
    DROP CONSTRAINT IF EXISTS bot_config_user_id_symbol_key;
ALTER TABLE public.bot_config
    ADD CONSTRAINT bot_config_user_id_symbol_bot_id_key
    UNIQUE (user_id, symbol, bot_id);

-- ------------------------------------------------------------------
-- 5. bot_positions: la UNIQUE (user_id, ticket) se MANTIENE.
--    Los tickets de MT5 son unicos por cuenta, asi que dos bots nunca
--    colisionan. bot_id es solo para filtrar/agrupar en el dashboard.
-- ------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_bot_positions_user_bot_status
    ON public.bot_positions (user_id, bot_id, status);

-- ------------------------------------------------------------------
-- 6. bot_logs: indice por bot para el stream del dashboard.
-- ------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_bot_logs_user_bot_ts
    ON public.bot_logs (user_id, bot_id, created_at DESC);

-- ------------------------------------------------------------------
-- 7. Gobierno de margen: parametros nuevos en bot_config.
--    Ver bot/budget.py. Defaults = rol SENIOR (M15).
-- ------------------------------------------------------------------
ALTER TABLE public.bot_config
    ADD COLUMN IF NOT EXISTS equity_weight      DOUBLE PRECISION DEFAULT 0.80,
    ADD COLUMN IF NOT EXISTS margin_cap_pct     DOUBLE PRECISION DEFAULT 60.0,
    ADD COLUMN IF NOT EXISTS peer_reserve_mult  DOUBLE PRECISION DEFAULT 1.5,
    ADD COLUMN IF NOT EXISTS ml_no_add          DOUBLE PRECISION DEFAULT 200.0,
    ADD COLUMN IF NOT EXISTS ml_flatten         DOUBLE PRECISION DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS margin_safety_mult DOUBLE PRECISION DEFAULT 1.1;

-- ------------------------------------------------------------------
-- 7b. Paridad codigo<->DB. Estos parametros vivian SOLO en el diccionario
--     DEFAULTS de bot/strategy.py: no tenian columna, asi que no eran
--     ajustables desde el dashboard y el bot usaba siempre el valor del
--     codigo. Los defaults de abajo replican ese valor exacto, de modo que
--     el comportamiento no cambia; solo pasan a ser configurables.
--     (Detectado al auditar la base antes de migrar: max_net_lots, el
--     parametro que estrangula la escalera del martingala, no existia.)
-- ------------------------------------------------------------------
ALTER TABLE public.bot_config
    ADD COLUMN IF NOT EXISTS max_net_lots             DOUBLE PRECISION DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS max_rescue_legs          INTEGER          DEFAULT 3,
    ADD COLUMN IF NOT EXISTS rescue_cooldown          INTEGER          DEFAULT 30,
    ADD COLUMN IF NOT EXISTS recovery_min_spacing_atr DOUBLE PRECISION DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS use_recovery_h4_gate     BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS use_vol_breaker          BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS vcb_atr_mult             DOUBLE PRECISION DEFAULT 2.8,
    ADD COLUMN IF NOT EXISTS healer_min_core          INTEGER          DEFAULT 3,
    ADD COLUMN IF NOT EXISTS min_green_profit         DOUBLE PRECISION DEFAULT 3.0,
    ADD COLUMN IF NOT EXISTS grinder_cooldown         INTEGER          DEFAULT 120,
    ADD COLUMN IF NOT EXISTS grinder_min_atr_points   INTEGER          DEFAULT 80;

-- ------------------------------------------------------------------
-- 8. Parametros del motor M5 (port de Grinder_Anterior.mq5).
--    Viven en la MISMA tabla: la fila bot_id='m5' los usa y la fila
--    bot_id='m15' simplemente los ignora.
-- ------------------------------------------------------------------
ALTER TABLE public.bot_config
    -- Gestion
    ADD COLUMN IF NOT EXISTS m5_lots_per_1000    DOUBLE PRECISION DEFAULT 0.02,
    ADD COLUMN IF NOT EXISTS m5_max_lot_cap      DOUBLE PRECISION DEFAULT 0.50,
    ADD COLUMN IF NOT EXISTS m5_min_margin_level DOUBLE PRECISION DEFAULT 150.0,
    -- Gran Hermano M15
    ADD COLUMN IF NOT EXISTS m5_use_bigbro       BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS m5_bigbro_period    INTEGER          DEFAULT 50,
    -- Riesgo
    ADD COLUMN IF NOT EXISTS m5_sl_atr           DOUBLE PRECISION DEFAULT 2.0,
    ADD COLUMN IF NOT EXISTS m5_max_trade_min    INTEGER          DEFAULT 45,
    ADD COLUMN IF NOT EXISTS m5_hard_sl_points   DOUBLE PRECISION DEFAULT 1000.0,
    -- Scalper (reversion)
    ADD COLUMN IF NOT EXISTS m5_use_reversion    BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS m5_ma_period        INTEGER          DEFAULT 50,
    ADD COLUMN IF NOT EXISTS m5_adx_period       INTEGER          DEFAULT 14,
    ADD COLUMN IF NOT EXISTS m5_rsi_period       INTEGER          DEFAULT 14,
    ADD COLUMN IF NOT EXISTS m5_base_tolerance   DOUBLE PRECISION DEFAULT 0.20,
    ADD COLUMN IF NOT EXISTS m5_rsi_os           INTEGER          DEFAULT 30,
    ADD COLUMN IF NOT EXISTS m5_rsi_ob           INTEGER          DEFAULT 70,
    -- Filtro de banda: ATR-relativo (sustituye al InpMaxBBWidth fijo en puntos,
    -- que a precio de oro actual bloqueaba el 100% de las señales de reversion).
    ADD COLUMN IF NOT EXISTS m5_band_mode        TEXT             DEFAULT 'atr',
    ADD COLUMN IF NOT EXISTS m5_band_atr_mult    DOUBLE PRECISION DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS m5_max_band_atr     DOUBLE PRECISION DEFAULT 2.5,
    -- Surfer (tendencia)
    ADD COLUMN IF NOT EXISTS m5_use_surfer       BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS m5_trend_adx        INTEGER          DEFAULT 30,
    ADD COLUMN IF NOT EXISTS m5_surfer_rsi_max   INTEGER          DEFAULT 85,
    ADD COLUMN IF NOT EXISTS m5_force_rsi_buy    INTEGER          DEFAULT 55,
    ADD COLUMN IF NOT EXISTS m5_force_rsi_sell   INTEGER          DEFAULT 45,
    -- Tiempo
    ADD COLUMN IF NOT EXISTS m5_use_time_filter  BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS m5_start_hour       INTEGER          DEFAULT 1,
    ADD COLUMN IF NOT EXISTS m5_end_hour         INTEGER          DEFAULT 23,
    -- Trailing 2 velocidades
    ADD COLUMN IF NOT EXISTS m5_use_trailing     BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS m5_trail_start      INTEGER          DEFAULT 100,
    ADD COLUMN IF NOT EXISTS m5_trail_dist       INTEGER          DEFAULT 50,
    ADD COLUMN IF NOT EXISTS m5_trail_step       INTEGER          DEFAULT 10,
    ADD COLUMN IF NOT EXISTS m5_turbo_trigger    INTEGER          DEFAULT 300,
    ADD COLUMN IF NOT EXISTS m5_turbo_dist       INTEGER          DEFAULT 150,
    -- Añadidos que Anterior no tenia
    ADD COLUMN IF NOT EXISTS m5_max_spread       INTEGER          DEFAULT 350,
    ADD COLUMN IF NOT EXISTS m5_use_vcb          BOOLEAN          DEFAULT true,
    ADD COLUMN IF NOT EXISTS m5_vcb_atr_mult     DOUBLE PRECISION DEFAULT 2.8;

COMMIT;

-- ==================================================================
-- SEED: YA EJECUTADO para user_id e8d840ab-9b7c-4a05-adf9-fec9f1d4adb7
-- (bot_instances.is_active = false: el M5 no operara hasta encenderlo).
-- Se conserva como referencia para nuevos usuarios.
--
-- SEED (opcional): crear la fila de config y la instancia del bot M5
-- clonando la del M15. Sustituir <USER_UUID> y <SYMBOL>.
-- Ejecutar SOLO cuando se vaya a arrancar el M5.
-- ==================================================================
--
-- INSERT INTO public.bot_config (user_id, symbol, bot_id, is_active, status)
-- VALUES ('<USER_UUID>', '<SYMBOL>', 'm5', true, 'ACTIVE')
-- ON CONFLICT (user_id, symbol, bot_id) DO NOTHING;
--
-- -- Rol JUNIOR: cede margen ante el M15 (ver bot/budget.py).
-- UPDATE public.bot_config
--    SET equity_weight  = 0.20,
--        margin_cap_pct = 10.0,
--        ml_no_add      = 400.0,
--        ml_flatten     = 250.0
--  WHERE user_id = '<USER_UUID>' AND bot_id = 'm5';
--
-- INSERT INTO public.bot_instances (user_id, bot_id, label, is_active, bot_status)
-- VALUES ('<USER_UUID>', 'm5', 'Grinder M5', false, 'STOPPED')
-- ON CONFLICT (user_id, bot_id) DO NOTHING;
