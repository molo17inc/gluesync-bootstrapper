# build-cache.ps1: PowerShell twin of build-cache.sh for the Windows runners (hq-windows-shell-*).
# Canonical copy: k8s-gitops apps/build-cache/client/build-cache.ps1. Projects vendor it as ci/build-cache.ps1.
#
#   . .\ci\build-cache.ps1
#   $key = Get-BcKey requirements.txt
#   if (-not (Restore-BuildCache "venv-$key")) { <build venv>; Save-BuildCache "venv-$key" venv }
#
# Same trust model and paths as build-cache.sh (protected refs -> ci-cache, others -> ci-cache-mr).
# Uses curl.exe and tar.exe (both ship with Windows 10/Server 2019+). Never fails the job.

if (-not $env:BUILD_CACHE_URL) { $env:BUILD_CACHE_URL = "https://nexus.hq.molo17.com/repository" }
$script:BcProject = if ($env:BC_NAMESPACE) { $env:BC_NAMESPACE } elseif ($env:CI_PROJECT_PATH_SLUG) { $env:CI_PROJECT_PATH_SLUG } else { "local" }

function Get-BcKey {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $ms = New-Object System.IO.MemoryStream
  $add = { param($b) $ms.Write($b, 0, $b.Length) }
  foreach ($p in $Paths) {
    if (Test-Path $p -PathType Container) {
      Get-ChildItem $p -Recurse -File | Sort-Object FullName | ForEach-Object {
        & $add ([Text.Encoding]::UTF8.GetBytes($_.FullName)); & $add ([IO.File]::ReadAllBytes($_.FullName)) }
    } elseif (Test-Path $p -PathType Leaf) {
      & $add ([Text.Encoding]::UTF8.GetBytes($p)); & $add ([IO.File]::ReadAllBytes((Resolve-Path $p)))
    } else { & $add ([Text.Encoding]::UTF8.GetBytes("${p}:absent")) }
  }
  & $add ([Text.Encoding]::UTF8.GetBytes("$($env:BC_KEY_EXTRA)|windows-$($env:PROCESSOR_ARCHITECTURE)"))
  $hash = $sha.ComputeHash($ms.ToArray())
  return (-join ($hash | ForEach-Object { $_.ToString("x2") })).Substring(0, 16)
}

function Test-BcTrusted { return $env:CI_COMMIT_REF_PROTECTED -eq "true" }

function Restore-BuildCache {
  param([string]$Key, [string]$Dir = ".")
  $tmp = Join-Path $env:TEMP "bc-$PID-$Key.tgz"
  $repos = if (Test-BcTrusted) { @("ci-cache") } else { @("ci-cache", "ci-cache-mr") }
  foreach ($r in $repos) {
    & curl.exe -fsS --retry 2 --connect-timeout 10 -o $tmp "$($env:BUILD_CACHE_URL)/$r/$script:BcProject/$Key.tgz" 2>$null
    if ($LASTEXITCODE -eq 0) {
      New-Item -ItemType Directory -Force -Path $Dir | Out-Null
      & tar.exe -xzf $tmp -C $Dir
      if ($LASTEXITCODE -eq 0) { Remove-Item $tmp -Force; Write-Host "build-cache: hit $Key ($r)"; return $true }
    }
  }
  Remove-Item $tmp -Force -ErrorAction SilentlyContinue
  Write-Host "build-cache: miss $Key"; $global:LASTEXITCODE = 0
  return $false
}

function Save-BuildCache {
  param([string]$Key, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths)
  if (Test-BcTrusted) { $r = "ci-cache"; $u = "ci-cache"; $pw = $env:BUILD_CACHE_PASSWORD }
  else { $r = "ci-cache-mr"; $u = "ci-cache-mr"; $pw = $env:BUILD_CACHE_MR_PASSWORD }
  if (-not $pw) { Write-Host "build-cache: upload of $Key skipped (no credentials for $r)"; return }
  $url = "$($env:BUILD_CACHE_URL)/$r/$script:BcProject/$Key.tgz"
  & curl.exe -fsS -I --connect-timeout 10 $url 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Host "build-cache: $Key already in $r"; return }
  $tmp = Join-Path $env:TEMP "bc-$PID-$Key.tgz"
  & tar.exe -czf $tmp @Paths
  if ($LASTEXITCODE -eq 0) {
    & curl.exe -fsS --retry 2 --connect-timeout 10 -u "${u}:$pw" --upload-file $tmp $url
    if ($LASTEXITCODE -eq 0) { Write-Host "build-cache: saved $Key to $r ($([math]::Round((Get-Item $tmp).Length / 1MB, 1)) MB)" }
    else { Write-Host "build-cache: upload of $Key failed (ignored)" }
  } else { Write-Host "build-cache: tar of $Key failed (ignored)" }
  Remove-Item $tmp -Force -ErrorAction SilentlyContinue
  $global:LASTEXITCODE = 0
}
