"""Notificaciones por correo (SMTP). Best-effort: nunca tumba el motor.

Config por entorno/.env (ver instances/example.env):
    SMTP_HOST, SMTP_PORT (587 STARTTLS | 465 SSL), SMTP_USER, SMTP_PASSWORD,
    SMTP_FROM (default: SMTP_USER).

Sin SMTP_HOST configurado el modulo queda inerte (solo loguea): el evento de
objetivo alcanzado sigue quedando en bot_logs y en account_settings aunque el
correo no salga.

El envio se hace en un hilo daemon: un SMTP lento (segundos) no debe congelar
el bucle de trading, que corre a 1 s por tick.
"""

import smtplib
import threading
from email.message import EmailMessage
from email.utils import formatdate

from . import config


def is_configured():
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def _send(to_addr, subject, body, logger=None):
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    try:
        port = int(config.SMTP_PORT)
        if port == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, port, timeout=20) as s:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, port, timeout=20) as s:
                s.starttls()
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        if logger:
            logger.write("SYSTEM", f"Email enviado a {to_addr}: {subject}")
    except Exception as e:  # noqa: BLE001  (best-effort; el evento ya quedo en DB)
        if logger:
            logger.write("ERROR", f"Fallo el envio de email a {to_addr}: {e}")


def send_async(to_addr, subject, body, logger=None):
    """Envia el correo en un hilo daemon. No-op (con log) si falta config o destinatario."""
    if not to_addr:
        if logger:
            logger.write("ERROR", "Email de objetivo NO enviado: sin destinatario (auth.users).")
        return
    if not is_configured():
        if logger:
            logger.write("SYSTEM", f"SMTP sin configurar: se omite el email a {to_addr} ({subject}).")
        return
    threading.Thread(
        target=_send, args=(to_addr, subject, body, logger),
        name="mail-sender", daemon=True,
    ).start()


def profit_target_body(*, profit_pct, target_pct, equity, base, symbol):
    return (
        f"Tu cuenta alcanzo el objetivo de ganancia configurado.\n"
        f"\n"
        f"  Ganancia actual : +{profit_pct:.2f}%  (objetivo: {target_pct:g}%)\n"
        f"  Equity          : {equity:,.2f}\n"
        f"  Saldo inicial   : {base:,.2f}\n"
        f"  Simbolo         : {symbol}\n"
        f"\n"
        f"Los bots pasaron a modo close-only: no abriran operaciones nuevas y\n"
        f"seguiran gestionando/cerrando las abiertas hasta quedar planos.\n"
        f"\n"
        f"En el dashboard puedes elegir entre esperar a que el bot termine de\n"
        f"gestionar las operaciones abiertas, o forzar el cierre inmediato\n"
        f"asumiendo el flotante actual.\n"
    )
