-- ==================================================================
-- 004_profit_target.sql  |  Objetivo de ganancia + cierre forzado
-- ==================================================================
-- Contexto: el usuario define un % de ganancia objetivo sobre su saldo
-- inicial. Cuando la cuenta lo alcanza (medido en EQUITY, incluye el
-- flotante), el bot:
--   1. Reclama el evento (target_reached_at, claim atomico -> un solo email
--      aunque corran m15 y m5 a la vez).
--   2. Apaga TODAS las instancias del usuario (is_active=false = close-only:
--      no abre nada nuevo, sigue gestionando/cerrando lo abierto).
--   3. Envia un correo de notificacion (bot/notify.py, SMTP por .env).
--
-- El usuario decide entonces en el dashboard:
--   a. ESPERAR a que el bot termine de gestionar lo abierto (default), o
--   b. FORZAR el cierre inmediato asumiendo el flotante actual
--      (bot_instances.force_close=true; el bot lo ejecuta y resetea).
--
-- account_settings es a nivel CUENTA (1 fila por usuario), no por bot_id:
-- la ganancia se mide contra el equity de la cuenta, que ambos motores
-- comparten. force_close en cambio es por instancia (cada motor cierra y
-- resetea lo suyo).
--
-- ESTADO: PENDIENTE de aplicar en Supabase (SQL Editor). Idempotente.
-- ==================================================================

BEGIN;

-- ------------------------------------------------------------------
-- 1. account_settings: preferencias a nivel cuenta (1 fila por usuario)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.account_settings (
    user_id           UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    -- Monto de fondeo declarado por el usuario. NULL = usar el
    -- bot_state.initial_balance auto-capturado de MT5 al primer arranque.
    initial_deposit   DOUBLE PRECISION,
    -- % de ganancia objetivo sobre la base (p.ej. 10 = +10%). NULL = sin objetivo.
    profit_target_pct DOUBLE PRECISION,
    -- Lo escribe el BOT al alcanzar el objetivo (claim atomico: dedup del
    -- email entre motores + banner en la UI). El usuario lo limpia al
    -- guardar un objetivo nuevo (re-armar).
    target_reached_at TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.account_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_settings_owner ON public.account_settings;
CREATE POLICY account_settings_owner ON public.account_settings
    FOR ALL TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- ------------------------------------------------------------------
-- 2. bot_instances.force_close: comando de cierre forzado por instancia.
--    Lo setea el USUARIO (dashboard, junto con is_active=false); el BOT
--    lo lee en el refresh de control (~3 s), cierra todas sus posiciones
--    y lo resetea a false.
-- ------------------------------------------------------------------
ALTER TABLE public.bot_instances
    ADD COLUMN IF NOT EXISTS force_close BOOLEAN NOT NULL DEFAULT false;

-- ------------------------------------------------------------------
-- 3. Realtime para que el dashboard vea target_reached_at sin polling.
-- ------------------------------------------------------------------
DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.account_settings;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_object THEN NULL;  -- proyecto sin publicacion realtime
END $$;

COMMIT;
