"""Provisioner de instancias: convierte provision_requests PENDING en instancias reales.

El signup privado del dashboard (webapp: /signup?invite=CODIGO) crea el usuario en
Supabase Auth, siembra bot_instances/bot_config y encola una fila en
provision_requests con las credenciales MT5 que rellena el invitado. Este script
(corre en el VPS, service-role) hace el resto:

  1. Clona la instalacion BASE de MetaTrader 5 a una carpeta propia del usuario
     (modo portable: los datos viven dentro del clon, no en AppData).
  2. Escribe instances/<user_id>.env con todos los valores (Supabase, identidad,
     credenciales MT5, MT5_PORTABLE=true).
  3. Marca la request como READY y BORRA mt5_password de la DB (la credencial
     queda solo en el .env local del VPS).

El arranque de los bots sigue siendo scripts\\start_all.ps1 (preflight + tests +
auditorias): este script NO lanza motores por defecto; con --start lanza los de
las instancias recien provisionadas en modo consola (sin preflight, para probar).

Uso (desde la raiz del repo, con SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY en el
entorno, en el .env de la raiz, o presentes en algun instances/*.env):

    python -m scripts.provision            # procesa las PENDING y termina
    python -m scripts.provision --watch    # queda vigilando (poll cada 60 s)
    python -m scripts.provision --start    # ademas lanza los bots provisionados

Config por entorno:
    MT5_BASE_DIR   instalacion base a clonar (default: C:\\Program Files\\MetaTrader 5)
    MT5_CLONES_DIR carpeta raiz de los clones (default: C:\\MT5_instances)
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCES_DIR = os.path.join(ROOT, "instances")

MT5_BASE_DIR = os.environ.get("MT5_BASE_DIR", r"C:\Program Files\MetaTrader 5")
MT5_CLONES_DIR = os.environ.get("MT5_CLONES_DIR", r"C:\MT5_instances")

# Tarea programada que tumba bots+terminales y relanza start_all tras
# provisionar algo nuevo. Corre INTERACTIVA en la sesion del operador (las
# TUIs y terminales necesitan ventana; este script corre como SYSTEM).
# Vacia ("PROVISION_RESTART_TASK=") = no reiniciar nada automaticamente.
RESTART_TASK = os.environ.get("PROVISION_RESTART_TASK", "SentinelRestart")

ENV_TEMPLATE = """# Usuario: {label} / {email}
# Generado por scripts/provision.py el {stamp} (request #{req_id}).
# Una instancia = un usuario = una cuenta = un terminal MT5 (clon portable).

# --- Supabase ---
SUPABASE_URL={supabase_url}
SUPABASE_SERVICE_ROLE_KEY={supabase_key}

# --- Identidad de ESTE usuario ---
USER_ID={user_id}
SYMBOL={symbol}

# --- Motores a levantar sobre esta cuenta ---
BOTS={bots}
MAGIC_M15=100100
MAGIC_M5=100200

