---
name: withdrawals-commission-usdt
description: Retiros declarados + comision USDT BEP20 verificada on-chain; deuda pendiente bloquea la reactivacion
metadata:
  type: project
---

Feature de 2026-08-12 (migración `migrations/007_withdrawals.sql`): el usuario retira
fondos por su cuenta desde Vantage, pero **declara** el retiro en el dashboard
(`/dashboard/retiros`) para calcular la comisión del servicio. Modelo de confianza:
app solo para conocidos, pero con gates.

**Flujo:**
1. Declarar retiro → comisión = `amount × billing_settings.commission_pct` (default
   10%; lo fija el operador, el usuario NO puede editarlo) → fila en `withdrawals`
   (`PENDING`) → ambos motores a `is_active=false`.
2. Pago en **USDT red BEP20** a la wallet del operador
   (`0xc2f250a3a69be9893291b461188be96567e77ed7`, en `src/lib/billing.ts` del webapp).
3. **Verificación automática on-chain**: el usuario pega el tx hash; la server action
   (`src/lib/actions/withdrawals.ts::verifyPayment`) consulta un RPC público de BSC
   (`eth_getTransactionReceipt`): tx exitosa, ≥5 confirmaciones, log `Transfer` del
   contrato USDT (`0x55d398…7955`, 18 decimales) hacia la wallet, monto ≥ comisión
   (tolerancia $0.01) y `tx_hash` UNIQUE (un pago no salda dos comisiones). Solo
   entonces `status=PAID` (escrito con service-role).
4. **Gate doble**: el dashboard bloquea el toggle con deuda PENDING (banner + Realtime)
   y el bot revierte `is_active=true` → close-only en cada heartbeat
   (`db.has_pending_commission`, guard en `bot/main.py`). Alineado con la semántica
   close-only de [[supabase-multi-instance-model]].

**Seguridad clave:** `billing_settings` y `withdrawals` tienen RLS **solo SELECT** para
el dueño; todas las escrituras pasan por server actions con service-role. Si se les
diera UPDATE, el usuario podría ponerse comisión 0 o marcarse pagado.

**Admin** (`/dashboard/admin`, allowlist `ADMIN_EMAILS` — los dos operadores): totales
cobrados/pendientes en USDT y tabla de comisiones con email, montos, estado y link a
bscscan. El % por usuario se cambia editando `billing_settings` (service-role/SQL).

**Why:** monetización por comisión sobre retiros, cobrada en cripto, sin pasarela de
pago; la verificación on-chain evita depender de screenshots o confirmación manual.

**How to apply:** cualquier flujo nuevo que deba "cobrar antes de operar" debe crear una
fila `withdrawals PENDING` (o tabla análoga con RLS solo-SELECT) y dejar que los dos
gates existentes hagan el bloqueo; nunca confiar solo en el frontend.
