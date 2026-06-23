<#
.SYNOPSIS
  One command to refresh the dashboard's data from the workflow + b5-decomp.

.DESCRIPTION
  Safe by design:
    * Backs up the database first (timestamped, never overwrites a prior backup).
    * Re-imports WITHOUT --reset, so live claims and correct done/blocked status are
      preserved -- import only applies committed done/blocked and skips rows that
      already match, so it never downgrades correct newer data.
    * Regenerates class_homes.json (the class TU -> real file map) from the current
      committed sources, then re-warms Git attribution.

  It does NOT run git pull/merge (so it can never fail on a diverged repo or touch
  history). Pull new commits yourself first if you want others' latest:
      git -C <workflow> pull
      git -C <workflow>\b5-decomp fetch origin dev

  Pass -Reconcile to also regenerate progress/status.json from committed files in
  promote-only mode (adds newly-finished work, never demotes anything).

.EXAMPLE
  .\sync.ps1
  .\sync.ps1 -Reconcile
  .\sync.ps1 -WorkflowRoot d:\Reverse\BP-Decomp_Workflow
#>
[CmdletBinding()]
param(
    [string]$WorkflowRoot = "d:\Reverse\BP-Decomp_Workflow",
    [string]$Database,
    [switch]$Reconcile,
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$decomp = Join-Path $WorkflowRoot "b5-decomp"
$env:BP_DECOMP_ROOT = $decomp

if (-not $Database) { $Database = Join-Path $root "data\bp-work.sqlite3" }
$usersDb = Join-Path (Split-Path $Database) `
    ((Split-Path $Database -LeafBase) + "-users" + [IO.Path]::GetExtension($Database))

if (-not (Test-Path $python)) { throw "venv python missing ($python) - run .\launch.ps1 once to create it." }
if (-not (Test-Path $decomp)) { throw "b5-decomp not found ($decomp) - pass -WorkflowRoot." }

# 1) SAFETY NET: timestamped backup, never clobbered.
if (-not $NoBackup) {
    $backupDir = Join-Path $root ("data\manual-backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    foreach ($db in @($Database, $usersDb)) { if (Test-Path $db) { Copy-Item $db $backupDir } }
    Write-Host "[1/4] Backed up DB -> $backupDir" -ForegroundColor Green
}

# 2) Refresh derived inputs from the current committed sources.
Write-Host "[2/4] Resolving class home files..." -ForegroundColor Cyan
& $python (Join-Path $WorkflowRoot "tools\work\resolve_class_homes.py") --apply
if ($Reconcile) {
    Write-Host "      Reconciling status.json (promote-only)..." -ForegroundColor Cyan
    & $python (Join-Path $WorkflowRoot "tools\work\reconcile_from_files.py") --apply --no-demote
}

# 3) Re-import (no --reset: preserves live claims + correct status).
Write-Host "[3/4] Importing progress into the server DB (no reset)..." -ForegroundColor Cyan
& $python -m bp_work_server.cli --db $Database import $WorkflowRoot

# 4) Re-warm Git attribution for the current revision.
Write-Host "[4/4] Warming Git attribution..." -ForegroundColor Cyan
& $python -m bp_work_server.cli --db $Database warm-attribution-cache --decomp-root $decomp --branch dev

Write-Host ""
Write-Host "Sync complete. View it with:  .\launch.ps1 -NoImport -DecompRoot $decomp" -ForegroundColor Green
