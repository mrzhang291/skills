$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    python -m compileall -q .
    python -m pytest -q tests
    if ($env:ARCHON_BASE_DIR) {
        python ci\run_ci.py --base-dir $env:ARCHON_BASE_DIR --dept $env:ARCHON_DEPARTMENT --dept-password $env:ARCHON_DEPT_PASSWORD
    }
} finally {
    Pop-Location
}
