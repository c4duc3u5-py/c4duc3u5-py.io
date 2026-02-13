#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run the eBay blog pipeline locally and push to GitHub.
.DESCRIPTION
    This script runs the full pipeline (scrape → plan → write → build → push)
    or skips scraping if listings.json already exists.
.PARAMETER SkipScrape
    Skip eBay scraping and use cached listings.json
.PARAMETER MaxPosts
    Maximum number of posts to generate (default: 5)
.PARAMETER NoPush
    Generate posts but don't push to GitHub
.EXAMPLE
    .\run.ps1 -SkipScrape -MaxPosts 3
    .\run.ps1 -NoPush
#>
param(
    [switch]$SkipScrape,
    [int]$MaxPosts = 5,
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Write-Host "`n=== eBay Auto-Blog Pipeline ===" -ForegroundColor Cyan
Write-Host "Project: $ProjectRoot"
Write-Host "Time:    $(Get-Date -Format 'yyyy-MM-dd HH:mm')`n"

# ── Step 1: Run the Python pipeline ──
Write-Host "[1/4] Running blog generator..." -ForegroundColor Yellow

$pythonArgs = @("$ProjectRoot\blog-generator\main.py", "--max-posts", $MaxPosts)
if ($SkipScrape) {
    $pythonArgs += "--skip-scrape"
    Write-Host "       (skipping scrape, using cached listings)" -ForegroundColor DarkGray
}

python @pythonArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Pipeline failed!" -ForegroundColor Red
    exit 1
}

# ── Step 2: Build Hugo site ──
Write-Host "`n[2/4] Building Hugo site..." -ForegroundColor Yellow

Push-Location "$ProjectRoot\site"
try {
    # Install theme if missing
    if (-not (Test-Path "themes\ananke")) {
        Write-Host "       Installing Ananke theme..." -ForegroundColor DarkGray
        git clone https://github.com/theNewDynamic/gohugo-theme-ananke.git themes/ananke --depth 1
    }
    hugo --minify
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Hugo build failed!" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

# ── Step 3: Commit changes ──
Write-Host "`n[3/4] Committing changes..." -ForegroundColor Yellow

Push-Location $ProjectRoot
try {
    git add site/content/posts/ blog-generator/data/
    $hasChanges = git diff --staged --quiet 2>&1; $LASTEXITCODE -ne 0
    if ($hasChanges) {
        $date = Get-Date -Format "yyyy-MM-dd"
        git commit -m "🤖 Auto-generated blog posts [$date]"
        Write-Host "       Committed new posts" -ForegroundColor Green
    } else {
        Write-Host "       No new changes to commit" -ForegroundColor DarkGray
    }

    # ── Step 4: Push ──
    if (-not $NoPush) {
        Write-Host "`n[4/5] Pushing source to GitHub..." -ForegroundColor Yellow
        git push
        Write-Host "       Pushed source to main" -ForegroundColor Green

        # ── Step 5: Deploy built site to gh-pages ──
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
            $siteChanged = git diff --staged --quiet 2>&1; $LASTEXITCODE -ne 0
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
        Write-Host "`n[4/5] Skipping push (--NoPush)" -ForegroundColor DarkGray
        Write-Host "[5/5] Skipping deploy (--NoPush)" -ForegroundColor DarkGray
    }
} finally {
    Pop-Location
}

Write-Host "`n=== Done! ===" -ForegroundColor Cyan
Write-Host "Site URL: https://c4duc3u5-py.github.io/c4duc3u5-py.io/`n"
