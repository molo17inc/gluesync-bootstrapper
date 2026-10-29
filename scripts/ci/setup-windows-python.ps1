# Embeddable Python 3.11.9 + pip + packages for the Windows shell-runner jobs (automator_smoke_windows,
# automator_build_windows).
#
# It used to download the Python zip from python.org and get-pip.py from bootstrap.pypa.io on every run. Now each
# download is looked up in this order and stored back on a miss:
#   1. the runner host:  C:\ci-cache\downloads   (survives between jobs on the shell runner)
#   2. Nexus raw cache:  ui-build-cache/gluesync-bootstrapper/shared/windows-python   (skipped when $env:NEXUS_REPO_PASSWORD is empty)
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
$nexusCache = 'https://nexus.hq.molo17.com/repository/ui-build-cache/gluesync-bootstrapper/shared/windows-python'
$cacheRoot = 'C:\ci-cache'

function Get-NexusHeaders {
  if (-not $env:NEXUS_REPO_PASSWORD) { return $null }
  $token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("gluesync:$($env:NEXUS_REPO_PASSWORD)"))
  return @{ Authorization = "Basic $token" }
}

# Fetch-Cached <name> <origin url> <out file>: host cache, then Nexus, then the origin (and store it back).
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

  $headers = Get-NexusHeaders
  $fromNexus = $false
  if ($headers) {
    try {
      Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri "$nexusCache/$Name" -OutFile $OutFile
      if ((Get-Item $OutFile).Length -gt 0) { $fromNexus = $true; Write-Host "cache $Name <- nexus" }
    } catch {
      Write-Host "cache ${Name}: miss"
      Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
    }
  }

  if (-not $fromNexus) {
    Invoke-WebRequest -UseBasicParsing -Uri $OriginUrl -OutFile $OutFile
    if ($headers) {
      try {
        Invoke-WebRequest -UseBasicParsing -Headers $headers -Method Put -InFile $OutFile -Uri "$nexusCache/$Name" | Out-Null
        Write-Host "cache $Name -> nexus"
      } catch { Write-Host "cache ${Name}: upload skipped" }
    }
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
