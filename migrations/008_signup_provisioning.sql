-- ==================================================================
-- 008_signup_provisioning.sql  |  Signup privado + provision de instancias
-- ==================================================================
-- Contexto: el alta de usuarios deja de ser manual. El operador genera un
-- codigo de invitacion en /dashboard/admin y envia el link
-- (https://<dashboard>/signup?invite=CODIGO). El invitado se registra con
-- TODOS los datos rellenables de su instancia (label, credenciales MT5,
-- simbolo) y el webapp:
--   1. Reclama el codigo (claim atomico: un solo uso).
--   2. Crea el usuario en Supabase Auth.
--   3. Siembra bot_instances (m15+m5, is_active=false) y bot_config
--      (defaults) — igual que hacia el operador a mano.
--   4. Encola una fila en provision_requests con las credenciales MT5.
--
-- En el VPS, scripts/provision.py (service-role) procesa las PENDING:
-- clona la instalacion base de MT5 (modo portable), escribe el
-- instances/<user_id>.env y marca READY **borrando mt5_password de la DB**
-- (la credencial queda solo en el .env local del VPS).
--
-- SEGURIDAD: ambas tablas tienen RLS habilitada SIN politicas -> ningun
-- usuario autenticado puede leerlas ni escribirlas; solo el service-role
-- (server actions del webapp y provisioner del VPS).
--
-- ESTADO: PENDIENTE de aplicar en Supabase (SQL Editor). Idempotente.
-- ==================================================================

BEGIN;

-- ------------------------------------------------------------------
-- 1. invites: codigos de un solo uso para el signup privado.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.invites (
    code       TEXT PRIMARY KEY,           -- token aleatorio del link
    note       TEXT,                       -- para quien es (memoria del operador)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    used_at    TIMESTAMPTZ,                -- claim atomico (WHERE used_at IS NULL)
    used_by    UUID REFERENCES auth.users(id) ON DELETE SET NULL
);

ALTER TABLE public.invites ENABLE ROW LEVEL SECURITY;
-- Sin politicas: solo service-role.

-- ------------------------------------------------------------------
-- 2. provision_requests: cola de instancias por instalar en el VPS.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.provision_requests (
    id             BIGSERIAL PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    email          TEXT NOT NULL,
    label          TEXT,                       -- "Cuenta Vantage de Juan"
    symbol         TEXT NOT NULL DEFAULT 'XAUUSD+',
    bots           TEXT NOT NULL DEFAULT 'm15,m5',
    mt5_login      BIGINT NOT NULL,
    mt5_server     TEXT NOT NULL,
    mt5_password   TEXT,                       -- se BORRA al provisionar
    status         TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | READY | ERROR
    error          TEXT,
    provisioned_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_provision_requests_status
    ON public.provision_requests (status, created_at);

ALTER TABLE public.provision_requests ENABLE ROW LEVEL SECURITY;
-- Sin politicas: solo service-role.

COMMIT;
