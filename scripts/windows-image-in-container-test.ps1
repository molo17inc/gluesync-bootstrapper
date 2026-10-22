# Runs INSIDE the Windows bootstrapper image (pwsh is its base): a fake Core Hub on loopback, then the exe against it.
# Started by scripts/windows-image-smoke.ps1 as
#   docker run --entrypoint pwsh -v <repo>\scripts:C:\smoke <image> -File C:\smoke\windows-image-in-container-test.ps1
#
#   1. hub-first-login on a fresh hub   -> exit 0, "first access completed: 200"
#   2. hub-first-login again            -> exit 0, "admin password already set"
#   3. the bootstrapper command         -> main.py is loaded, logs in to the hub and saves its token under
#                                          C:\opt\config (it then fails at the pipeline creation: the fake hub only
#                                          knows the two authentication routes, every other route answers 404)
[CmdletBinding()]
param(
  [string]$Exe = 'C:\opt\python\gluesync-bootstrapper.exe',
  [int]$Port = 1717
)
$ErrorActionPreference = 'Stop'

$hubScript = {
  param($Port)
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
  $listener.Start()
  $password = 'admin'
  $changeRequired = $true
  while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
      $stream = $client.GetStream()
      $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::ASCII)
      $requestLine = $reader.ReadLine()
      if (-not $requestLine) { continue }
      $length = 0
      while ($true) {
        $line = $reader.ReadLine()
        if ($null -eq $line -or $line -eq '') { break }
        if ($line -match '^Content-Length:\s*(\d+)') { $length = [int]$Matches[1] }
      }
      $body = ''
      if ($length -gt 0) {
        $buffer = New-Object char[] $length
        $read = 0
        while ($read -lt $length) {
          $n = $reader.Read($buffer, $read, $length - $read)
          if ($n -le 0) { break }
          $read += $n
        }
        $body = -join $buffer[0..($read - 1)]
      }
      $parts = $requestLine -split ' '
      $method = $parts[0]
      $path = $parts[1]
      $status = 404
      $payload = '{"message":"unknown route"}'
      if ($method -eq 'POST' -and $path -eq '/authentication/login') {
        $data = $body | ConvertFrom-Json
        if ($data.password -ceq $password) {
          $status = 200
          $payload = (@{ apiToken = 'smoke-token'; changeRequired = $changeRequired } | ConvertTo-Json -Compress)
        } else {
          $status = 401
          $payload = '{"message":"bad credentials"}'
        }
      } elseif ($method -eq 'POST' -and $path -eq '/authentication/reset-password') {
        $data = $body | ConvertFrom-Json
        $password = [string]$data.newPassword
        $changeRequired = $false
        $status = 200
        $payload = '{}'
      }
      $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
      $head = "HTTP/1.1 $status Smoke`r`nContent-Type: application/json`r`nContent-Length: $($payloadBytes.Length)`r`nConnection: close`r`n`r`n"
      $headBytes = [System.Text.Encoding]::ASCII.GetBytes($head)
      $stream.Write($headBytes, 0, $headBytes.Length)
      $stream.Write($payloadBytes, 0, $payloadBytes.Length)
      $stream.Flush()
    } catch {
      # a probe or a reset connection: the next request is served normally
    } finally {
      $client.Close()
    }
  }
}

function Write-Step {
  param([string]$Message)
  Write-Host ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $Message)
}

function Start-FakeHub {
  Write-Step "starting the fake Core Hub on 127.0.0.1:$Port"
  $starter = if (Get-Command Start-ThreadJob -ErrorAction SilentlyContinue) { 'Start-ThreadJob' } else { 'Start-Job' }
  $job = & $starter -ScriptBlock $hubScript -ArgumentList $Port
  $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline) {
    if ($job.State -eq 'Failed') { throw "fake Core Hub failed to start: $($job.ChildJobs[0].JobStateInfo.Reason)" }
    try {
      $probe = New-Object System.Net.Sockets.TcpClient
      $probe.Connect('127.0.0.1', $Port)
      $probe.Close()
      Write-Step 'the fake Core Hub listens'
      return $job
    } catch {
      Start-Sleep -Milliseconds 300
    }
  }
  throw "fake Core Hub did not listen on 127.0.0.1:$Port within 30 seconds"
}

