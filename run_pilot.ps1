# LP reward-farming LIVE launcher (bare-metal alternative to docker compose).
# Loads creds from .env, sets a tiny/hard-stopped config, runs farm.py --loop.
# Usage (from the repo root):  .\run_pilot.ps1
$ErrorActionPreference = "Stop"

# Paths default to the repo layout; override with env vars if yours differs.
$EnvFile = if ($env:LP_ENV_FILE) { $env:LP_ENV_FILE } else { Join-Path $PSScriptRoot ".env" }
$FarmDir = Join-Path $PSScriptRoot "lp"
$VenvPy  = if ($env:LP_PYTHON) { $env:LP_PYTHON } else { "python" }

if (-not (Test-Path $EnvFile)) { Write-Error "env file not found: $EnvFile (copy .env.example to .env)"; exit 1 }

# --- load .env: KEY=VALUE, skip comments/blanks, strip surrounding quotes ---
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

# --- verify creds are present (never print the key) ---
if (-not $env:POLY_PRIVATE_KEY)    { Write-Error "POLY_PRIVATE_KEY missing from .env"; exit 1 }
if (-not $env:POLY_FUNDER_ADDRESS) { Write-Error "POLY_FUNDER_ADDRESS missing from .env"; exit 1 }
Write-Host ("creds loaded: POLY_PRIVATE_KEY=set  funder={0}  sigType={1}" -f `
    $env:POLY_FUNDER_ADDRESS, $env:POLY_SIGNATURE_TYPE)

# --- config: tiny, hard-stopped, fat-pool only, dollar-capped ---
$env:LP_AUTOEXEC       = "1"     # LIVE (places orders); set 0 for a dry-run
$env:LP_MARKETS        = ""      # dynamic selection (long-dated, non-toxic)
$env:LP_K_CENTS        = "1.0"   # quote 1c off mid
$env:LP_MAX_INV_USD    = "75"    # dollar inventory cap per market
$env:LP_MIN_POOL       = "40"    # fat-pool floor
$env:LP_MAX_COMP       = "800"   # skip over-crowded books
$env:LP_DEFAULT_SIZE   = "50"    # shares / side
$env:LP_DAILY_STOP_USD = "15"    # hard daily loss stop

Set-Location $FarmDir
Write-Host "LAUNCHING farm.py --loop  [LIVE]  size=$env:LP_DEFAULT_SIZE/side  cap=$env:LP_MAX_INV_USD  dailyStop=$env:LP_DAILY_STOP_USD"
Write-Host "Ctrl+C to stop.  To cancel ALL resting orders: run .\kill_pilot.ps1 in another window."
& $VenvPy farm.py --loop
