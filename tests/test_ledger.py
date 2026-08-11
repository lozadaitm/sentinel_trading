"""Regresion del CycleLedger (bot/ledger.py). No necesita MT5 ni Supabase.

Cubre los modos de fallo de la auditoria 6-11 ago (docs/memory/
unwind-orphans-hedge-count-state): hedges viudos re-etiquetados como OP1,
hedge-sobre-hedge, y el emparejamiento espurio de un hedge huerfano con la
ENTRY del ciclo siguiente tras un restart.

Correr:  python -m tests.test_ledger
"""

import os
import tempfile
from types import SimpleNamespace

from bot import ledger
from bot.ledger import (ROLE_ENTRY, ROLE_HEDGE, ROLE_ORPHAN, ROLE_RECOVERY,
                        ROLE_RESCUE, CycleLedger)

BUY, SELL = 0, 1


def pos(ticket, ptype, comment, t=0, volume=0.1):
    return SimpleNamespace(ticket=ticket, type=ptype, comment=comment,
                           time=t, volume=volume)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  OK  {msg}")


def test_comment_roles():
    print("[comment -> rol]")
    check(ledger.role_from_comment("SMC Buy Entry") == ROLE_ENTRY, "SMC = ENTRY")
    check(ledger.role_from_comment("Hedge Lock") == ROLE_HEDGE, "Hedge Lock = HEDGE")
    check(ledger.role_from_comment("Recovery Sell (V-Shape)") == ROLE_RECOVERY, "Recovery = RECOVERY")
    check(ledger.role_from_comment("Sentinel Op 4 (L3-CRITICO)") == ROLE_RESCUE, "Op 4 = RESCUE")
    check(ledger.role_from_comment("") == ROLE_ORPHAN, "sin comment = ORPHAN")
    check(ledger.role_from_comment("manual") == ROLE_ORPHAN, "comment ajeno = ORPHAN")


def test_register_and_pairs():
    print("[registro explicito + pares]")
    lg = CycleLedger(path=None)
    entry = pos(10, SELL, "SMC Sell Entry", t=1)
    hedge = pos(11, BUY, "Hedge Lock", t=2)
    lg.register(10, ROLE_ENTRY)
    lg.register(11, ROLE_HEDGE, covers=10)
    live = [entry, hedge]
    lg.sync(live)
    check(lg.entry_pos(live).ticket == 10, "entry_pos encuentra la ENTRY")
    check(lg.hedge_of(10, live).ticket == 11, "hedge_of encuentra su par")
    check(lg.pair_members(live) == {10, 11}, "pair_members = ambos tickets")
    check(len(lg.cycle_legs(live)) == 2, "ENTRY y su hedge son legs de ciclo")
    check(lg.naked_orphans(live) == [], "sin huerfanos")


def test_rehydration_by_comment():
    print("[rehidratacion por comment (restart sin archivo)]")
    lg = CycleLedger(path=None)
    entry = pos(20, BUY, "SMC Buy Entry", t=1)
    hedge = pos(21, SELL, "Hedge Lock", t=2)
    rec = pos(22, BUY, "Recovery Buy (V-Shape)", t=3)
    op4 = pos(23, BUY, "Sentinel Op 4 (L2)", t=4)
    live = [entry, hedge, rec, op4]
    roles = lg.sync(live)
    check(roles[20] == ROLE_ENTRY and roles[22] == ROLE_RECOVERY
          and roles[23] == ROLE_RESCUE, "roles adoptados por comment")
    check(roles[21] == ROLE_HEDGE and lg.hedge_of(20, live).ticket == 21,
          "hedge desconocido se empareja con la ENTRY viva")
    check(len(lg.cycle_legs(live)) == 4, "los 4 son legs del ciclo")


def test_widow_hedge_becomes_orphan():
    print("[hedge viudo -> ORPHAN (nunca OP1)]")
    lg = CycleLedger(path=None)
    entry = pos(30, SELL, "SMC Sell Entry", t=1)
    hedge = pos(31, BUY, "Hedge Lock", t=2)
    lg.sync([entry, hedge])
    # El Unwind banca la ENTRY verde: el hedge queda solo (episodio A, 6-ago).
    roles = lg.sync([hedge])
    # Viudo -> ORPHAN, y al no haber ENTRY se promueve (es un leg direccional
    # desnudo). Lo CRITICO: queda registrado como promocion, y su volumen/lado
    # ya no puede ser emparejado por inferencia con hedges futuros de otro par.
    check(roles[31] == ROLE_ENTRY, "huerfano solitario descubierto se promueve a ENTRY")
    check(lg.entry_pos([hedge]).ticket == 31, "el promovido es la ENTRY del ciclo")


