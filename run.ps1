#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run the eBay blog pipeline and deploy to GitHub Pages.
.DESCRIPTION
    Full pipeline: cleanup stale/dupe posts > scrape eBay > plan posts > AI write > build Hugo > commit ALL changes > push source > deploy gh-pages.
    Commits everything: posts, images, templates, config, data, Python modules.
.PARAMETER SkipScrape
    Skip eBay scraping and use cached listings.json
.PARAMETER MaxPosts
    Maximum number of posts to generate (default: 5)
.PARAMETER NoPush
    Generate and build but don't push/deploy to GitHub
.PARAMETER NoPinterest
    Skip Pinterest pinning step
.PARAMETER CleanupOnly
    Run deduplication + stale post cleanup only (no scraping or generation)
.PARAMETER SkipCleanup
    Skip the pre-run cleanup step
.PARAMETER Backend
    Scraping backend: 'browser' (Playwright, default) or 'http' (httpx)
.EXAMPLE
    .\run.ps1                          # Full pipeline + deploy
    .\run.ps1 -SkipScrape -MaxPosts 3  # Use cached listings, 3 posts
    .\run.ps1 -NoPush                  # Build only, no deploy
    .\run.ps1 -NoPinterest             # Skip Pinterest
    .\run.ps1 -CleanupOnly             # Just cleanup + push
    .\run.ps1 -Backend http            # Use HTTP scraper instead of browser
#>
param(
    [switch]$SkipScrape,
    [int]$MaxPosts = 5,
    [switch]$NoPush,
    [switch]$NoPinterest,
    [switch]$CleanupOnly,
    [switch]$SkipCleanup,
    [ValidateSet("browser", "http")]
    [string]$Backend = "browser"
)

$ErrorActionPreference = "Stop"

# Wrap everything in a try/catch so errors are visible
try {

$ProjectRoot = $PSScriptRoot
# If $PSScriptRoot is empty (running interactively), use current dir
if (-not $ProjectRoot) { $ProjectRoot = Get-Location }

# Hugo is not on PATH - use full path from WinGet install
$HugoPath = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Hugo.Hugo.Extended_Microsoft.Winget.Source_8wekyb3d8bbwe\hugo.exe"
if (-not (Test-Path $HugoPath)) {
    # Fallback: try PATH
    $HugoPath = "hugo"
}

Write-Host "`n=== eBay Auto-Blog Pipeline ===" -ForegroundColor Cyan
Write-Host "Project: $ProjectRoot"
Write-Host "Time:    $(Get-Date -Format 'yyyy-MM-dd HH:mm')`n"

# Force Python to use UTF-8 everywhere (Windows console fix for emojis)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Activate venv if present
$VenvPython = "$ProjectRoot\.venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    Write-Host "Using venv Python: $VenvPython" -ForegroundColor DarkGray
    $PythonCmd = $VenvPython
} else {
    $PythonCmd = "python"
}

# -- Step 0: Cleanup (dedup + stale management) --
if ($CleanupOnly) {
    Write-Host "[1/3] Running cleanup only..." -ForegroundColor Yellow
    & $PythonCmd "$ProjectRoot\blog-generator\main.py" --cleanup
    if ($LASTEXITCODE -ne 0) {
        Write-Host "       WARNING: Cleanup exited with code $LASTEXITCODE" -ForegroundColor DarkYellow
    }
    # Build Hugo so the public/ folder is up to date
    Write-Host "`n[2/3] Building Hugo site..." -ForegroundColor Yellow
    Push-Location "$ProjectRoot\site"
    try {
        if (-not (Test-Path "themes\ananke")) {
            Write-Host "       Installing Ananke theme..." -ForegroundColor DarkGray
            git clone https://github.com/theNewDynamic/gohugo-theme-ananke.git themes/ananke --depth 1
        }
        & $HugoPath --minify --cleanDestinationDir
        if ($LASTEXITCODE -ne 0) { throw "Hugo build failed! Exit code $LASTEXITCODE" }
    } finally { Pop-Location }

    if ($NoPush) {
        Write-Host "`n[3/3] Skipping push (NoPush)" -ForegroundColor DarkGray
        Write-Host "`n=== Cleanup Done! ===" -ForegroundColor Cyan
        Write-Host "`nPress any key to close..." -ForegroundColor DarkGray
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        return
    }
    # Otherwise fall through to commit/push below
    $stepPrefix = "3/3"
} else {
    # -- Step 1: Pre-run cleanup --
    if (-not $SkipCleanup) {
        Write-Host "[1/6] Running pre-run cleanup (dedup + stale)..." -ForegroundColor Yellow
        & $PythonCmd "$ProjectRoot\blog-generator\main.py" --cleanup
        if ($LASTEXITCODE -ne 0) {
            Write-Host "       WARNING: Cleanup exited with code $LASTEXITCODE" -ForegroundColor DarkYellow
        }
    } else {
        Write-Host "[1/6] Skipping cleanup (SkipCleanup)" -ForegroundColor DarkGray
    }

    # -- Step 2: Run the Python pipeline --
    Write-Host "`n[2/6] Running blog generator..." -ForegroundColor Yellow

    $pythonArgs = @("$ProjectRoot\blog-generator\main.py", "--max-posts", $MaxPosts, "--backend", $Backend)
    if ($SkipScrape) {
        $pythonArgs += "--skip-scrape"
        Write-Host "       (skipping scrape, using cached listings)" -ForegroundColor DarkGray
    }
    if ($NoPinterest) {
        $pythonArgs += "--no-pinterest"
        Write-Host "       (skipping Pinterest pinning)" -ForegroundColor DarkGray
    }

    & $PythonCmd @pythonArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "       WARNING: Pipeline exited with code $LASTEXITCODE (some posts may have failed)" -ForegroundColor DarkYellow
        Write-Host "       Continuing with build and deploy..." -ForegroundColor DarkYellow
    }

    # -- Step 3: Build Hugo site --
    Write-Host "`n[3/6] Building Hugo site..." -ForegroundColor Yellow

    Push-Location "$ProjectRoot\site"
    try {
        if (-not (Test-Path "themes\ananke")) {
            Write-Host "       Installing Ananke theme..." -ForegroundColor DarkGray
            git clone https://github.com/theNewDynamic/gohugo-theme-ananke.git themes/ananke --depth 1
        }
        & $HugoPath --minify --cleanDestinationDir
        if ($LASTEXITCODE -ne 0) { throw "Hugo build failed! Exit code $LASTEXITCODE" }
    } finally { Pop-Location }
    $stepPrefix = "4/6"
}

