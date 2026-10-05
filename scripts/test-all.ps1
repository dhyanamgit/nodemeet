# nodemeet: run every test on Windows (PowerShell). From the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\test-all.ps1            # unit + core databases + browsers
#   powershell -ExecutionPolicy Bypass -File scripts\test-all.ps1 -Full      # + all 19 databases (needs ~7 GB RAM for Docker)
#   powershell -ExecutionPolicy Bypass -File scripts\test-all.ps1 -NoDocker  # skip the database servers
param([switch]$Full, [switch]$NoDocker, [switch]$NoBrowsers)
$ErrorActionPreference = "Continue"
$report = "test-report.txt"
"nodemeet test report  $(Get-Date)" | Out-File $report

Write-Host "== 1/4 installing test packages" -ForegroundColor Cyan
python -m pip install -q -e . -r tests\requirements-test.txt -r tests\integration\requirements-db.txt -r tests\e2e\requirements-e2e.txt

Write-Host "== 2/4 unit tests" -ForegroundColor Cyan
python -m pytest tests -q -rs --ignore=tests\integration --ignore=tests\e2e 2>&1 | Tee-Object -Append $report

if (-not $NoDocker) {
  Write-Host "== 3/4 real databases (Docker)" -ForegroundColor Cyan
  $compose = "tests\integration\docker-compose.yml"
  if ($Full) { docker compose -f $compose --profile full up -d --wait } else { docker compose -f $compose up -d --wait }
  python -m pytest tests\integration -v -rs 2>&1 | Tee-Object -Append $report
  Write-Host "   (stop them later with: docker compose -f $compose --profile full down -v)"
}

if (-not $NoBrowsers) {
  Write-Host "== 4/4 browsers (Chromium, Firefox, WebKit)" -ForegroundColor Cyan
  python -m playwright install chromium firefox webkit
  python -m pytest tests\e2e -v -rs 2>&1 | Tee-Object -Append $report
}
Write-Host "Done. Send me $report if anything failed." -ForegroundColor Green
