---
name: workflow-implement-then-commit
description: Para cambios del bot el usuario espera implementar+commit directo; no meter backtest-validation sin que lo pida
metadata: 
  node_type: memory
  type: feedback
  originSessionId: a536f284-c744-4403-bd66-dd99dd3b52ca
---

Cuando el usuario aprueba un cambio de estrategia (ej. Fork A), espera el flujo: implementar → commit → push en la rama actual. NO insertar una fase de evaluación por backtesting a menos que lo pida explícitamente; lo confunde y lo siente como desvío del objetivo ("No entendimos absolutamente, estás intentando evaluar... a través de backtesting?").

**Why:** los defaults se deciden por criterio de estrategia acordado, no por el arnés. Además el arnés `bot_backtesting` decide **una vez por cierre de vela M15**, así que NO puede reproducir eventos intra-vela (la cascada real de 7 legs en 3 min del 2026-06-17 21:00) — el VCB y cualquier protección de spike son **no testeables** ahí. Correr ablaciones largas no representa el escenario real.

**How to apply:** tras acordar el diseño, editar + compilar (`ast.parse`) + commit + push. Ofrecer backtest solo como opción explícita, no ejecutarlo por default. Relacionado: [[bot-design-constraints]].
