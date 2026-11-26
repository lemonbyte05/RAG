param([switch]$Demo, [int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($Demo) {
    $demoFolder = Join-Path $PSScriptRoot 'data'
    New-Item -ItemType Directory -Path $demoFolder -Force | Out-Null
    $env:EHR_DB_PATH = Join-Path $demoFolder 'demo.db'
    $env:EHR_UPLOAD_DIR = Join-Path $demoFolder 'demo_uploads'
    python -m app.demo
    if ($LASTEXITCODE -ne 0) { throw 'Demo initialization failed' }
}
Write-Host "Open http://127.0.0.1:$Port/"
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
