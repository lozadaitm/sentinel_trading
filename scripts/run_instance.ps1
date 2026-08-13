<#
.SYNOPSIS
    Arranca UN motor de UNA instancia, cargando su .env al entorno del proceso.

.DESCRIPTION
    Lee un archivo .env (KEY=VALUE por linea) y lanza el modulo de Python
    correspondiente. Las variables se setean SOLO en este proceso.

    Un .env = un USUARIO = una cuenta MT5. Los motores que corren sobre esa
    cuenta (m15, m5) NO necesitan .env propio: comparten credenciales y difieren
    unicamente en BOT_ID y MAGIC_NUMBER, que este script inyecta a partir de
    -BotId y de MAGIC_M15 / MAGIC_M5.

    Para levantar todos los motores de todos los usuarios de golpe, con
    preflight y auditorias, usa scripts\start_all.ps1.

.PARAMETER EnvFile
    Ruta al .env del usuario.

.PARAMETER BotId
    Motor a arrancar: m15 (Sentinel) o m5 (Grinder). Si se omite, se toma
    BOT_ID del .env, y si tampoco esta, m15.

.PARAMETER UI
    Arranca la TUI en vez del modo consola.

.PARAMETER Lite
    Modo ahorro para VPS: sin TUI, sin velas, sin log en consola. Solo un
    banner "Operando en <usuario>". El log completo sigue en CSV + Supabase.
    Tiene prioridad sobre -UI.

.PARAMETER Module
    Modulo de Python explicito. Anula la deduccion por BotId/UI/Lite.

.EXAMPLE
    scripts\run_instance.ps1 instances\daniel.env
    scripts\run_instance.ps1 instances\daniel.env -BotId m5 -UI
    scripts\run_instance.ps1 instances\daniel.env -Lite
    scripts\run_instance.ps1 instances\daniel.env -Module bot.tui
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$EnvFile,

    [ValidateSet("m15", "m5", "")]
    [string]$BotId = "",

    [switch]$UI,

    [switch]$Lite,

    [string]$Module = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $EnvFile)) {
    Write-Error "No existe el archivo de entorno: $EnvFile"
    exit 1
}

# Cargar KEY=VALUE (ignora comentarios y lineas vacias).
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#") -or ($line -notmatch "=")) { return }
    $idx = $line.IndexOf("=")
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
    Set-Item -Path "Env:$key" -Value $val
}

# --- Identidad del motor -------------------------------------------------
# Precedencia: -BotId > BOT_ID del .env > m15.
if ($BotId -eq "") {
    if ($env:BOT_ID) { $BotId = $env:BOT_ID } else { $BotId = "m15" }
}
$env:BOT_ID = $BotId

# El magic se deriva del motor. Solo se respeta un MAGIC_NUMBER del .env si el
# usuario NO pidio un motor concreto (compatibilidad con .env de una sola
# instancia); en cuanto se pasa -BotId, manda el mapa MAGIC_M15 / MAGIC_M5.
$magicMap = @{
    "m15" = $(if ($env:MAGIC_M15) { $env:MAGIC_M15 } else { "100100" })
    "m5"  = $(if ($env:MAGIC_M5)  { $env:MAGIC_M5 }  else { "100200" })
}
if ($PSBoundParameters.ContainsKey("BotId") -or -not $env:MAGIC_NUMBER) {
    $env:MAGIC_NUMBER = $magicMap[$BotId]
}

# --- Modulo --------------------------------------------------------------
if ($Module -eq "") {
    if ($env:MODULE) {
        $Module = $env:MODULE
    } elseif ($BotId -eq "m5") {
        if ($Lite) { $Module = "bot.lite_m5" } elseif ($UI) { $Module = "bot.tui_m5" } else { $Module = "bot.main_m5" }
    } else {
        if ($Lite) { $Module = "bot.lite" } elseif ($UI) { $Module = "bot.tui" } else { $Module = "bot.main" }
    }
}

$etiqueta = "Sentinel M15"
if ($BotId -eq "m5") { $etiqueta = "Grinder M5" }
Write-Host "Arrancando $etiqueta | USER_ID=$env:USER_ID SYMBOL=$env:SYMBOL BOT_ID=$env:BOT_ID MAGIC=$env:MAGIC_NUMBER (modulo: $Module)" -ForegroundColor Cyan

# Correr desde la raiz del proyecto (padre de scripts/).
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
python -m $Module