function Invoke-Exe {
  param([string[]]$ExeArgs, [int]$TimeoutSeconds = 120)
  $stdout = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N') + '.out')
  $stderr = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N') + '.err')
  $startArgs = @{ FilePath = $Exe; RedirectStandardOutput = $stdout; RedirectStandardError = $stderr; PassThru = $true; NoNewWindow = $true }
  if ($ExeArgs -and $ExeArgs.Count -gt 0) { $startArgs.ArgumentList = $ExeArgs }
  Write-Step "running: $Exe $($ExeArgs -join ' ')"
  $process = Start-Process @startArgs
  $null = $process.Handle
  if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    $process.Kill()
    throw "$Exe $($ExeArgs -join ' ') did not finish within $TimeoutSeconds seconds"
  }
  $process.WaitForExit()
  Write-Step "finished with exit code $($process.ExitCode)"
  $result = [pscustomobject]@{
    ExitCode = $process.ExitCode
    Output   = ((Get-Content -Raw -ErrorAction SilentlyContinue $stdout) + (Get-Content -Raw -ErrorAction SilentlyContinue $stderr))
  }
  Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
  return $result
}

function Assert-That {
  param([bool]$Condition, [string]$Message, $Result)
  if (-not $Condition) {
    if ($Result) { Write-Host '---- output ----'; Write-Host $Result.Output; Write-Host "---- exit code $($Result.ExitCode) ----" }
    throw "FAILED: $Message"
  }
  Write-Host "ok: $Message"
}

$env:CORE_HUB_URL = "http://127.0.0.1:$Port"
$env:HUB_ADMIN_PASSWORD = 'Test123!'
$env:DEFAULT_PASSWORD = 'Test123!'

$hub = Start-FakeHub
try {
  $first = Invoke-Exe @('hub-first-login')
  Assert-That ($first.ExitCode -eq 0) 'hub-first-login completes the first access on a fresh hub' $first
  Assert-That ($first.Output -match 'first access completed: 200') 'hub-first-login reports the password change' $first

  $again = Invoke-Exe @('hub-first-login')
  Assert-That ($again.ExitCode -eq 0) 'a second hub-first-login exits 0' $again
  Assert-That ($again.Output -match 'admin password already set') 'a second hub-first-login finds the password already set' $again

  # The bootstrapper command, with the Windows paths of the image (C:\opt\config\config.json and auth_token.json)
  $configDir = 'C:\opt\config'
  New-Item -ItemType Directory -Force -Path $configDir | Out-Null
  Set-Content -Path (Join-Path $configDir 'config.json') -Value '{}' -Encoding ascii
  $boot = Invoke-Exe @('--pipeline-name', 'smoke')
  Assert-That ($boot.Output -match 'Core Hub URL: http://127\.0\.0\.1:') 'the bootstrapper command loads main.py and reaches the Core Hub login' $boot
  Assert-That ($boot.Output -notmatch 'ModuleNotFoundError|ImportError|NameError') 'the bootstrapper command has no missing module' $boot
  Assert-That (Test-Path 'C:\opt\config\auth_token.json') 'the bootstrapper saved its token under C:\opt\config (the /opt/config of the Linux image)' $boot
  Write-Host "bootstrapper exit code against the fake hub: $($boot.ExitCode) (non-zero is expected: the fake hub cannot create a pipeline)"
}
finally {
  Stop-Job $hub -ErrorAction SilentlyContinue
  Remove-Job $hub -Force -ErrorAction SilentlyContinue
}
Write-Host 'in-container tests passed'
