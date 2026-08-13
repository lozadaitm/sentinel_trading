---
name: mt5-clone-ipc-profile-seed
description: Los clones MT5 dan IPC timeout (-10005) salvo que se les siembre el perfil de runtime maduro del base; portable+timeout no basta
metadata:
  type: project
---

Diagnóstico de 2026-08-13. `start_all.ps1` fallaba en FASE 3 con
`No se pudo inicializar MT5: (-10005, 'IPC timeout')` en `audit_margin` sobre los
clones nuevos.

**Causa raíz (no obvia):** un clon creado con `shutil.copytree(MT5_BASE_DIR, ...)`
copia los binarios + un `config` mínimo. Ese terminal **arranca y CONECTA al broker**
(la ventana muestra `login - servidor`), pero **su terminal nunca atiende el pipe IPC
de Python** (`\\.\pipe\MT5.Terminal.<hash>`), así que `mt5.initialize()` espera el
handshake y expira con -10005. El estado "listo para IPC" NO vive en la carpeta de
instalación sino en el **perfil de runtime maduro** que MT5 crea en
`%APPDATA%\MetaQuotes\Terminal\<md5(UPPER(ruta) UTF-16LE)>` cuando el terminal se
ejecuta interactivamente por primera vez. El clon copiado nunca lo tiene.

**Verificado por variable única** (mismo `terminal64.exe`, byte-idéntico, todos los
demás terminales cerrados): el único que atiende IPC es el **install de Program Files
con su perfil D0E8 maduro**. Portable/no-portable, `login=`, `timeout` alto y hasta un
INI de auto-login: todos dan timeout en un clon fresco. Sembrar el `config` del perfil
maduro (excluyendo `accounts.dat` y `common.ini`) hace conectar al clon en ~5-7 s, y
funciona **igual en portable**. No es credenciales (login OK en 1.2 s inyectado en el
terminal que sí conecta) ni modal (no hay diálogo; ventana principal visible).

**Corrección de un diagnóstico previo:** el commit d208257 creyó que faltaba el flag
`portable` y lo añadió. `portable` está bien y se queda, pero NO era la causa; lo que
faltaba era sembrar el perfil. `settings.ini` solo no basta; hace falta el conjunto del
perfil maduro.

**Fix (scripts/provision.py):** `_base_runtime_config()` localiza el `config` del perfil
del base (hash canónico, con fallback a `origin.txt`); `_seed_ipc_profile(clone_dir)`
lo copia al clon excluyendo `_ACCOUNT_LOCAL_FILES = {accounts.dat, common.ini}` para que
cada clon conserve su identidad y su botón de Algo Trading. Idempotente: si ya hay
`settings.ini` no pisa. Se llama en ambas ramas de `_clone_mt5` (clon nuevo y existente).

**Prerequisito operativo:** el terminal base (`MT5_BASE_DIR`) debe haberse abierto e
iniciado sesión al menos una vez para TENER perfil maduro que sembrar; si no, el
provisioner avisa y el clon daría IPC timeout. Relacionado: [[private-signup-provisioning]].