# --- Terminal MetaTrader 5 (clon portable propio de este usuario) ---
MT5_PATH={mt5_path}
MT5_LOGIN={mt5_login}
MT5_SERVER={mt5_server}
MT5_PASSWORD={mt5_password}
MT5_PORTABLE=true
"""


def _read_env_file(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _find_supabase_creds():
    """SUPABASE_URL/KEY del entorno, del .env raiz, o de algun instances/*.env."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if url and key:
        return url, key
    candidates = [os.path.join(ROOT, ".env")]
    if os.path.isdir(INSTANCES_DIR):
        for name in sorted(os.listdir(INSTANCES_DIR)):
            if name.endswith(".env") and not name.startswith(("example", "template", "_")):
                candidates.append(os.path.join(INSTANCES_DIR, name))
    for path in candidates:
        vals = _read_env_file(path)
        if vals.get("SUPABASE_URL") and vals.get("SUPABASE_SERVICE_ROLE_KEY"):
            return vals["SUPABASE_URL"], vals["SUPABASE_SERVICE_ROLE_KEY"]
    return "", ""


def _enable_algo_trading(clone_dir):
    """Deja el Algo Trading (AutoTrading) habilitado desde el primer arranque.

    En modo portable el terminal lee su configuracion de <clon>\\config\\
    common.ini. Sin el boton activo, mt5.order_send() devuelve retcode 10027
    (client disables autotrading) y el bot puede leer pero no operar. Se
    escribe la seccion [Experts] ANTES del primer arranque; si el archivo ya
    existe (clon ya arrancado alguna vez) no se pisa: en ese caso el estado
    del boton ya es el que dejo el operador.
    """
    cfg_dir = os.path.join(clone_dir, "config")
    ini_path = os.path.join(cfg_dir, "common.ini")
    if os.path.exists(ini_path):
        return
    os.makedirs(cfg_dir, exist_ok=True)
    with open(ini_path, "w", encoding="utf-16") as f:  # MT5 usa UTF-16 en sus .ini
        f.write("[Experts]\nAllowLiveTrading=1\nAllowDllImport=0\nEnabled=1\n")
    print("    Algo Trading habilitado (config/common.ini del clon).")


def _clone_mt5(login):
    """Copia la instalacion base a un clon propio del login. Idempotente."""
    clone_dir = os.path.join(MT5_CLONES_DIR, f"mt5_{login}")
    exe = os.path.join(clone_dir, "terminal64.exe")
    if os.path.exists(exe):
        print(f"    clon MT5 ya existe: {clone_dir}")
        _enable_algo_trading(clone_dir)
        return exe
    base_exe = os.path.join(MT5_BASE_DIR, "terminal64.exe")
    if not os.path.exists(base_exe):
        raise RuntimeError(
            f"No existe la instalacion base de MT5 en {MT5_BASE_DIR} "
            f"(define MT5_BASE_DIR).")
    print(f"    clonando {MT5_BASE_DIR} -> {clone_dir} (puede tardar)...")
    os.makedirs(MT5_CLONES_DIR, exist_ok=True)
    shutil.copytree(MT5_BASE_DIR, clone_dir)
    _enable_algo_trading(clone_dir)
    return exe


def _write_instance_env(req, mt5_path, supabase_url, supabase_key):
    path = os.path.join(INSTANCES_DIR, f"{req['user_id']}.env")
    if os.path.exists(path):
        # No pisar un .env existente (podria tener ajustes manuales): se
        # escribe una copia .new para que el operador la revise.
        path = path + ".new"
    content = ENV_TEMPLATE.format(
        label=req.get("label") or "sin etiqueta",
        email=req.get("email", ""),
        stamp=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        req_id=req["id"],
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        user_id=req["user_id"],
        symbol=req.get("symbol") or "XAUUSD+",
        bots=req.get("bots") or "m15,m5",
        mt5_path=mt5_path,
        mt5_login=req["mt5_login"],
        mt5_server=req["mt5_server"],
        mt5_password=req.get("mt5_password") or "",
    )
    os.makedirs(INSTANCES_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _trigger_restart():
    """Dispara el reinicio completo (scripts/restart_all.ps1) via la tarea
    programada SentinelRestart: mata bots y terminales y relanza start_all
    en la sesion del operador, ya con la(s) instancia(s) nueva(s) incluidas.
    """
    if not RESTART_TASK:
        print("PROVISION_RESTART_TASK vacio: sin reinicio automatico.")
        return
    try:
        subprocess.run(["schtasks", "/Run", "/TN", RESTART_TASK],
                       check=True, capture_output=True, text=True)
        print(f"Reinicio completo disparado (tarea '{RESTART_TASK}').")
    except Exception as e:  # noqa: BLE001
        print(f"No se pudo disparar el reinicio ('{RESTART_TASK}'): {e}. "
              f"Lanza scripts\\restart_all.ps1 (o start_all.ps1) a mano.")


def _start_instance(env_path, bots):
    """Lanza los motores de la instancia en ventanas nuevas (modo consola)."""
    runner = os.path.join(ROOT, "scripts", "run_instance.ps1")
    for bot_id in bots:
        module = "bot.main_m5" if bot_id == "m5" else "bot.main"
        subprocess.Popen(
            ["powershell", "-NoExit", "-ExecutionPolicy", "Bypass",
             "-File", runner, env_path, "-BotId", bot_id, "-Module", module],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        time.sleep(1.5)


def process_pending(client, *, start=False):
    res = (client.table("provision_requests").select("*")
           .eq("status", "PENDING").order("created_at").execute())
    requests = res.data or []
    if not requests:
        print("Sin provision_requests PENDING.")
        return 0

    supabase_url, supabase_key = _find_supabase_creds()
    done = 0
    for req in requests:
        rid = req["id"]
        print(f"[{rid}] {req.get('email')} login={req.get('mt5_login')} "
              f"server={req.get('mt5_server')} bots={req.get('bots')}")
        try:
            mt5_path = _clone_mt5(req["mt5_login"])
            env_path = _write_instance_env(req, mt5_path, supabase_url, supabase_key)
            print(f"    .env escrito: {env_path}")
            client.table("provision_requests").update({
                "status": "READY",
                "error": None,
                "mt5_password": None,  # la credencial queda SOLO en el .env del VPS
                "provisioned_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }).eq("id", rid).execute()
            done += 1
            print("    READY (mt5_password borrada de la DB).")
            if start:
                bots = [b.strip() for b in (req.get("bots") or "m15").split(",") if b.strip()]
                _start_instance(env_path, bots)
                print(f"    motores lanzados: {bots}")
        except Exception as e:  # noqa: BLE001  (una request mala no frena la cola)
            print(f"    ERROR: {e}")
            try:
                client.table("provision_requests").update(
                    {"status": "ERROR", "error": str(e)[:500]}
                ).eq("id", rid).execute()
            except Exception:  # noqa: BLE001
                pass
    return done


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", action="store_true",
                        help="vigila la cola (poll cada 60 s) en vez de salir")
    parser.add_argument("--start", action="store_true",
                        help="lanza los bots de cada instancia provisionada (modo consola)")
    args = parser.parse_args()

    url, key = _find_supabase_creds()
    if not url or not key:
        print("Faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (entorno, .env raiz "
              "o algun instances/*.env).")
        sys.exit(1)

    from supabase import create_client  # import tardio: mismo venv que el bot
    client = create_client(url, key)

    if args.watch:
        print("Vigilando provision_requests (Ctrl+C para salir)...")
        while True:
            try:
                done = process_pending(client, start=args.start)
                if done and not args.start:
                    _trigger_restart()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"Error consultando la cola: {e}")
            time.sleep(60)
    else:
        done = process_pending(client, start=args.start)
        if done and not args.start:
            _trigger_restart()


if __name__ == "__main__":
    main()
