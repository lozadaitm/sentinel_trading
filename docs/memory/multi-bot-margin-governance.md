---
name: multi-bot-margin-governance
description: M15 y M5 conviven en una cuenta; el M15 es senior y su Hedge Lock es inbloqueable, el M5 cede el margen
metadata:
  type: project
---

Un usuario corre DOS motores contra la MISMA cuenta MT5: `bot_id='m15'`
(Sentinel, magic 100100) y `bot_id='m5'` (Grinder SmartCut, magic 100200). Los
magics separan la lógica; el margen es compartido y lo arbitra `bot/budget.py`.

**Why:** el M15 no lleva SL catastrófico por diseño (ver
[[bot-design-constraints]]), así que su Hedge Lock es la única red que congela
la pérdida. El M5 lleva SL y sus scalps son desechables. La asimetría de
criticidad es lo que decide el reparto, no un porcentaje de equity.

**How to apply:**
- Orden protectora (`KIND_PROTECTIVE`) = nunca se pre-filtra por margen ni por
  tope. Cualquier cap que pueda impedir un hedge es un bug.
- El M5 (`ROLE_JUNIOR`) descuenta de su margen disponible la protectora
  pendiente del M15, leyendo sus posiciones de MT5 (`positions_of_magic`). La
  coordinación NO pasa por Supabase: sin race de red y sobrevive si un proceso
  muere.
- Escalera por margin level: el M5 deja de abrir y luego se liquida solo; el
  M15 solo deja de abrir aditivas. Calibrar contra `margin_so_call` /
  `margin_so_so` reales con `scripts/audit_margin.py`.
- Todo lo dimensiona `equity_weight` (M15 0.80 / M5 0.20): con dos motores,
  escalar ambos contra el equity completo es doble conteo.

Plan completo y estado: `docs/plan-m5-m15-coexistencia.md`.
Regresión: `python -m tests.test_m5_and_budget` (no necesita MT5 ni Supabase).

**Decisión pendiente de confirmar:** apagar `use_grinder` en la fila m15
(`migrations/003_disable_m15_grinder.sql`) seca el presupuesto del Healer,
porque sus amputaciones se financiaban con los verdes del grinder que
compartían magic. Es de-risking deliberado dado
[[healer-unwind-hedge-cascade]], pero no se ha aplicado.
