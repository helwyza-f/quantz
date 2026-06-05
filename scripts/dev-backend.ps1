$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

$env:PYTHONPATH = "src"
$env:QUANTZ_ROOT = "$repoRoot"

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
  & $venvPython -m uvicorn quantz.api.app:app --host 127.0.0.1 --port 8787 --reload
  exit $LASTEXITCODE
}

python -m uvicorn quantz.api.app:app --host 127.0.0.1 --port 8787 --reload
