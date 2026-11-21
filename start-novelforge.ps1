$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Port = if ($env:PORT) { $env:PORT } else { '3457' }
$Url = "http://127.0.0.1:$Port"
$HealthUrl = "$Url/api/health"

function Get-Backend {
  try {
    $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2
    if ($response.Headers['X-Novel-Forge-Backend'] -eq 'fastapi') { return 'fastapi' }
    return 'other'
  } catch { return $null }
}

$backend = Get-Backend
if ($backend -eq 'fastapi') {
  Start-Process $Url
  exit 0
}
if ($backend) {
  Write-Host "Port $Port is already in use. Stop the previous server or set PORT to a free port."
  exit 1
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$python = if ($env:NOVEL_FORGE_PYTHON) {
  $env:NOVEL_FORGE_PYTHON
} elseif (Test-Path -LiteralPath $venvPython -PathType Leaf) {
  $venvPython
} else {
  $systemPython = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $systemPython) {
    Write-Host 'Python 3.11+ is required. Install Python and the dependencies in requirements.txt.'
    exit 1
  }
  $systemPython.Source
}

$opener = Start-Job -ScriptBlock {
  param($health, $url)
  for ($i = 0; $i -lt 60; $i++) {
    try {
      $response = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 1
      if ($response.Headers['X-Novel-Forge-Backend'] -eq 'fastapi') {
        Start-Process $url
        break
      }
    } catch {}
    Start-Sleep -Milliseconds 350
  }
} -ArgumentList $HealthUrl, $Url

$code = 1
try {
  & $python -B -m novel_forge
  $code = $LASTEXITCODE
} finally {
  Stop-Job $opener -ErrorAction SilentlyContinue
  Remove-Job $opener -ErrorAction SilentlyContinue
}
exit $code
