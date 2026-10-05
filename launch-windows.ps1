$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue) -or -not (Test-Path '.venv')) {
  throw 'Run setup-windows.ps1 first.'
}

uv sync --inexact --extra dev
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Server = Start-Process -FilePath $Python -ArgumentList @('-m', 'backend.app') -WorkingDirectory $Root -PassThru -NoNewWindow
try {
  $Ready = $false
  for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    if ($Server.HasExited) { throw 'The local service stopped during startup.' }
    try {
      Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2 | Out-Null
      $Ready = $true
      break
    } catch {
      Start-Sleep -Seconds 1
    }
  }
  if (-not $Ready) { throw 'Blot did not start in time.' }
  Start-Process 'http://127.0.0.1:8765/obfuscation-workspace.html'
  $Server.WaitForExit()
} finally {
  if (-not $Server.HasExited) { Stop-Process -Id $Server.Id -Force }
}
