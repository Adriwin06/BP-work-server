<#
.SYNOPSIS
  Sync the Google Drive assets, build the decomp, bundle exe + assets, zip, and
  publish to the BP work server's download button.

.DESCRIPTION
  Runs on a self-hosted Windows runner from the b5-decomp checkout. Copy this file
  to `ci/publish-build.ps1` in the b5-decomp repo (next to `.github/workflows/`).

  Pipeline:
    1. rclone sync  : mirror the current Drive folder locally (only changed files
                      are transferred; removed files are deleted). This is the
                      "check if any file changed" step -- the local copy always
                      matches Drive.
    2. manifest hash: fingerprint the synced asset set so each published build
                      records exactly which assets it shipped.
    3. cmake build  : configure + build the exe (MSVC / Release).
    4. bundle       : exe at the root, all assets alongside it at the root.
    5. zip + upload : POST the zip to /admin/builds with an admin X-Work-Token.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $WorkServer,     # e.g. https://adriwin.fr
    [Parameter(Mandatory)] [string] $WorkToken,      # an admin X-Work-Token
    [Parameter(Mandatory)] [string] $RcloneRemote,   # e.g. gdrive:BurnoutParadiseAssets
    [string] $CommitSha = "",
    [string] $Branch = "",
    [string] $AssetsDir = "C:\bp-build\assets",       # persistent across runs -> incremental sync
    [string] $BuildDir = "build",
    [string] $Config = "Release",
    [string] $ExePath = "",                           # auto-discovered if empty
    [string] $CMakeArgs = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- 1. Sync assets from Drive -------------------------------------------------
Step "Syncing assets from $RcloneRemote"
New-Item -ItemType Directory -Force -Path $AssetsDir | Out-Null
# --fast-list keeps Drive API calls down; sync mirrors adds/edits/deletes.
& rclone sync $RcloneRemote $AssetsDir --fast-list --transfers 8 --checkers 16
if ($LASTEXITCODE -ne 0) { throw "rclone sync failed ($LASTEXITCODE)" }

# --- 2. Asset manifest hash ----------------------------------------------------
Step "Fingerprinting assets"
$md5 = [System.Security.Cryptography.MD5]::Create()
$lines = Get-ChildItem -Path $AssetsDir -Recurse -File | Sort-Object FullName | ForEach-Object {
    $rel = $_.FullName.Substring($AssetsDir.Length).TrimStart('\','/').Replace('\','/')
    $hash = (Get-FileHash -Path $_.FullName -Algorithm MD5).Hash.ToLower()
    "$rel`:$hash"
}
$joined = ($lines -join "`n")
$sha = [System.Security.Cryptography.SHA256]::Create()
$assetManifestHash = ([System.BitConverter]::ToString(
    $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($joined))
) -replace '-', '').ToLower()
Write-Host "    $($lines.Count) asset files, manifest $($assetManifestHash.Substring(0,12))"

# --- 3. Build ------------------------------------------------------------------
Step "Configuring + building ($Config)"
$cfgArgs = @("-S", ".", "-B", $BuildDir)
if ($CMakeArgs) { $cfgArgs += $CMakeArgs.Split(" ") }
& cmake @cfgArgs
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed ($LASTEXITCODE)" }
& cmake --build $BuildDir --config $Config
if ($LASTEXITCODE -ne 0) { throw "cmake build failed ($LASTEXITCODE)" }

if (-not $ExePath) {
    $exe = Get-ChildItem -Path $BuildDir -Recurse -Filter *.exe -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $exe) { throw "no .exe produced under $BuildDir -- set -ExePath explicitly" }
    $ExePath = $exe.FullName
}
Write-Host "    exe: $ExePath"

# --- 4. Bundle: exe + assets share the same root -------------------------------
Step "Assembling bundle"
$staging = Join-Path $BuildDir "bundle"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging | Out-Null
Copy-Item $ExePath -Destination $staging
# Assets land at the bundle root, next to the exe (recurse into subfolders).
Copy-Item -Path (Join-Path $AssetsDir '*') -Destination $staging -Recurse -Force

# --- 5. Zip --------------------------------------------------------------------
Step "Zipping"
$zip = Join-Path $BuildDir "burnout-build.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
$sevenZip = Get-Command 7z -ErrorAction SilentlyContinue
if ($sevenZip) {
    & 7z a -tzip -mx=5 $zip (Join-Path $staging '*') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "7z failed ($LASTEXITCODE)" }
} else {
    # Compress-Archive is fine for modest bundles; install 7-Zip for large/fast zips.
    Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zip -CompressionLevel Optimal
}
$size = (Get-Item $zip).Length
Write-Host "    $zip ($([math]::Round($size / 1MB, 1)) MB)"

# --- 6. Publish ----------------------------------------------------------------
Step "Publishing to $WorkServer"
if (-not $CommitSha) { $CommitSha = (& git rev-parse HEAD).Trim() }
$short = if ($CommitSha) { $CommitSha.Substring(0, [Math]::Min(12, $CommitSha.Length)) } else { "" }
$builtAt = (Get-Date).ToUniversalTime().ToString("o")

# curl.exe streams the multipart body straight from disk -- no whole-file buffering,
# so multi-GB zips upload without exhausting runner memory.
& curl.exe --fail --show-error --silent `
    --retry 3 --retry-delay 5 `
    -H "X-Work-Token: $WorkToken" `
    -F "file=@$zip;type=application/zip" `
    -F "commit_sha=$CommitSha" `
    -F "commit_short=$short" `
    -F "branch=$Branch" `
    -F "asset_manifest_hash=$assetManifestHash" `
    -F "built_at=$builtAt" `
    "$WorkServer/admin/builds"
if ($LASTEXITCODE -ne 0) { throw "publish upload failed ($LASTEXITCODE)" }

Write-Host ""
Step "Published build $short ($([math]::Round($size / 1MB, 1)) MB)"
