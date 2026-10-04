$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw 'Install uv from https://docs.astral.sh/uv/ and rerun this setup.'
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  throw 'Install Node.js LTS (which includes npm) and rerun this setup.'
}

uv sync --extra dev
if ($LASTEXITCODE -ne 0) { throw 'Python environment setup failed.' }
npm ci
if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency setup failed.' }
npm run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
Write-Host 'Blot setup is complete. Run launch-windows.ps1 to start the local app.'
