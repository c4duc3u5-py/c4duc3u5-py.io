#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run the eBay blog pipeline and deploy to GitHub Pages.
.DESCRIPTION
    Full pipeline: scrape eBay > plan posts > AI write > build Hugo > commit ALL changes > push source > deploy gh-pages.
    Commits everything: posts, images, templates, config, data, Python modules.
.PARAMETER SkipScrape
    Skip eBay scraping and use cached listings.json
.PARAMETER MaxPosts
    Maximum number of posts to generate (default: 5)
.PARAMETER NoPush
    Generate and build but don't push/deploy to GitHub
.PARAMETER NoPinterest
    Skip Pinterest pinning step
.EXAMPLE
    .\run.ps1                          # Full pipeline + deploy
    .\run.ps1 -SkipScrape -MaxPosts 3  # Use cached listings, 3 posts
    .\run.ps1 -NoPush                  # Build only, no deploy
    .\run.ps1 -NoPinterest             # Skip Pinterest
#>
param(
    [switch]$SkipScrape,
    [int]$MaxPosts = 5,
    [switch]$NoPush,
    [switch]$NoPinterest
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

# -- Step 1: Run the Python pipeline --
Write-Host "[1/5] Running blog generator..." -ForegroundColor Yellow

$pythonArgs = @("$ProjectRoot\blog-generator\main.py", "--max-posts", $MaxPosts)
if ($SkipScrape) {
    $pythonArgs += "--skip-scrape"
    Write-Host "       (skipping scrape, using cached listings)" -ForegroundColor DarkGray
}
if ($NoPinterest) {
    $pythonArgs += "--no-pinterest"
    Write-Host "       (skipping Pinterest pinning)" -ForegroundColor DarkGray
}

python @pythonArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "       WARNING: Pipeline exited with code $LASTEXITCODE (some posts may have failed)" -ForegroundColor DarkYellow
    Write-Host "       Continuing with build and deploy..." -ForegroundColor DarkYellow
}

# -- Step 2: Build Hugo site --
Write-Host "`n[2/5] Building Hugo site..." -ForegroundColor Yellow

Push-Location "$ProjectRoot\site"
try {
    # Install theme if missing
    if (-not (Test-Path "themes\ananke")) {
        Write-Host "       Installing Ananke theme..." -ForegroundColor DarkGray
        git clone https://github.com/theNewDynamic/gohugo-theme-ananke.git themes/ananke --depth 1
    }
    & $HugoPath --minify --cleanDestinationDir
    if ($LASTEXITCODE -ne 0) {
        throw "Hugo build failed! Exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

# -- Step 3: Commit ALL source changes --
Write-Host "`n[3/5] Committing changes..." -ForegroundColor Yellow

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

    # -- Step 4: Push source to main --
    if (-not $NoPush) {
        Write-Host "`n[4/5] Pushing source to GitHub..." -ForegroundColor Yellow
        git push origin main
        Write-Host "       Pushed source to main" -ForegroundColor Green

        # -- Step 5: Deploy built site to gh-pages --
        Write-Host "`n[5/5] Deploying to gh-pages..." -ForegroundColor Yellow

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
        Write-Host "`n[4/5] Skipping push (NoPush)" -ForegroundColor DarkGray
        Write-Host "[5/5] Skipping deploy (NoPush)" -ForegroundColor DarkGray
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
