<#
.SYNOPSIS
    Levanta TODAS las instancias definidas en instances\*.env, cada una en su
    propia ventana de PowerShell (proceso independiente).

.DESCRIPTION
    Ignora instances\example.env (plantilla). Cada .env real arranca un proceso
    `bot.main` via run_instance.ps1. Cerrar una ventana detiene esa instancia
    (Ctrl+C -> shutdown limpio).

.EXAMPLE
    scripts\launch_all.ps1
#>
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
    Start-Process powershell -ArgumentList @(
        "-NoExit", "-ExecutionPolicy", "Bypass",
        "-File", "`"$runner`"", "`"$($f.FullName)`""
    )
}

Write-Host "Lanzadas $($envFiles.Count) instancia(s)." -ForegroundColor Cyan
