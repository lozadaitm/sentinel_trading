<#
.SYNOPSIS
    Arranque completo: preflight -> tests -> auditorias -> lanza los bots con UI.

.DESCRIPTION
    Encadena en el orden correcto todo lo que hay que hacer para levantar el
    sistema, y ABORTA antes de operar si algo no cuadra. El objetivo es que no
    se pueda arrancar en un estado incoherente por saltarse un paso.

    Fases:
      1. Preflight   - python, dependencias, .env completos, BOT_ID vs MAGIC.
      2. Tests       - regresion local (no toca MT5 ni Supabase).
      3. Auditorias  - audit_margin (siempre) y audit_m5_signals (si toca).
      4. Lanzamiento - una ventana de PowerShell por bot, con la TUI.

    Las auditorias son READ-ONLY y su salida se archiva con fecha en
    logs/audits/, para tener historico de como estaba la cuenta en cada arranque.

    audit_m5_signals tarda (recorre ~20.000 velas), asi que solo se ejecuta si
    la ultima es mas vieja que -AuditMaxAgeDays. audit_margin son 2 segundos y
    corre siempre: es el que dice si el peor caso de la escalera cabe en el
    equity de HOY.

.PARAMETER InstancesDir
    Carpeta con los .env. Por defecto instances\ del repo.

.PARAMETER AuditMaxAgeDays
    Antiguedad maxima de audit_m5_signals antes de repetirla. Por defecto 30.

.PARAMETER SkipAudits
    Salta la fase 3 entera. Para reinicios rapidos del mismo dia.

.PARAMETER ForceAudits
    Ejecuta audit_m5_signals aunque la ultima sea reciente.

.PARAMETER SkipTests
    Salta la regresion. No recomendado tras un git pull.

.PARAMETER NoUI
    Lanza bot.main / bot.main_m5 (consola) en vez de las TUI.

.PARAMETER M15Only
    Levanta solo el Sentinel. Util mientras el M5 sigue en validacion.

.PARAMETER DryRun
    Hace preflight, tests y auditorias, pero NO lanza los bots.

.EXAMPLE
    scripts\start_all.ps1
    scripts\start_all.ps1 -M15Only -SkipAudits
    scripts\start_all.ps1 -DryRun -ForceAudits
#>
param(
    [string]$InstancesDir = "",
    [int]$AuditMaxAgeDays = 30,
    [switch]$SkipAudits,
    [switch]$ForceAudits,
    [switch]$SkipTests,
    [switch]$NoUI,
    [switch]$M15Only,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
if ($InstancesDir -eq "") { $InstancesDir = Join-Path $projectRoot "instances" }
$auditDir = Join-Path $projectRoot "logs\audits"
$runner = Join-Path $PSScriptRoot "run_instance.ps1"

function Write-Phase($n, $text) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
    Write-Host ("  FASE $n - $text") -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
}
function Write-Ok($t)   { Write-Host "  [OK]    $t" -ForegroundColor Green }
function Write-Warn($t) { Write-Host "  [AVISO] $t" -ForegroundColor Yellow }
function Write-Fail($t) { Write-Host "  [FALLO] $t" -ForegroundColor Red }

function Read-EnvFile($path) {
    $map = @{}
    Get-Content $path | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq "" -or $line.StartsWith("#") -or ($line -notmatch "=")) { return }
        $i = $line.IndexOf("=")
        $map[$line.Substring(0, $i).Trim()] = $line.Substring($i + 1).Trim().Trim('"').Trim("'")
    }
    return $map
}

# Aplica un hashtable de variables al proceso actual y devuelve los valores
# previos, para poder restaurarlos despues (las auditorias necesitan el entorno
# de la instancia, pero no queremos contaminar el resto del script).
function Push-Env($map) {
    $prev = @{}
    foreach ($k in $map.Keys) {
        $prev[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
        Set-Item -Path "Env:$k" -Value $map[$k]
    }
    return $prev
}
function Pop-Env($prev) {
    foreach ($k in $prev.Keys) {
        if ($null -eq $prev[$k]) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }
        else { Set-Item -Path "Env:$k" -Value $prev[$k] }
    }
}

