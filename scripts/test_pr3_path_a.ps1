# Test harness for PR 3 — Stage 6.45 Path A (NOF grantor -> DCAD owner_index)
#
# What this does:
#   1. Pulls the latest claude/pr3-path-a-nof-grantor branch from origin
#   2. Runs the unit test suite for resolution_paths + dcad_owner_index
#   3. Runs the validation harness against data/records.json with --path A
#   4. (Optional) Runs --path all to see full pipeline order B -> variant -> A
#   5. Writes enriched records to data/records.path_a.json for diffing
#
# Does NOT touch data/records.json. Production records are read-only here.
#
# Usage:
#   .\scripts\test_pr3_path_a.ps1
#   .\scripts\test_pr3_path_a.ps1 -SkipPull       # don't git pull
#   .\scripts\test_pr3_path_a.ps1 -SkipTests      # skip pytest
#   .\scripts\test_pr3_path_a.ps1 -All            # also run --path all
#   .\scripts\test_pr3_path_a.ps1 -RebuildCache   # force-rebuild DCAD cache

param(
    [switch]$SkipPull,
    [switch]$SkipTests,
    [switch]$All,
    [switch]$RebuildCache
)

$ErrorActionPreference = "Stop"

# Move to repo root (script lives in <repo>/scripts/)
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Header($text) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor Cyan
    Write-Host $text -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor Cyan
}

# 1. Pull PR 3 branch -----------------------------------------------------------
if (-not $SkipPull) {
    Write-Header "STEP 1: Pulling claude/pr3-path-a-nof-grantor"
    git fetch origin claude/pr3-path-a-nof-grantor
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }

    git checkout claude/pr3-path-a-nof-grantor
    if ($LASTEXITCODE -ne 0) { throw "git checkout failed" }

    git pull origin claude/pr3-path-a-nof-grantor
    if ($LASTEXITCODE -ne 0) { throw "git pull failed" }

    Write-Host ""
    Write-Host "On commit:" -ForegroundColor Green
    git log --oneline -1
} else {
    Write-Host "Skipping pull (use -SkipPull to skip, default is to pull)" -ForegroundColor Yellow
}

# 2. Unit tests -----------------------------------------------------------------
if (-not $SkipTests) {
    Write-Header "STEP 2: Running PR 3 unit tests"
    python -m pytest tests/test_resolution_paths.py tests/test_dcad_owner_index.py tests/test_address_variants.py -q
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed — fix before testing live records"
    }
    Write-Host ""
    Write-Host "All unit tests passed." -ForegroundColor Green
}

# 3. Rebuild DCAD cache (optional) ---------------------------------------------
if ($RebuildCache) {
    Write-Header "STEP 3: Rebuilding DCAD address_index + Path A indexes caches"
    python scripts/validate_resolution.py --build-cache
    if ($LASTEXITCODE -ne 0) { throw "Cache rebuild failed" }
}

# 4. Run Path A in isolation ----------------------------------------------------
Write-Header "STEP 4: Validating Path A against data/records.json"
python scripts/validate_resolution.py --path A --write data/records.path_a.json --verbose
if ($LASTEXITCODE -ne 0) { throw "Path A run failed" }

Write-Host ""
Write-Host "Output written to data/records.path_a.json" -ForegroundColor Green

# 5. Run full pipeline (optional) ----------------------------------------------
if ($All) {
    Write-Header "STEP 5: Validating full pipeline (B -> variant -> A)"
    python scripts/validate_resolution.py --path all --write data/records.all_paths.json --verbose
    if ($LASTEXITCODE -ne 0) { throw "Pipeline run failed" }

    Write-Host ""
    Write-Host "Output written to data/records.all_paths.json" -ForegroundColor Green
}

# 6. Suggested next steps -------------------------------------------------------
Write-Header "Done. Suggested next steps:"
Write-Host "  - Inspect newly resolved records in the summary above"
Write-Host "  - Diff against current production:"
Write-Host "      code --diff data/records.json data/records.path_a.json"
Write-Host "  - If something looks wrong, copy the per-record output (record_id,"
Write-Host "    grantor, before/after address, warnings) and report it back."
Write-Host ""
