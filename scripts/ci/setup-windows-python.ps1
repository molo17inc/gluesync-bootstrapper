# Embeddable Python 3.11.9 + pip + packages for the Windows shell-runner jobs (automator_smoke_windows,
# automator_build_windows).
#
# It used to download the Python zip from python.org and get-pip.py from bootstrap.pypa.io on every run. Now each
# download is looked up in this order and stored back on a miss:
#   1. the runner host:  C:\ci-cache\downloads   (survives between jobs on the shell runner)
#   2. the HQ build cache ($env:BUILD_CACHE_URL, scripts/ci/build-cache.ps1), keyed on the download URL. These jobs run on
#      protected refs only, so they read and write the trusted ci-cache path (group variable BUILD_CACHE_PASSWORD).
#   3. the origin (python.org, bootstrap.pypa.io)
# pip uses the Nexus PyPI proxy through $env:PIP_INDEX_URL (set globally in .gitlab-ci.yml) and a persistent wheel cache
# on the runner host, C:\ci-cache\pip. A cache that cannot be created or written never fails the job.
#
#   .\scripts\ci\setup-windows-python.ps1 -PythonDir <dir> -PipArgs @('-r', 'requirements.txt', 'pyinstaller')
param(
  [Parameter(Mandatory = $true)][string]$PythonDir,
  [string[]]$PipArgs = @()
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the progress bar slows Invoke-WebRequest down a lot on Windows PowerShell 5.1
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$pythonVersion = '3.11.9'
$cacheRoot = 'C:\ci-cache'

. (Join-Path $PSScriptRoot 'build-cache.ps1')

# Build cache entries are tgz files of .bc-downloads\<name> under the job dir. The helpers run with
# ErrorActionPreference Continue: Windows PowerShell 5.1 turns the stderr of curl.exe (a miss) into a terminating error
# under Stop, and a cache problem must never fail the job.
function Get-DownloadCacheKey([string]$OriginUrl) {
  $ErrorActionPreference = 'Continue'
  $extra = $env:BC_KEY_EXTRA
  try { $env:BC_KEY_EXTRA = $OriginUrl; return "download-$(Get-BcKey)" } finally { $env:BC_KEY_EXTRA = $extra }
}

function Restore-Download([string]$Name, [string]$OriginUrl, [string]$OutFile) {
  $ErrorActionPreference = 'Continue'
  try {
    if (Restore-BuildCache (Get-DownloadCacheKey $OriginUrl) $env:CI_PROJECT_DIR) {
      $cached = Join-Path (Join-Path $env:CI_PROJECT_DIR '.bc-downloads') $Name
      if ((Test-Path $cached) -and ((Get-Item $cached).Length -gt 0)) { Move-Item $cached $OutFile -Force; return $true }
    }
  } catch { Write-Host "build-cache: restore of $Name failed (ignored): $($_.Exception.Message)" }
  $global:LASTEXITCODE = 0
  return $false
}

function Save-Download([string]$Name, [string]$OriginUrl, [string]$File) {
  $ErrorActionPreference = 'Continue'
  try {
    $dir = Join-Path $env:CI_PROJECT_DIR '.bc-downloads'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Copy-Item $File (Join-Path $dir $Name) -Force
    Push-Location $env:CI_PROJECT_DIR
    try { Save-BuildCache (Get-DownloadCacheKey $OriginUrl) ".bc-downloads/$Name" } finally { Pop-Location }
    Remove-Item (Join-Path $dir $Name) -Force -ErrorAction SilentlyContinue
  } catch { Write-Host "build-cache: save of $Name failed (ignored): $($_.Exception.Message)" }
  $global:LASTEXITCODE = 0
}

# Fetch-Cached <name> <origin url> <out file>: host cache, then the build cache, then the origin (and store it back).
function Fetch-Cached([string]$Name, [string]$OriginUrl, [string]$OutFile) {
  $hostFile = $null
  try {
    $dir = Join-Path $cacheRoot 'downloads'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $hostFile = Join-Path $dir $Name
  } catch { Write-Host "host download cache unavailable: $($_.Exception.Message)" }

  if ($hostFile -and (Test-Path $hostFile) -and ((Get-Item $hostFile).Length -gt 0)) {
    Copy-Item $hostFile $OutFile -Force
    Write-Host "cache $Name <- runner host"
    return
  }

  if (-not (Restore-Download $Name $OriginUrl $OutFile)) {
    Invoke-WebRequest -UseBasicParsing -Uri $OriginUrl -OutFile $OutFile
    Save-Download $Name $OriginUrl $OutFile
  }

  if ($hostFile) {
    try { Copy-Item $OutFile $hostFile -Force } catch { Write-Host "host download cache not written: $($_.Exception.Message)" }
  }
}

function Invoke-Native([string]$What, [scriptblock]$Command) {
  & $Command
  if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

# Persistent pip wheel cache on the runner host
try {
  $pipCache = Join-Path $cacheRoot 'pip'
  New-Item -ItemType Directory -Force -Path $pipCache | Out-Null
  $env:PIP_CACHE_DIR = $pipCache
} catch { Write-Host "pip cache dir unavailable, using the default: $($_.Exception.Message)" }

New-Item -ItemType Directory -Force -Path $PythonDir | Out-Null
$pythonZip = Join-Path $PythonDir 'python.zip'
Fetch-Cached "python-$pythonVersion-embed-amd64.zip" "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip" $pythonZip
Expand-Archive -Path $pythonZip -DestinationPath $PythonDir -Force
Remove-Item $pythonZip

$pthFile = Join-Path $PythonDir 'python311._pth'
(Get-Content $pthFile) -replace '#import site', 'import site' | Set-Content $pthFile

$getPip = Join-Path $PythonDir 'get-pip.py'
Fetch-Cached 'get-pip.py' 'https://bootstrap.pypa.io/get-pip.py' $getPip

$python = Join-Path $PythonDir 'python.exe'
Invoke-Native 'get-pip' { & $python $getPip }
Invoke-Native 'pip upgrade' { & $python -m pip install --upgrade pip }
if ($PipArgs.Count -gt 0) {
  Invoke-Native 'pip install' { & $python -m pip install @PipArgs }
}
