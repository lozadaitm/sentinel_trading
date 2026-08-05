"""Fase 0.1: auditoria de margen de la cuenta. READ-ONLY, no envia ordenes.

Produce los numeros que calibran el gobierno de margen (bot/budget.py) y el
dimensionado de la convivencia M15 + M5. Ver docs/plan-m5-m15-coexistencia.md.

Lo critico que responde:
  - margin_hedged: si es 0, el par Op1+Hedge del Sentinel no consume margen
    adicional y el presupuesto es holgado. Si cobra completo, el lock es lo
    mas caro del sistema y hay que bajar los caps del M15.
  - Peor caso de la escalera del M15 en lotes brutos y en dinero, contra el
    equity real. Si no cabe, el M15 no puede completar su propia escalera y el
    presupuesto del M5 es irrelevante.

Uso:  scripts\\run_instance.ps1 instances\\<usuario>.env -Module scripts.audit_margin
  o:  python -m scripts.audit_margin      (usa el terminal MT5 por defecto)
"""

import os
import sys

import MetaTrader5 as mt5

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config  # noqa: E402


def _init():
    kw = {}
    if config.MT5_PATH:
        kw["path"] = config.MT5_PATH
    if config.MT5_LOGIN:
        kw["login"] = int(config.MT5_LOGIN)
        kw["server"] = config.MT5_SERVER
        kw["password"] = config.MT5_PASSWORD
    if not mt5.initialize(**kw):
        raise SystemExit(f"No se pudo inicializar MT5: {mt5.last_error()}")
    if not mt5.symbol_select(config.SYMBOL, True):
        print(f"[ALERTA] no se pudo seleccionar {config.SYMBOL}")


