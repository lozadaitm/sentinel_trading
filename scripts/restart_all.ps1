<#
.SYNOPSIS
    Reinicio completo: tumba bots y terminales MT5 y relanza start_all.ps1.

.DESCRIPTION
    Lo dispara scripts/provision.py (via la tarea programada SentinelRestart,
    que corre INTERACTIVA en la sesion del operador) cuando se provisiona una
    instancia nueva: asi la instancia recien creada se levanta junto con las
    existentes sin intervencion manual.

    Pasos:
      1. Mata los procesos de bot (python corriendo bot.main/bot.tui/bot.*_m5)
         y sus ventanas host (powershell con run_instance.ps1).
      2. Mata los terminal64.exe (cada bot relanza el suyo via mt5.initialize).
      3. Relanza scripts\start_all.ps1 -SkipAudits (sin prompts; los tests de
         regresion SI corren: si fallan, mejor no arrancar nada y verlo en el
         log que arrancar codigo roto).

    Todo queda en logs\restart_all.log.

.PARAMETER DryRun
    Solo loguea que procesos mataria y que lanzaria, sin tocar nada.

.PARAMETER KeepTerminals
    No mata los terminal64.exe (util si hay terminales manuales abiertos).

.PARAMETER Lite
    Relanza en modo ahorro (start_all.ps1 -Lite): sin TUI ni log en consola.
    Para que el reinicio automatico use lite, añade -Lite a los argumentos de
    la tarea programada SentinelRestart.
#>
param(
    [switch]$DryRun,
    [switch]$KeepTerminals,
    [switch]$Lite
)

$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $projectRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$log = Join-Path $logDir "restart_all.log"

function Write-Log($t) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $t"
    Write-Host $line
    Add-Content -Path $log -Value $line
}

$modo = if ($DryRun) { " (DRY RUN)" } else { "" }
Write-Log "=== Reinicio solicitado$modo (provision de instancia nueva) ==="

# --- 1. Bots: python con modulos bot.* + ventanas host de run_instance ---
$procs = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match '^python' -and $_.CommandLine -match 'bot\.(main|tui|lite)|scripts\.news_exporter') -or
    ($_.Name -match '^powershell' -and $_.CommandLine -match 'run_instance\.ps1')
}
if (-not $procs) { Write-Log "sin procesos de bot corriendo" }
foreach ($p in $procs) {
    Write-Log "matar PID $($p.ProcessId) [$($p.Name)]: $($p.CommandLine -replace '\s+', ' ')"
    if (-not $DryRun) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}

# --- 2. Terminales MT5 ---
if ($KeepTerminals) {
    Write-Log "terminales MT5 conservados (-KeepTerminals)"
} else {
    $terms = Get-Process terminal64 -ErrorAction SilentlyContinue
    if (-not $terms) { Write-Log "sin terminal64.exe corriendo" }
    foreach ($t in $terms) {
        Write-Log "matar terminal64 PID $($t.Id)"
        if (-not $DryRun) { Stop-Process -Id $t.Id -Force -ErrorAction SilentlyContinue }
    }
}

if ($DryRun) {
    Write-Log "=== DRY RUN: no se lanzo start_all ==="
    exit 0
}

Start-Sleep -Seconds 5

# --- 3. Relanzar todo ---
$startArgs = @("-SkipAudits")
if ($Lite) { $startArgs += "-Lite" }
Write-Log "lanzando start_all.ps1 $($startArgs -join ' ') ..."
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_all.ps1") @startArgs *>> $log
Write-Log "=== start_all terminado (exit $LASTEXITCODE) ==="
