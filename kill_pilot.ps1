# Kill switch — cancel ALL resting orders on the farm account.
# Usage:  .\kill_pilot.ps1          cancel EVERY open order on the account
#         .\kill_pilot.ps1 -Ours    cancel only orders farm.py recorded (surgical)
param([switch]$Ours)
$ErrorActionPreference = "Stop"

$EnvFile = if ($env:LP_ENV_FILE) { $env:LP_ENV_FILE } else { Join-Path $PSScriptRoot ".env" }
$FarmDir = Join-Path $PSScriptRoot "lp"
$VenvPy  = if ($env:LP_PYTHON) { $env:LP_PYTHON } else { "python" }

if (-not (Test-Path $EnvFile)) { Write-Error "env file not found: $EnvFile"; exit 1 }

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $name = $line.Substring(0, $idx).Trim()
    $val  = $line.Substring($idx + 1).Trim()
    if ($val.Length -ge 2 -and
        (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'")))) {
        $val = $val.Substring(1, $val.Length - 2)
    }
    Set-Item -Path "Env:$name" -Value $val
}
if (-not $env:POLY_PRIVATE_KEY) { Write-Error "POLY_PRIVATE_KEY missing from .env"; exit 1 }

Set-Location $FarmDir
if ($Ours) { & $VenvPy kill.py --ours } else { & $VenvPy kill.py }