def test_covered_orphan_pair_is_frozen_not_promoted():
    print("[par huerfano congelado: no se promueve ni cuenta profundidad]")
    lg = CycleLedger(path=None)
    orphan = pos(40, BUY, "Hedge Lock", t=1)
    cover = pos(41, SELL, "Hedge Lock", t=2)
    lg.register(40, ROLE_ORPHAN)
    lg.register(41, ROLE_HEDGE, covers=40)
    live = [orphan, cover]
    roles = lg.sync(live)
    check(roles[40] == ROLE_ORPHAN, "huerfano cubierto NO se promueve")
    check(lg.cycle_legs(live) == [], "el par huerfano no fabrica profundidad de ciclo")
    check(lg.pair_members(live) == {40, 41}, "el par huerfano es par intacto (intocable)")
    check(lg.naked_orphans(live) == [], "huerfano cubierto no dispara watchdog")


def test_persisted_orphan_not_paired_with_new_entry():
    print("[persistencia: hedge huerfano NO adopta la ENTRY del ciclo siguiente]")
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        # Sesion 1: hedge buy queda huerfano (su OP1 murio) y se persiste. Hay
        # ademas una recovery viva, asi que NO se promueve a ENTRY (hay ciclo).
        lg1 = CycleLedger(path=path)
        old_hedge = pos(50, BUY, "Hedge Lock", t=10)
        rec = pos(51, BUY, "Recovery Buy (V-Shape)", t=11)
        lg1.register(50, ROLE_HEDGE, covers=49)  # 49 ya no existe
        roles = lg1.sync([old_hedge, rec])
        check(roles[50] == ROLE_ORPHAN, "hedge viudo degradado a ORPHAN (hay ciclo vivo)")

        # Restart (sesion 2): la recovery cerro; abre ENTRY sell nueva. Sin
        # persistencia, el hedge buy (lado opuesto) se emparejaria con ella por
        # inferencia: el bug historico. Con el archivo, sigue siendo ORPHAN.
        lg2 = CycleLedger(path=path)
        new_entry = pos(60, SELL, "SMC Sell Entry", t=20)
        live = [old_hedge, new_entry]
        roles = lg2.sync(live)
        check(roles[50] == ROLE_ORPHAN, "tras restart sigue ORPHAN (no re-emparejado)")
        check(lg2.hedge_of(60, live) is None, "la ENTRY nueva NO tiene hedge espurio")
        check([p.ticket for p in lg2.naked_orphans(live)] == [50],
              "el huerfano descubierto es objetivo del watchdog")
        check([p.ticket for p in lg2.cycle_legs(live)] == [60],
              "profundidad de ciclo = 1 (solo la ENTRY)")
    finally:
        os.unlink(path)


def test_watchdog_cover_pairs_with_orphan():
    print("[cobertura del watchdog se empareja con su huerfano]")
    lg = CycleLedger(path=None)
    entry = pos(70, SELL, "SMC Sell Entry", t=1)
    orphan = pos(71, BUY, "Hedge Lock", t=0)  # de un ciclo anterior
    lg.register(70, ROLE_ENTRY)
    lg.register(71, ROLE_ORPHAN)
    lg.sync([entry, orphan])
    # El watchdog abre cobertura sobre el huerfano y la registra.
    cover = pos(72, SELL, "Hedge Lock", t=2)
    lg.register(72, ROLE_HEDGE, covers=71)
    live = [entry, orphan, cover]
    roles = lg.sync(live)
    check(roles[72] == ROLE_HEDGE, "la cobertura del watchdog es HEDGE")
    check(lg.naked_orphans(live) == [], "huerfano ya cubierto: watchdog en paz")
    check([p.ticket for p in lg.cycle_legs(live)] == [70],
          "ni el huerfano ni su cobertura cuentan como ciclo")
    check(lg.pair_members(live) == {71, 72}, "par huerfano-cobertura intacto")


def test_duplicate_entry_adopted_as_recovery():
    print("[segunda ENTRY viva se adopta como RECOVERY (anomalia)]")
    lg = CycleLedger(path=None)
    e1 = pos(80, BUY, "SMC Buy Entry", t=1)
    e2 = pos(81, BUY, "SMC Buy Entry", t=2)
    roles = lg.sync([e1, e2])
    check(roles[80] == ROLE_ENTRY and roles[81] == ROLE_RECOVERY,
          "la mas antigua es ENTRY; la duplicada, RECOVERY")


def main():
    test_comment_roles()
    test_register_and_pairs()
    test_rehydration_by_comment()
    test_widow_hedge_becomes_orphan()
    test_covered_orphan_pair_is_frozen_not_promoted()
    test_persisted_orphan_not_paired_with_new_entry()
    test_watchdog_cover_pairs_with_orphan()
    test_duplicate_entry_adopted_as_recovery()
    print("\ntest_ledger: TODO OK")


if __name__ == "__main__":
    main()
