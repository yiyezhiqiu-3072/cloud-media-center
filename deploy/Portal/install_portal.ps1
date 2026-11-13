# Cloud Media Center - portal static file installer
# Run this ON THE SERVER that hosts nginx, from the folder that contains
# the delivered portal files (index.html, status.html, login.html,
# version.js, switcher.js, player.js).
#
# Why: the home page is a plain static file served by nginx. Restarting
# any backend .exe does NOT update it. This script copies the delivered
# files into the real nginx web root (with a backup + verification).
#
# Usage:
#   .\install_portal.ps1 -WebRoot "C:\nginx\html"
#   .\install_portal.ps1 -NginxConf "C:\nginx\conf\nginx.conf"
#   .\install_portal.ps1                      # auto-detect web root
#
# After running: nginx -s reload, then hard-refresh the browser (Ctrl+F5).

param(
    [string]$WebRoot = "",
    [string]$NginxConf = ""
)

$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$files = @("index.html", "status.html", "login.html", "version.js", "switcher.js", "player.js")

foreach ($f in $files) {
    if (-not (Test-Path (Join-Path $src $f))) {
        Write-Host "ERROR: missing delivered file: $f" -ForegroundColor Red
        Write-Host "Put install_portal.ps1 in the same folder as the delivered Portal files."
        exit 1
    }
}

# ---- 1. Locate nginx web root -------------------------------------------
$candidates = @()

if ($WebRoot) { $candidates += $WebRoot }

if (-not $NginxConf) {
    foreach ($p in @("C:\nginx\conf\nginx.conf", "C:\Cloud\nginx\conf\nginx.conf",
                     "C:\Program Files\nginx\conf\nginx.conf", "D:\nginx\conf\nginx.conf")) {
        if (Test-Path $p) { $NginxConf = $p; break }
    }
}

if ($NginxConf -and (Test-Path $NginxConf)) {
    $txt = Get-Content -Raw -LiteralPath $NginxConf
    foreach ($m in [regex]::Matches($txt, '(?m)^\s*root\s+([^;]+);')) {
        $r = $m.Groups[1].Value.Trim().Trim('"')
        if ($r -notmatch '^[A-Za-z]:' -and $r -notmatch '^/') {
            $r = Join-Path (Split-Path -Parent $NginxConf) $r
        }
        $r = $r -replace '/', '\'
        if (Test-Path (Join-Path $r "index.html")) { $candidates += $r }
    }
}

$candidates = @($candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)

if ($candidates.Count -eq 0) {
    Write-Host "ERROR: cannot find nginx web root." -ForegroundColor Red
    Write-Host "Run this on the server with:  .\install_portal.ps1 -WebRoot ""C:\path\to\html"""
    Write-Host "Hint: run 'nginx -T' and look for the 'root' line."
    exit 1
}

# Prefer a root whose current index.html is the old portal page.
$target = $null
foreach ($c in $candidates) {
    $cur = Join-Path $c "index.html"
    if ((Test-Path $cur) -and (Select-String -LiteralPath $cur -Pattern 'auth/health' -Quiet)) {
        $target = $c; break
    }
}
if (-not $target) { $target = $candidates[0] }

Write-Host "Source : $src"
Write-Host "WebRoot: $target"
if ($NginxConf) { Write-Host "Nginx  : $NginxConf" }
Write-Host ""

# ---- 2. Backup + copy ----------------------------------------------------
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bak = Join-Path $target ("portal_backup_" + $stamp)
New-Item -ItemType Directory -Path $bak -Force | Out-Null

foreach ($f in $files) {
    $to = Join-Path $target $f
    if (Test-Path $to) { Copy-Item -LiteralPath $to -Destination (Join-Path $bak $f) -Force }
    Copy-Item -LiteralPath (Join-Path $src $f) -Destination $to -Force
    Write-Host ("  copied  " + $f)
}
Write-Host ("  backup  " + $bak)

# ---- 3. Verify -----------------------------------------------------------
$newIndex = Get-Content -Raw -LiteralPath (Join-Path $target "index.html")
$okCard = $newIndex -match 'href="device/"'
$okStatus = (Get-Content -Raw -LiteralPath (Join-Path $target "status.html")) -match 'svcDevice'

Write-Host ""
if ($okCard) { Write-Host "  OK   device card is present in index.html" -ForegroundColor Green }
else { Write-Host "  FAIL device card missing in index.html" -ForegroundColor Red }
if ($okStatus) { Write-Host "  OK   device service row is present in status.html" -ForegroundColor Green }
else { Write-Host "  FAIL device row missing in status.html" -ForegroundColor Red }

# ---- 4. Reminders --------------------------------------------------------
if ($NginxConf -and (Test-Path $NginxConf)) {
    $nc = Get-Content -Raw -LiteralPath $NginxConf
    if ($nc -match 'location\s+/device/') {
        Write-Host "  OK   nginx.conf already has the /device/ proxy block" -ForegroundColor Green
    } else {
        Write-Host "  TODO nginx.conf has NO /device/ block: copy the delivered nginx.conf," -ForegroundColor Yellow
        Write-Host "       keep your own paths/users, then run: nginx -s reload" -ForegroundColor Yellow
    }
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) nginx -s reload   (only if nginx.conf changed)"
Write-Host "  2) Hard-refresh the browser (Ctrl+F5) or clear cache"
Write-Host "  3) Home page should show 5 cards; /device/ must open the new app"
if (-not $okCard -or -not $okStatus) { exit 1 }
exit 0
