<#
.SYNOPSIS
    Levanta TODAS las instancias definidas en instances\*.env, cada una en su
    propia ventana de PowerShell (proceso independiente).

.DESCRIPTION
    Ignora instances\example.env (plantilla). Cada .env real arranca un proceso
    `bot.main` via run_instance.ps1. Cerrar una ventana detiene esa instancia
    (Ctrl+C -> shutdown limpio).

.PARAMETER Lite
    Modo ahorro para VPS: lanza bot.lite (sin render ni log en consola; solo
    "Operando en <usuario>"). Ver scripts\run_instance.ps1 -Lite.

.EXAMPLE
    scripts\launch_all.ps1
    scripts\launch_all.ps1 -Lite
#>
param(
    [switch]$Lite
)
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_instance.ps1"
$instancesDir = Join-Path $projectRoot "instances"

$envFiles = Get-ChildItem -Path $instancesDir -Filter "*.env" |
    Where-Object { $_.Name -ne "example.env" }

if (-not $envFiles) {
    Write-Warning "No hay instancias en $instancesDir (solo example.env). Crea instances\<usuario>.env."
    exit 0
}

foreach ($f in $envFiles) {
    Write-Host "Lanzando instancia: $($f.Name)" -ForegroundColor Green
    $procArgs = @(
        "-NoExit", "-ExecutionPolicy", "Bypass",
        "-File", "`"$runner`"", "`"$($f.FullName)`""
    )
    if ($Lite) { $procArgs += "-Lite" }
    Start-Process powershell -ArgumentList $procArgs
}

Write-Host "Lanzadas $($envFiles.Count) instancia(s)." -ForegroundColor Cyan
