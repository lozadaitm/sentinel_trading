---
name: private-signup-provisioning
description: Signup privado por invitacion + provisioner que clona MT5 portable y escribe el .env de la instancia
metadata:
  type: project
---

Feature de 2026-08-12 (migración `migrations/008_signup_provisioning.sql`): el alta de
usuarios ya no es manual.

**Flujo de alta:**
1. El operador genera un código de un solo uso en `/dashboard/admin` (sección
   Invitaciones) y envía el link `https://<dashboard>/signup?invite=CODIGO`.
2. El invitado se registra (`/signup`, ruta pública en `src/proxy.ts`) con TODOS los
   datos rellenables: label, email+password del panel, y de su cuenta MT5: login,
   servidor, **password de trading** (no inversor) y símbolo (default XAUUSD+).
3. La server action (`src/app/signup/actions.ts`, service-role): claim atómico del
   invite → `auth.admin.createUser` (email confirmado) → siembra `bot_instances`
   (m15+m5, `is_active=false`) y `bot_config` (defaults) → encola `provision_requests`
   → inicia sesión y entra al dashboard.
4. **Provisioner en el VPS** (`scripts/provision.py`, repo del bot):
   `python -m scripts.provision` (o `--watch` para poll cada 60 s, `--start` para
   lanzar los motores en consola). Por cada PENDING: clona la instalación base de MT5
   (`MT5_BASE_DIR`, default `C:\Program Files\MetaTrader 5`) a
   `MT5_CLONES_DIR\mt5_<login>` (default `C:\MT5_instances`), escribe
   `instances/<user_id>.env` (con `MT5_PORTABLE=true` → `mt5.initialize(portable=True)`,
   soporte añadido en `bot/config.py`/`bot/main.py`) y marca READY **borrando
   `mt5_password` de la DB** (la credencial queda solo en el .env del VPS). Si el .env
   ya existe escribe `<user_id>.env.new` para no pisar ajustes manuales.
5. El arranque sigue siendo `scripts\start_all.ps1` (preflight+tests+auditorías); las
   instancias nacen con `is_active=false` (no operan hasta que el usuario/operador
   enciende el toggle).

**Seguridad:** `invites` y `provision_requests` tienen RLS habilitada SIN políticas —
solo service-role las toca (ni el propio usuario puede leerlas). El claim del invite es
`UPDATE ... WHERE used_at IS NULL` (un solo uso). Si `createUser` falla, el invite se
des-reclama.

**Why:** poder mandar un link a conocidos y que la instancia quede lista sin tocar SQL
ni el VPS a mano (solo correr el provisioner, o dejarlo en `--watch`).

**How to apply:** cualquier dato nuevo que necesite una instancia debe añadirse al form
de `/signup`, a `provision_requests` (migración) y a la plantilla `ENV_TEMPLATE` del
provisioner — los tres puntos, o el .env saldrá incompleto.