Set-Location $projectRoot
Write-Host ""
Write-Host "  SENTINEL - arranque completo" -ForegroundColor White
Write-Host "  repo: $projectRoot"

# ==================================================================
# FASE 1 - PREFLIGHT
# ==================================================================
Write-Phase 1 "PREFLIGHT"

$py = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $py) { Write-Fail "python no esta en el PATH."; exit 1 }
Write-Ok "python: $((python --version) 2>$null)"

# find_spec en vez de import: no ejecuta los modulos y no escupe traceback.
$faltan = python -c "import importlib.util as u; print(','.join(m for m in ('MetaTrader5','pandas','numpy','supabase','rich','plotext') if u.find_spec(m) is None))"
if ($LASTEXITCODE -ne 0) { Write-Fail "no se pudo comprobar las dependencias."; exit 1 }
if ($faltan -ne "") {
    Write-Fail "faltan dependencias: $faltan"
    Write-Host "         python -m pip install -r requirements.txt" -ForegroundColor DarkGray
    exit 1
}
Write-Ok "dependencias presentes (incl. rich/plotext para la TUI)"

if (-not (Test-Path $InstancesDir)) { Write-Fail "no existe $InstancesDir"; exit 1 }

$envFiles = Get-ChildItem -Path $InstancesDir -Filter "*.env" |
    Where-Object { $_.Name -notlike "example*" }
if (-not $envFiles) {
    Write-Fail "no hay instancias en $InstancesDir (solo plantillas example*.env)."
    Write-Host "         Copia instances\example.env y instances\example-m5.env." -ForegroundColor DarkGray
    exit 1
}

# Clasifica por BOT_ID y valida cada .env.
$instances = @()
$hardFail = $false
$requiredKeys = @("USER_ID", "SYMBOL", "MAGIC_NUMBER", "BOT_ID",
                  "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")

foreach ($f in $envFiles) {
    $cfg = Read-EnvFile $f.FullName
    $missing = @()
    foreach ($k in $requiredKeys) {
        if (-not $cfg.ContainsKey($k) -or $cfg[$k] -eq "") { $missing += $k }
    }
    if ($missing.Count -gt 0) {
        Write-Fail "$($f.Name): faltan claves -> $($missing -join ', ')"
        $hardFail = $true
        continue
    }

    $botId = $cfg["BOT_ID"]
    $magic = [int]$cfg["MAGIC_NUMBER"]

    # Misma comprobacion que config.validate_identity(), adelantada al arranque
    # para no descubrirla cuando ya se abrio la ventana.
    $expected = $null
    if ($botId -eq "m15") {
        if ($cfg.ContainsKey("MAGIC_M15")) { $expected = [int]$cfg["MAGIC_M15"] } else { $expected = 100100 }
    } elseif ($botId -eq "m5") {
        if ($cfg.ContainsKey("MAGIC_M5")) { $expected = [int]$cfg["MAGIC_M5"] } else { $expected = 100200 }
    }
    if ($null -ne $expected -and $magic -ne $expected) {
        Write-Fail "$($f.Name): BOT_ID=$botId pero MAGIC_NUMBER=$magic (esperado $expected)."
        Write-Host "         Los dos motores compartirian magic y se verian las posiciones mutuamente." -ForegroundColor DarkGray
        $hardFail = $true
        continue
    }

    if ($cfg.ContainsKey("MAGIC_M15") -and $cfg.ContainsKey("MAGIC_M5")) {
        if ([int]$cfg["MAGIC_M15"] -eq [int]$cfg["MAGIC_M5"]) {
            Write-Fail "$($f.Name): MAGIC_M15 y MAGIC_M5 son el mismo numero."
            $hardFail = $true
            continue
        }
    }

    $module = "bot.main"
    if ($cfg.ContainsKey("MODULE")) { $module = $cfg["MODULE"] }
    if (-not $NoUI) {
        if ($module -eq "bot.main")    { $module = "bot.tui" }
        if ($module -eq "bot.main_m5") { $module = "bot.tui_m5" }
    }

    $shadow = "no"
    if ($cfg.ContainsKey("SHADOW_MODE") -and $cfg["SHADOW_MODE"] -match "^(?i:true|1)$") { $shadow = "SI" }

    $instances += [PSCustomObject]@{
        Name = $f.BaseName; Path = $f.FullName; Cfg = $cfg
        BotId = $botId; Magic = $magic; Module = $module
        Symbol = $cfg["SYMBOL"]; Shadow = $shadow
    }
    Write-Ok "$($f.Name): bot_id=$botId magic=$magic symbol=$($cfg['SYMBOL']) modulo=$module sombra=$shadow"
}

