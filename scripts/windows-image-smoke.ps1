# Smoke test of the Windows bootstrapper image, on a Windows runner: `docker run` of the pushed image.
#   --version, --help, self-test        the exe starts on Nano Server and carries every module it needs
#   hub-first-login, no Core Hub        exits 2 with "Core Hub not reachable" and no traceback
#   in-container tests                  a fake Core Hub inside the container: hub-first-login, then the bootstrapper command
# Used by the bootstrapper_windows_smoke_* jobs of .gitlab-ci.yml: .\scripts\windows-image-smoke.ps1 -Image <ref> -ExpectedVersion <v>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Image,
  [string]$ExpectedVersion = ''
)
$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }

function Invoke-Docker {
  # docker's own exit code and both streams, without PowerShell turning stderr into a terminating error
  param([string[]]$DockerArgs, [int]$TimeoutSeconds = 300, [string]$ContainerName = '')
  $stdout = [System.IO.Path]::GetTempFileName()
  $stderr = [System.IO.Path]::GetTempFileName()
  try {
    # one command-line string: Windows PowerShell 5.1 and PowerShell 7 quote an argument array differently
    $commandLine = ($DockerArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $process = Start-Process -FilePath 'docker' -ArgumentList $commandLine -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -NoNewWindow
    $null = $process.Handle  # Windows PowerShell 5.1 loses ExitCode without a cached handle
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      $process.Kill()
      if ($ContainerName) { cmd /c "docker rm -f $ContainerName >nul 2>nul"; $global:LASTEXITCODE = 0 }
      Write-Host '---- output until the timeout ----'
      Write-Host (Get-Content -Raw -ErrorAction SilentlyContinue $stdout)
      Write-Host (Get-Content -Raw -ErrorAction SilentlyContinue $stderr)
      throw "docker $($DockerArgs -join ' ') did not finish within $TimeoutSeconds seconds"
    }
    $process.WaitForExit()
    $out = (Get-Content -Raw -ErrorAction SilentlyContinue $stdout)
    $err = (Get-Content -Raw -ErrorAction SilentlyContinue $stderr)
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = "$out"; Stderr = "$err"; Output = "$out$err" }
  }
  finally {
    Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
  }
}

function Assert-That {
  param([bool]$Condition, [string]$Message, $Result)
  if (-not $Condition) {
    if ($Result) { Write-Host '---- output ----'; Write-Host $Result.Output; Write-Host "---- exit code $($Result.ExitCode) ----" }
    throw "SMOKE FAILED: $Message"
  }
  Write-Host "ok: $Message"
}

Write-Host "Smoke testing $Image"
$size = (& docker image inspect $Image --format '{{.Size}}')
if ($LASTEXITCODE -eq 0 -and $size) { Write-Host ("image size: {0:N1} MB" -f ([double]$size / 1MB)) }

$version = Invoke-Docker @('run', '--rm', $Image, '--version')
Assert-That ($version.ExitCode -eq 0) '--version exits 0' $version
if ($ExpectedVersion) {
  Assert-That ($version.Stdout.Trim() -eq $ExpectedVersion) "--version prints $ExpectedVersion (got '$($version.Stdout.Trim())')" $version
}

$help = Invoke-Docker @('run', '--rm', $Image, '--help')
Assert-That ($help.ExitCode -eq 0) '--help exits 0' $help
Assert-That ($help.Stdout -match 'hub-first-login' -and $help.Stdout -match 'CORE_HUB_URL') '--help documents the commands and the environment' $help

$selfTest = Invoke-Docker @('run', '--rm', $Image, 'self-test')
Assert-That ($selfTest.ExitCode -eq 0) 'self-test exits 0 (every bundled module loads, the exe can run itself)' $selfTest
Assert-That ($selfTest.Stdout -match 'self-test OK' -and $selfTest.Stdout -match 'utf8_mode=1') 'self-test reports OK with UTF-8 mode on' $selfTest

# The same call the Linux kit makes, with no Core Hub behind it: a clear error, exit code 2, no stack trace
$dry = Invoke-Docker @('run', '--rm', '-e', 'CORE_HUB_URL=http://127.0.0.1:1', '-e', 'HUB_ADMIN_PASSWORD=Test123!', $Image, 'hub-first-login', '--attempts', '2', '--interval', '1')
Assert-That ($dry.ExitCode -eq 2) 'hub-first-login without a Core Hub exits with code 2' $dry
Assert-That ($dry.Stderr -match 'Core Hub not reachable at http://127\.0\.0\.1:1 after 2 attempt') 'hub-first-login names the Core Hub URL it could not reach' $dry
Assert-That ($dry.Output -notmatch 'Traceback') 'hub-first-login without a Core Hub prints no traceback' $dry

# The kit's entrypoint override form must work too: docker run --entrypoint <exe> <image> hub-first-login
$override = Invoke-Docker @('run', '--rm', '--entrypoint', 'C:\opt\python\gluesync-bootstrapper.exe', '-e', 'CORE_HUB_URL=http://127.0.0.1:1', '-e', 'HUB_ADMIN_PASSWORD=Test123!', $Image, 'hub-first-login', '--attempts', '1')
Assert-That ($override.ExitCode -eq 2) '--entrypoint C:\opt\python\gluesync-bootstrapper.exe hub-first-login behaves the same' $override

# A fake Core Hub inside the container: the real first-login exchange and the bootstrapper command
$scriptsDir = (Resolve-Path -LiteralPath $here).Path
$containerName = "bootstrapper-smoke-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$inside = Invoke-Docker @('run', '--rm', '--name', $containerName, '--entrypoint', 'pwsh', '-v', "${scriptsDir}:C:\smoke", $Image, '-NoProfile', '-File', 'C:\smoke\windows-image-in-container-test.ps1') -ContainerName $containerName
Write-Host $inside.Output
Assert-That ($inside.ExitCode -eq 0) 'the in-container tests (fake Core Hub) pass' $inside

Write-Host "Smoke test of $Image passed"