def _h(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main():
    _init()
    sym = mt5.symbol_info(config.SYMBOL)
    acc = mt5.account_info()
    tick = mt5.symbol_info_tick(config.SYMBOL)
    if sym is None or acc is None or tick is None:
        raise SystemExit("MT5 no devolvio symbol_info / account_info / tick.")

    _h(f"CUENTA  ({acc.company} / {acc.server})")
    print(f"  login             : {acc.login}")
    print(f"  balance           : {acc.balance:.2f} {acc.currency}")
    print(f"  equity            : {acc.equity:.2f}")
    print(f"  margen usado      : {acc.margin:.2f}")
    print(f"  margen libre      : {acc.margin_free:.2f}")
    print(f"  margin level      : {acc.margin_level:.1f} %" if acc.margin else
          "  margin level      : (sin posiciones)")
    print(f"  apalancamiento    : 1:{acc.leverage}")
    print(f"  margin call       : {acc.margin_so_call:.1f} %   <- calibra ml_no_add")
    print(f"  stop out          : {acc.margin_so_so:.1f} %   <- calibra ml_flatten")
    print(f"  modo de cuenta    : {'HEDGING' if acc.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING else 'NETTING/EXCHANGE'}")
    if acc.margin_mode != mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
        print("  *** AVISO: el Sentinel REQUIERE cuenta hedging (Op1 + Hedge Lock). ***")

    _h(f"SIMBOLO  {config.SYMBOL}")
    print(f"  digits / point    : {sym.digits} / {sym.point}")
    print(f"  contract size     : {sym.trade_contract_size}")
    print(f"  spread actual     : {sym.spread} puntos")
    print(f"  stops level       : {sym.trade_stops_level} puntos   <- lo respeta el trailing M5")
    print(f"  freeze level      : {sym.trade_freeze_level} puntos")
    print(f"  volumen min/max/step: {sym.volume_min} / {sym.volume_max} / {sym.volume_step}")
    print(f"  bid / ask         : {tick.bid:.{sym.digits}f} / {tick.ask:.{sym.digits}f}")
    print(f"  margin_hedged     : {sym.margin_hedged}")
    print(f"  margin_hedged_use_leg: {bool(getattr(sym, 'margin_hedged_use_leg', False))}")
    if sym.margin_hedged == 0:
        print("  => Par cubierto SIN coste de margen adicional. Presupuesto holgado.")
    else:
        print("  => El par cubierto SI consume margen. El Hedge Lock es caro:")
        print("     bajar max_entry_lots/max_recovery_lots del M15 antes de arrancar el M5.")

    _h("COSTE DE MARGEN POR LOTE")
    print(f"  {'lotes':>8} | {'margen BUY':>12} | {'% del equity':>13}")
    print("  " + "-" * 40)
    for lots in (0.01, 0.05, 0.10, 0.50, 1.00, 2.00, 3.00):
        m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, config.SYMBOL, lots, tick.ask)
        if m is None:
            print(f"  {lots:>8.2f} | {'n/d':>12} |")
            continue
        pct = (m / acc.equity * 100.0) if acc.equity else 0.0
        print(f"  {lots:>8.2f} | {m:>12.2f} | {pct:>12.1f}%")

    _h("POSICIONES VIVAS POR MAGIC")
    pos = mt5.positions_get(symbol=config.SYMBOL) or []
    by_magic = {}
    for p in pos:
        by_magic.setdefault(p.magic, []).append(p)
    if not by_magic:
        print("  (ninguna)")
    for magic, ps in sorted(by_magic.items()):
        etiqueta = {config.MAGIC_M15: "M15 Sentinel", config.MAGIC_M5: "M5 Grinder"}.get(magic, "otro")
        net = sum(p.volume if p.type == mt5.POSITION_TYPE_BUY else -p.volume for p in ps)
        gross = sum(p.volume for p in ps)
        pl = sum(p.profit + p.swap for p in ps)
        print(f"  magic {magic} ({etiqueta}): {len(ps)} pos | bruto {gross:.2f} | "
              f"neto {net:+.2f} | P&L {pl:+.2f}")

    _h("PEOR CASO DE LA ESCALERA DEL M15")
    print("  Verificar contra la fila REAL de bot_config (los DEFAULTS de")
    print("  strategy.py son solo fallback). Si max_net_lots > 0, _net_cap_ok")
    print("  estrangula la escalera muy por debajo de los caps de lote.")
    print()
    escenarios = [
        ("max_net_lots = 1.0 (tope activo)", 1.0 + 1.0 + 1.0),
        ("max_net_lots = 0 (tope DESACTIVADO)", 1.0 + 1.0 + 2.0 + 3 * 2.0),
    ]
    for nombre, bruto in escenarios:
        m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, config.SYMBOL, bruto, tick.ask)
        if m is None:
            continue
        pct = (m / acc.equity * 100.0) if acc.equity else 0.0
        veredicto = "CABE" if m < acc.equity * 0.6 else ("JUSTO" if m < acc.equity else "NO CABE")
        print(f"  {nombre}")
        print(f"     bruto {bruto:.2f} lotes -> margen {m:.2f} ({pct:.0f}% del equity)  [{veredicto}]")
        if sym.margin_hedged == 0:
            print("     (con margin_hedged=0 el coste real sera MENOR: los pares cubiertos no suman)")
    print()
    print("  Si el escenario vigente sale 'NO CABE', bajar los caps del M15")
    print("  ANTES de arrancar el M5: el presupuesto del M5 no arregla eso.")

    _h("SIGUIENTE PASO")
    print("  1. Anotar margin_hedged, leverage, margin_so_call y margin_so_so.")
    print("  2. Consultar la fila real de bot_config:")
    print("       SELECT max_net_lots, max_entry_lots, max_recovery_lots, max_rescue_legs")
    print(f"         FROM bot_config WHERE user_id='{config.USER_ID}' AND bot_id='m15';")
    print("  3. Calibrar ml_no_add / ml_flatten contra el stop out real de arriba.")

    mt5.shutdown()


if __name__ == "__main__":
    main()