if ($hardFail) { Write-Host ""; Write-Fail "preflight fallido. No se arranca nada."; exit 1 }

$m15 = @($instances | Where-Object { $_.BotId -eq "m15" })
$m5  = @($instances | Where-Object { $_.BotId -eq "m5" })
if ($m15.Count -eq 0) { Write-Fail "no hay ninguna instancia con BOT_ID=m15."; exit 1 }
if ($M15Only) { $m5 = @() }

# Dos instancias del MISMO usuario deben apuntar al mismo simbolo y cuenta.
foreach ($a in $m15) {
    $pareja = $m5 | Where-Object { $_.Cfg["USER_ID"] -eq $a.Cfg["USER_ID"] }
    foreach ($b in $pareja) {
        if ($b.Symbol -ne $a.Symbol) {
            Write-Warn "$($a.Name) y $($b.Name) comparten USER_ID pero distinto SYMBOL ($($a.Symbol) vs $($b.Symbol))."
        }
        if ($a.Cfg["MT5_LOGIN"] -ne $b.Cfg["MT5_LOGIN"]) {
            Write-Warn "$($a.Name) y $($b.Name) comparten USER_ID pero distinto MT5_LOGIN. El gobierno de margen asume UNA cuenta."
        }
    }
}

# ==================================================================
# FASE 2 - TESTS
# ==================================================================
if ($SkipTests) {
    Write-Phase 2 "TESTS (saltados por -SkipTests)"
} else {
    Write-Phase 2 "TESTS DE REGRESION"
    python -m tests.test_m5_and_budget | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "la regresion ha fallado. Revisa la salida completa con: python -m tests.test_m5_and_budget"
        exit 1
    }
    Write-Ok "regresion en verde"
}

