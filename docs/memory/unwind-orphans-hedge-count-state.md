---
name: unwind-orphans-hedge-count-state
description: Auditoría reporte MT5 6-11 ago 2026 — el Unwind banca la OP1 verde y deja el hedge huérfano; la máquina por conteo lo re-etiqueta como OP1 y abre hedge-sobre-hedge
metadata:
  type: project
---

Auditoría del historial MT5 (cuenta 25556247, reset a $5,000 el 2026-08-06 00:31 hora
servidor → reporte del 2026-08-11 02:50). Confirmada la sospecha del usuario y algo peor:
la máquina de estados por CONTEO (`core_count = len(positions)`) pierde la identidad de
las operaciones en cuanto el Unwind rompe el par OP1↔OP2.

**Patrón observado 3 veces (episodios A/B/C):**

1. OP1 + Hedge Lock correctos. El precio revierte: OP1 queda verde, hedge rojo.
2. `_profit_banking` (gate = `cycle_realized + basket_net > 0`, sin noción de rol) banca
   la **OP1 verde** → el hedge queda **huérfano en rojo** (A: 06-ago 16:09:23, OP1s +176.50;
   B: 07-ago 16:26:43, OP1b +332.88 dejando HEDGE-s + 3×OP4 + REC — el estado exacto que
   el usuario sospechaba: op2 con op3/op4 y sin poder abrir cobertura nueva).
3. Con count==1 la máquina trata el hedge huérfano como "OP1" y le abre un **Hedge Lock
   encima** (HEDGE+b + HEDGE-s simultáneos, 06-ago 16:09:30); con count>=2 abre
   Recovery/OP4 sobre esa base falsa → churn de 111 aperturas de OP4 "L3-CRITICO" y
   181 cierres "Unwind Profit Banking" en 5 días.
4. El Healer termina realizando el rojo del huérfano con el presupuesto del churn
   (-322.80 y -612.46 en A; -1,332.63 el 11-ago 02:10:31 amputando el remanente 0.17 de
   la OP1s del episodio C, pérdida total de esa OP1: -1,460.51).

**Loop de scalping del hedge (episodio C, 10-ago):** con `cycle_realized` inflado por los
verdes del churn, el freeze del Unwind queda abierto aunque la OP1 flote en rojo profundo
→ banca el hedge verde y el ESTADO 1 lo reabre al tick siguiente (12+ ciclos de +$30-45).
Al final del reporte quedó un **Hedge Lock buy 0.17 @4402 huérfano abierto** que la
máquina tratará como OP1.

Neto desde el reset: M15 +$6,181 (el trend salvó el churn), M5 -$3. El resultado positivo
enmascara la incoherencia estructural.

**Why:** la identidad por conteo no distingue quién es quién; el Unwind y el Healer
operan sin rol y rompen pares. `_closing_increases_exposure` no basta: con RECs abiertas,
cerrar la OP1 verde no aumenta |neto| y pasa el filtro, pero huérfana al hedge igual.

**How to apply:** IMPLEMENTADO como **CycleLedger** (`bot/ledger.py`, persistido en
`instances/cycle_ledger_<BOT_ID>_<USER_ID>.json`, regresión `python -m tests.test_ledger`):

- Roles ENTRY/HEDGE/RECOVERY/RESCUE/ORPHAN por ticket, par explícito (`covers`);
  rehidratación por comment de MT5 tras restart. La persistencia evita que un hedge
  huérfano se re-empareje por inferencia con la ENTRY del ciclo siguiente.
- Hedge monitor corre a CUALQUIER conteo (antes solo count==1); watchdog cubre
  huérfanos rojos > hedge_dist; `_resize_hedge` encoge el hedge al remanente de la ENTRY.
- Unwind y Healer excluyen miembros de par intacto (el par solo se desarma completo);
  la máquina de estados usa `cycle_legs` (sin huérfanos) como profundidad.
- Huérfano solitario se promueve a ENTRY solo cuando NO queda ningún leg de ciclo vivo.
- El backtest usa ledger en memoria (no toca el JSON de la instancia viva).

Ver [[bot-design-constraints]] y [[healer-unwind-hedge-cascade]] (esta es la variante
espejo: allí el Healer desarmaba, aquí el Unwind huérfana).
