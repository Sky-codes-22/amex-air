$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Error "AMEX AIR is not configured. The .env file is missing."
    exit 1
}
foreach ($line in Get-Content -LiteralPath $envFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
    $parts = $trimmed.Split("=", 2)
    if ($parts.Count -eq 2) { [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process") }
}
Set-Location -LiteralPath $projectRoot
Write-Host "Starting AMEX AIR laptop processing engine..." -ForegroundColor Cyan
python -m air.remote_worker