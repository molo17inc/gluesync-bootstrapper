# Signs the Windows Automator (gluesync-automator-windows.exe, the PyInstaller build from automator_build_windows) with the
# MOLO17 SRL code-signing certificate, then verifies it. Runs in the protected signing job automator_sign_windows (runner
# hq-windows-sign, environment "signing"; same DigiCert KeyLocker setup as the platform installer, scripts/gs-signing.ps1).
# Signed in place, so automator_publish uploads the signed copy from this job's artifacts.
# Writes .automator-build/dist/automator-signatures.json; automator_publish refuses to upload unless the exe is listed as valid.
# Fails (exit 1) on an unsigned, invalid, untimestamped or foreign-signed exe.
[CmdletBinding()]
param([string]$Exe = '', [string]$ExpectedSubject = 'CN=MOLO17*', [string]$ExpectedThumbprint = $env:SM_CODE_SIGNING_CERT_SHA1_HASH)
$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$root = (Resolve-Path -LiteralPath (Join-Path $here '..')).Path
if (-not $Exe) { $Exe = Join-Path $root '.automator-build\dist\gluesync-automator-windows.exe' }
if (-not (Test-Path -LiteralPath $Exe)) { throw "Automator exe missing: $Exe (artifacts of automator_build_windows?)" }
$Exe = (Resolve-Path -LiteralPath $Exe).Path
. (Join-Path $here 'gs-signing.ps1')

try {
  Initialize-GsSigning
  Invoke-GsSign -Path @($Exe) -Description 'Gluesync Automator'
}
finally { Close-GsSigning }

$s = Get-AuthenticodeSignature -LiteralPath $Exe
$info = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($Exe)
$rec = [ordered]@{
  file = Split-Path -Leaf $Exe; productVersion = $info.ProductVersion; status = "$($s.Status)"; signer = ''; thumbprint = ''; timestamp = ''
  sha256 = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant(); problems = @()
}
if ($s.SignerCertificate) { $rec.signer = $s.SignerCertificate.Subject; $rec.thumbprint = $s.SignerCertificate.Thumbprint }
if ($s.TimeStamperCertificate) { $rec.timestamp = $s.TimeStamperCertificate.Subject }
if ($s.Status -ne 'Valid') { $rec.problems += "status $($s.Status): $($s.StatusMessage)" }
if ($rec.signer -notlike $ExpectedSubject) { $rec.problems += "signer '$($rec.signer)' is not $ExpectedSubject" }
if ($ExpectedThumbprint -and $rec.thumbprint -ne $ExpectedThumbprint.Trim().ToUpperInvariant()) { $rec.problems += 'not the configured signing certificate' }
if (-not $rec.timestamp) { $rec.problems += 'no RFC 3161 timestamp' }
ConvertTo-Json -InputObject @([pscustomobject]$rec) -Depth 4 | Set-Content -LiteralPath (Join-Path (Split-Path -Parent $Exe) 'automator-signatures.json') -Encoding UTF8
if ($rec.problems.Count) { Write-Host "FAIL  $($rec.file): $($rec.problems -join '; ')" -ForegroundColor Red; exit 1 }
Write-Host "OK    $($rec.file) $($rec.productVersion): $($rec.signer -replace ',.*$', ''), timestamp $($rec.timestamp -replace ',.*$', ''), sha256 $($rec.sha256)"
exit 0