# ==================================================================
# FASE 3 - AUDITORIAS
# ==================================================================
if ($SkipAudits) {
    Write-Phase 3 "AUDITORIAS (saltadas por -SkipAudits)"
} else {
    Write-Phase 3 "AUDITORIAS (read-only, no envian ordenes)"
    if (-not (Test-Path $auditDir)) { New-Item -ItemType Directory -Force -Path $auditDir | Out-Null }
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"

    foreach ($inst in $m15) {
        Write-Host ""
        Write-Host "  --- cuenta: $($inst.Name) ---" -ForegroundColor White
        $prev = Push-Env $inst.Cfg
        try {
            # --- audit_margin: siempre. Es el que valida los supuestos de HOY. ---
            $outMargin = Join-Path $auditDir "margin_$($inst.Name)_$stamp.txt"
            python -m scripts.audit_margin | Tee-Object -FilePath $outMargin | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Fail "audit_margin fallo. Sin MT5 accesible no tiene sentido arrancar."
                Write-Host "         Revisa MT5_PATH / MT5_LOGIN / que el terminal este abierto." -ForegroundColor DarkGray
                Pop-Env $prev
                exit 1
            }
            Write-Ok "audit_margin -> $(Split-Path -Leaf $outMargin)"

            $texto = Get-Content $outMargin -Raw
            foreach ($linea in (Select-String -Path $outMargin -Pattern "margin_hedged\s+:|stop out|margin call|apalancamiento").Line) {
                Write-Host "         $($linea.Trim())" -ForegroundColor DarkGray
            }
            if ($texto -match "NO CABE") {
                Write-Warn "el peor caso de la escalera NO CABE en el equity actual."
                Write-Host "         Baja max_entry_lots / max_recovery_lots del M15 antes de operar." -ForegroundColor DarkGray
                Write-Host "         Detalle en $outMargin" -ForegroundColor DarkGray
                $resp = Read-Host "         Continuar de todos modos? (s/N)"
                if ($resp -notmatch "^(?i:s|si|y|yes)$") { Pop-Env $prev; exit 1 }
            } elseif ($texto -match "REQUIERE cuenta hedging") {
                Write-Fail "la cuenta NO es hedging. El Sentinel necesita Op1 + Hedge Lock simultaneos."
                Pop-Env $prev
                exit 1
            }

            # --- audit_m5_signals: solo si toca. Recorre ~20.000 velas. ---
            if ($m5.Count -gt 0) {
                $ultima = Get-ChildItem -Path $auditDir -Filter "m5signals_$($inst.Name)_*.txt" -ErrorAction SilentlyContinue |
                          Sort-Object LastWriteTime -Descending | Select-Object -First 1
                $toca = $true
                if ($null -ne $ultima -and -not $ForceAudits) {
                    $dias = [int]((Get-Date) - $ultima.LastWriteTime).TotalDays
                    if ($dias -lt $AuditMaxAgeDays) {
                        $toca = $false
                        Write-Ok "audit_m5_signals: al dia (ultima hace $dias dias, umbral $AuditMaxAgeDays). Usa -ForceAudits para repetirla."
                    }
                }
                if ($toca) {
                    Write-Host "         audit_m5_signals: recorriendo el historico, esto tarda..." -ForegroundColor DarkGray
                    $outSig = Join-Path $auditDir "m5signals_$($inst.Name)_$stamp.txt"
                    python -m scripts.audit_m5_signals | Tee-Object -FilePath $outSig | Out-Null
                    if ($LASTEXITCODE -ne 0) {
                        Write-Warn "audit_m5_signals fallo. No bloquea el arranque; calibra m5_max_band_atr a mano."
                    } else {
                        Write-Ok "audit_m5_signals -> $(Split-Path -Leaf $outSig)"
                        foreach ($linea in (Select-String -Path $outSig -Pattern "=>|media/mes").Line) {
                            Write-Host "         $($linea.Trim())" -ForegroundColor DarkGray
                        }
                    }
                }
            }
        } finally {
            Pop-Env $prev
        }
    }
}

# ==================================================================
# FASE 4 - LANZAMIENTO
# ==================================================================
Write-Phase 4 "LANZAMIENTO"

$aLanzar = @($m15) + @($m5)
if ($DryRun) {
    Write-Warn "-DryRun: no se lanza nada. Se habrian abierto:"
    foreach ($i in $aLanzar) { Write-Host "         $($i.Name)  ->  $($i.Module)" }
    Write-Host ""
    Write-Ok "preflight, tests y auditorias completados."
    exit 0
}

# El Sentinel primero: es el senior del gobierno de margen y conviene que su
# cesta ya este publicada cuando el M5 calcule la reserva de proteccion.
foreach ($i in $aLanzar) {
    $etiqueta = "Sentinel M15"
    if ($i.BotId -eq "m5") { $etiqueta = "Grinder M5" }
    Write-Host "  Lanzando $etiqueta ($($i.Name)) -> $($i.Module)" -ForegroundColor Green
    Start-Process powershell -ArgumentList @(
        "-NoExit", "-ExecutionPolicy", "Bypass",
        "-File", "`"$runner`"", "`"$($i.Path)`"", "-Module", $i.Module
    )
    Start-Sleep -Milliseconds 1500
}

Write-Host ""
Write-Host ("=" * 72) -ForegroundColor DarkCyan
Write-Ok "$($aLanzar.Count) bot(s) lanzado(s). Cada uno en su ventana."
if ($m5.Count -eq 0) {
    Write-Host "         Solo M15. Para levantar tambien el M5, crea instances\<usuario>-m5.env" -ForegroundColor DarkGray
}
Write-Host "         Cerrar una ventana (Ctrl+C) detiene esa instancia limpiamente." -ForegroundColor DarkGray
Write-Host "         Auditorias archivadas en logs\audits\" -ForegroundColor DarkGray
Write-Host ""