# -- Commit ALL source changes --
Write-Host "`n[$stepPrefix] Committing changes..." -ForegroundColor Yellow

Push-Location $ProjectRoot
try {
    # Stage everything: posts, images, data, templates, config, scripts, python modules
    git add -A

    # Check if there are staged changes
    git diff --staged --quiet 2>$null
    $hasChanges = $LASTEXITCODE -ne 0

    if ($hasChanges) {
        # Build a descriptive commit message
        $date = Get-Date -Format "yyyy-MM-dd"
        $newPosts = (git diff --staged --name-only | Where-Object { $_ -like "site/content/posts/*.md" }).Count
        $newImages = (git diff --staged --name-only | Where-Object { $_ -like "site/static/images/*" }).Count
        $changedFiles = (git diff --staged --name-only).Count

        $parts = @("Auto-update [$date]")
        if ($newPosts -gt 0) { $parts += "$newPosts new posts" }
        if ($newImages -gt 0) { $parts += "$newImages images" }
        $parts += "$changedFiles files total"
        $commitMsg = $parts -join " | "

        git commit -m $commitMsg
        Write-Host "       Committed: $commitMsg" -ForegroundColor Green
    } else {
        Write-Host "       No new changes to commit" -ForegroundColor DarkGray
    }

    # -- Push source to main --
    if (-not $NoPush) {
        Write-Host "`nPushing source to GitHub..." -ForegroundColor Yellow
        git push origin main
        Write-Host "       Pushed source to main" -ForegroundColor Green

        # -- Deploy built site to gh-pages --
        Write-Host "`nDeploying to gh-pages..." -ForegroundColor Yellow

        Push-Location "$ProjectRoot\site\public"
        try {
            if (-not (Test-Path ".git")) {
                git init -b gh-pages
                git remote add origin https://github.com/c4duc3u5-py/c4duc3u5-py.io.git
            }
            # Prevent GitHub from processing with Jekyll
            New-Item -Path ".nojekyll" -ItemType File -Force | Out-Null
            git config user.name "c4duc3u5-py"
            git config user.email "c4duc3u5-py@users.noreply.github.com"
            git add -A

            git diff --staged --quiet 2>$null
            $siteChanged = $LASTEXITCODE -ne 0

            if ($siteChanged) {
                $date = Get-Date -Format "yyyy-MM-dd HH:mm"
                git commit -m "Deploy site [$date]"
                git push -f origin gh-pages
                Write-Host "       Deployed to gh-pages!" -ForegroundColor Green
            } else {
                Write-Host "       Site unchanged, skipping deploy" -ForegroundColor DarkGray
            }
        } finally {
            Pop-Location
        }
    } else {
        Write-Host "`nSkipping push (NoPush)" -ForegroundColor DarkGray
        Write-Host "Skipping deploy (NoPush)" -ForegroundColor DarkGray
    }
} finally {
    Pop-Location
}

Write-Host "`n=== Done! ===" -ForegroundColor Cyan
Write-Host "Site URL: https://c4duc3u5-py.github.io/c4duc3u5-py.io/`n"

} catch {
    Write-Host "`n=== ERROR ===" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
}

# Keep window open so you can read the output
Write-Host "`nPress any key to close..." -ForegroundColor DarkGray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
