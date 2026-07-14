<#
.SYNOPSIS
    Arranca UNA instancia del bot cargando su archivo .env al entorno del proceso.

.DESCRIPTION
    Lee un archivo .env (KEY=VALUE por linea), setea esas variables SOLO en este
    proceso y lanza `python -m bot.main`. Cada instancia = un usuario/cuenta.

.EXAMPLE
    scripts\run_instance.ps1 instances\usuarioA.env
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$EnvFile
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

Write-Host "Arrancando instancia: USER_ID=$env:USER_ID SYMBOL=$env:SYMBOL" -ForegroundColor Cyan

# Correr desde la raiz del proyecto (padre de scripts/).
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
python -m bot.main
