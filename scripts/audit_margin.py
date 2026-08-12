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

import math
import os
import sys

import MetaTrader5 as mt5

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config  # noqa: E402


def _init():
    # kwargs centralizados (bot/config.py): incluyen `portable` y `timeout`.
    # Construirlos a mano aqui lanzaba los clones portables en modo normal
    # (perfil nuevo + asistente de primera ejecucion => IPC timeout).
    if not mt5.initialize(**config.mt5_init_kwargs()):
        raise SystemExit(f"No se pudo inicializar MT5: {mt5.last_error()}")
    if not mt5.symbol_select(config.SYMBOL, True):
        print(f"[ALERTA] no se pudo seleccionar {config.SYMBOL}")


def _load_bot_config():
    """Fila real de bot_config para este usuario/bot, con fallback a DEFAULTS.

    Devuelve (dict, descripcion_del_origen). Si Supabase no responde se usan los
    DEFAULTS de strategy.py, que es exactamente lo que el bot usaria en ese caso.
    """
    from bot.strategy import DEFAULTS
    claves = ("equity_weight", "base_risk", "base_lots", "max_entry_lots",
              "max_recovery_lots", "max_net_lots", "max_rescue_legs")
    valores = {k: DEFAULTS[k] for k in claves}
    try:
        from bot.db import Database
        db = Database()
        db.connect()
        fila, _ = db.load_config()
        db.close()
        if fila:
            faltan = []
            for k in claves:
                if fila.get(k) is None:
                    faltan.append(k)
                else:
                    valores[k] = fila[k]
            origen = f"bot_config (user={config.USER_ID[:8]}..., bot_id={config.BOT_ID})"
            if faltan:
                origen += f"; sin columna -> DEFAULTS: {', '.join(faltan)}"
            return valores, origen
        return valores, "DEFAULTS de strategy.py (no hay fila activa en bot_config)"
    except Exception as e:  # noqa: BLE001
        return valores, f"DEFAULTS de strategy.py (Supabase no respondio: {e})"


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

    # Config REAL de bot_config. Sin ella el calculo seria hipotetico: los caps
    # (max_entry_lots, max_recovery_lots) son TOPES, no tamaños -- el lote sale
    # de _calculate_lots() y escala con el equity, asi que con cuentas pequeñas
    # queda muy por debajo del cap y evaluar el cap infla el resultado.
    cfg, origen = _load_bot_config()
    print(f"  parametros: {origen}")
    for k in ("equity_weight", "base_risk", "base_lots", "max_entry_lots",
              "max_recovery_lots", "max_net_lots", "max_rescue_legs"):
        print(f"     {k:<20} = {cfg[k]}")
    print()

    step = sym.volume_step if sym.volume_step else 0.01

    def _lots_op1(equity):
        """Replica bot/strategy.py::_calculate_lots(is_recovery=False)."""
        capital = equity * float(cfg["equity_weight"])
        base_risk = float(cfg["base_risk"])
        ratio = (capital / base_risk) if base_risk else 0.0
        lots = ratio * float(cfg["base_lots"])
        lots = math.floor(lots / step) * step
        lots = max(lots, sym.volume_min)
        lots = min(lots, sym.volume_max, float(cfg["max_entry_lots"]))
        return lots

    def _escalera(equity):
        """Bruto acumulado de la cesta, respetando el tope de exposicion neta.

        Op1 + Hedge (mismo volumen, sentido opuesto -> neto 0) + Op3 + rescates.
        _net_cap_ok corta los adds en cuanto |neto| superaria max_net_lots, asi
        que los rescates dejan de entrar mucho antes de agotar max_rescue_legs.
        """
        op1 = _lots_op1(equity)
        op3 = min(op1 * 2.0, float(cfg["max_recovery_lots"]))
        cap_neto = float(cfg["max_net_lots"])

        bruto = op1 + op1          # Op1 + Hedge Lock
        neto = 0.0                 # el hedge lo deja plano
        for leg in [op3] + [op3] * int(cfg["max_rescue_legs"]):
            nuevo_neto = neto + leg
            if cap_neto > 0 and abs(nuevo_neto) > cap_neto and abs(nuevo_neto) > abs(neto):
                break              # _net_cap_ok bloquea este add
            neto = nuevo_neto
            bruto += leg
        return op1, bruto

    def _margen(lotes):
        m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, config.SYMBOL, lotes, tick.ask)
        return float(m) if m is not None else 0.0

    # --- A. Situacion de HOY: es la que decide si se puede operar ---
    op1_hoy, bruto_hoy = _escalera(acc.equity)
    m_hoy = _margen(bruto_hoy)
    pct_hoy = (m_hoy / acc.equity * 100.0) if acc.equity else 0.0
    if m_hoy < acc.equity * 0.6:   veredicto = "CABE"
    elif m_hoy < acc.equity:       veredicto = "JUSTO"
    else:                          veredicto = "NO CABE"

    print(f"  A) HOY, con equity {acc.equity:.2f}")
    print(f"     Op1 = {op1_hoy:.2f} lotes   (el cap es {cfg['max_entry_lots']}, no se alcanza salvo con equity alto)")
    print(f"     escalera completa = {bruto_hoy:.2f} lotes brutos")
    print(f"     margen = {m_hoy:.2f} ({pct_hoy:.1f}% del equity)")
    print(f"     VEREDICTO: {veredicto}")
    if sym.margin_hedged == 0:
        print("     (margin_hedged=0: el par Op1+Hedge no suma margen, el coste real es MENOR)")

    # --- B. Techo teorico: cuando el equity crece hasta saturar los caps ---
    eq_saturado = float(cfg["base_risk"]) * float(cfg["max_entry_lots"]) / (
        float(cfg["base_lots"]) * float(cfg["equity_weight"]))
    _, bruto_max = _escalera(eq_saturado)
    m_max = _margen(bruto_max)
    print()
    print(f"  B) Techo teorico: los caps se saturan a partir de ~{eq_saturado:.0f} de equity")
    print(f"     escalera completa = {bruto_max:.2f} lotes brutos -> margen {m_max:.2f}")
    print(f"     (necesitarias {m_max / 0.6:.0f} de equity para que siguiera siendo holgado)")

    print()
    if veredicto == "NO CABE":
        print("  ACCION REQUERIDA: baja max_entry_lots / max_recovery_lots del M15")
        print("  ANTES de operar. El presupuesto del M5 no arregla esto.")
    else:
        print("  Sin accion. La escalera de hoy entra con holgura en el equity.")

    _h("SIGUIENTE PASO")
    print("  1. Anotar margin_hedged, leverage, margin_so_call y margin_so_so.")
    print("  2. Consultar la fila real de bot_config:")
    print("       SELECT max_net_lots, max_entry_lots, max_recovery_lots, max_rescue_legs")
    print(f"         FROM bot_config WHERE user_id='{config.USER_ID}' AND bot_id='m15';")
    print("  3. Calibrar ml_no_add / ml_flatten contra el stop out real de arriba.")

    mt5.shutdown()


if __name__ == "__main__":
    main()
