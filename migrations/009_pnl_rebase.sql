-- ==================================================================
-- 009_pnl_rebase.sql  |  Reinicio del P&L por ciclos de meta
-- ==================================================================
-- Contexto: el P&L funciona por CICLOS. Al alcanzar el objetivo de ganancia
-- los bots pasan a close-only; el usuario retira (o no) y reactiva. En esa
-- reactivacion la base del P&L se REINICIA al monto presente de la cuenta:
--   - si hubo retiro, la base nueva es lo que quedo tras el retiro;
--   - si no lo hubo, la base nueva es el monto actual (la ganancia del ciclo
--     anterior queda "consolidada" y el ciclo nuevo arranca de 0 hacia la
--     proxima meta).
-- Sin esto, re-armar el mismo % tras alcanzarlo re-dispararia el objetivo al
-- instante (la ganancia acumulada seguiria contando).
--
-- Mecanica:
--   1. El DASHBOARD estampa account_settings.baseline_reset_at (y limpia
--      target_reached_at) cuando el usuario reactiva un motor o re-arma la
--      meta DESPUES de un objetivo alcanzado.
--   2. Cada BOT (m15 y m5 por separado) ve el sello nuevo en su heartbeat y
--      lo aplica UNA vez: initial_balance = equity presente, adelanta el
--      ancla de flujos (last_flow_ticket) y graba el sello aplicado en
--      bot_state.baseline_applied_at (idempotencia entre heartbeats,
--      restarts y entre los dos motores).
--
-- ESTADO: PENDIENTE de aplicar en Supabase (SQL Editor). Idempotente.
-- ==================================================================

ALTER TABLE public.account_settings
    ADD COLUMN IF NOT EXISTS baseline_reset_at TIMESTAMPTZ;

ALTER TABLE public.bot_state
    ADD COLUMN IF NOT EXISTS baseline_applied_at TIMESTAMPTZ;
