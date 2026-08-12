-- ==================================================================
-- 007_withdrawals.sql  |  Retiros declarados + comision USDT (BEP20)
-- ==================================================================
-- Contexto: el usuario retira fondos por su cuenta desde Vantage, pero debe
-- DECLARAR el retiro en el dashboard. Al declararlo:
--   1. Se calcula la comision = amount * commission_pct (el % vive en
--      billing_settings, por usuario; default 10%).
--   2. Ambos motores pasan a is_active=false (close-only).
--   3. La reactivacion queda BLOQUEADA hasta pagar la comision en USDT
--      (BEP20) a la wallet del operador. El pago se verifica ON-CHAIN de
--      forma automatica: el usuario pega el tx hash y el servidor comprueba
--      contra un RPC publico de BSC que la transferencia USDT llego a la
--      wallet con el monto suficiente (ver webapp: lib/actions/withdrawals).
--   4. El BOT tambien vigila: si detecta is_active=true con una comision
--      PENDING, se auto-apaga (bot/main.py). Buena fe + cinturon.
--
-- SEGURIDAD (por que las RLS son solo SELECT):
--   - El usuario NO puede editar su commission_pct ni marcar un retiro como
--     pagado: cualquier escritura pasa por server actions con service-role
--     tras verificar la sesion (y el pago, on-chain).
--   - tx_hash UNIQUE: un mismo pago no puede saldar dos comisiones.
--
-- ESTADO: PENDIENTE de aplicar en Supabase (SQL Editor). Idempotente.
-- ==================================================================

BEGIN;

-- ------------------------------------------------------------------
-- 1. billing_settings: % de comision por usuario (lo fija el operador).
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.billing_settings (
    user_id        UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    commission_pct DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.billing_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS billing_settings_owner_select ON public.billing_settings;
CREATE POLICY billing_settings_owner_select ON public.billing_settings
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());
-- Sin politicas de INSERT/UPDATE para authenticated: escribe solo el
-- service-role (server actions / operador).

-- ------------------------------------------------------------------
-- 2. withdrawals: retiros declarados y estado de su comision.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.withdrawals (
    id             BIGSERIAL PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    amount         DOUBLE PRECISION NOT NULL,  -- monto declarado del retiro (USD)
    commission_pct DOUBLE PRECISION NOT NULL,  -- % aplicado al registrar
    commission_usd DOUBLE PRECISION NOT NULL,  -- a pagar en USDT (BEP20)
    status         TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | PAID
    tx_hash        TEXT UNIQUE,                -- hash BEP20 verificado on-chain
    paid_amount    DOUBLE PRECISION,           -- USDT que llegaron a la wallet
    paid_at        TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_withdrawals_user_created
    ON public.withdrawals (user_id, created_at DESC);

ALTER TABLE public.withdrawals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS withdrawals_owner_select ON public.withdrawals;
CREATE POLICY withdrawals_owner_select ON public.withdrawals
    FOR SELECT TO authenticated
    USING (user_id = auth.uid());
-- Sin INSERT/UPDATE para authenticated (ver cabecera).

-- ------------------------------------------------------------------
-- 3. Realtime: el dashboard refleja el pago verificado sin recargar.
-- ------------------------------------------------------------------
DO $$ BEGIN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.withdrawals;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN undefined_object THEN NULL;
END $$;

COMMIT;
