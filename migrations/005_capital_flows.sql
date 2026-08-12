-- ==================================================================
-- 005_capital_flows.sql  |  Base del objetivo ajustada por depositos/retiros
-- ==================================================================
-- Contexto: la base del objetivo de ganancia es bot_state.initial_balance
-- (auto-capturado de MT5 en la primera corrida). Un deposito o retiro
-- posterior distorsionaba el % de ganancia: la ganancia medida debe ser SOLO
-- la del trading, es decir  equity - (inicial + flujos externos netos).
--
-- El bot detecta los deals de deposito/retiro/credito de la cuenta
-- (DEAL_TYPE_BALANCE / DEAL_TYPE_CREDIT) y los incorpora a la base:
--   deposito +X -> initial_balance += X   (la ganancia no salta)
--   retiro   -X -> initial_balance -= X   (la ganancia no se hunde)
--
-- last_flow_ticket es el ANCLA de reconciliacion: el ticket del ultimo deal
-- de flujo ya incorporado. Se ancla por ticket (creciente) y no por fecha
-- porque los deals de MT5 vienen en hora del servidor y anclar por reloj
-- local arriesga saltarse o duplicar una ventana de horas.
-- Ver bot/broker.py::capital_flows y bot/main.py::_reconcile_capital_flows.
--
-- ESTADO: PENDIENTE de aplicar en Supabase (SQL Editor). Idempotente.
-- ==================================================================

ALTER TABLE public.bot_state
    ADD COLUMN IF NOT EXISTS last_flow_ticket BIGINT;
