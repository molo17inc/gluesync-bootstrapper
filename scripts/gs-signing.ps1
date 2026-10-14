# Code signing for the Windows installer (1.3.4): DigiCert KeyLocker (DigiCert ONE Software Trust Manager) + signtool.
# Dot-sourced by scripts/build-windows-installer.ps1 -Sign. The private key never leaves the DigiCert HSM: smctl syncs the
# KSP-backed certificate into CurrentUser\My and signtool signs through the KSP (/sha1 <thumbprint>).
#
# CI variables (project 75734286, environment scope "signing", protected + masked):
#   SM_HOST, SM_API_KEY, SM_CLIENT_CERT_FILE (File variable: the client-auth .p12, base64 text or raw), SM_CLIENT_CERT_PASSWORD,
#   SM_CODE_SIGNING_CERT_SHA1_HASH (signer thumbprint), DIGICERT_KEYPAIR_ALIAS (keypair alias for smctl certsync).
# Optional: SIGNTOOL_PATH, SMCTL_PATH (tool locations), GS_SIGN_TSA (first timestamp server).
#
# Order (build-windows-installer.ps1): every .ps1 first (before ps2exe and candle/light, so the EXE and the MSI cabinet
# carry signed scripts), then the EXE, then the MSI. All signatures: SHA-256 file digest + RFC 3161 timestamp.

$script:GsSignTsa = @($(if ($env:GS_SIGN_TSA) { $env:GS_SIGN_TSA }), 'http://timestamp.digicert.com', 'http://timestamp.sectigo.com') | Where-Object { $_ } | Select-Object -Unique
$script:GsSignState = $null

function Find-GsSignTool {
  if ($env:SIGNTOOL_PATH -and (Test-Path -LiteralPath $env:SIGNTOOL_PATH)) { return $env:SIGNTOOL_PATH }
  $cands = @(Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue | Sort-Object { [version]($_.Directory.Parent.Name -replace '[^0-9.]', '') } -Descending)
  if ($cands.Count) { return $cands[0].FullName }
  $c = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  throw 'signtool.exe not found (Windows SDK). Install the Windows 10/11 SDK signing tools on the signing runner or set SIGNTOOL_PATH.'
}

function Find-GsSmctl {
  if ($env:SMCTL_PATH -and (Test-Path -LiteralPath $env:SMCTL_PATH)) { return $env:SMCTL_PATH }
  foreach ($p in @("$env:ProgramFiles\DigiCert\DigiCert One Signing Manager Tools\smctl.exe", "${env:ProgramFiles(x86)}\DigiCert\DigiCert One Signing Manager Tools\smctl.exe")) {
    if (Test-Path -LiteralPath $p) { return $p }
  }
  $c = Get-Command smctl.exe -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  throw 'smctl.exe not found (DigiCert smtools). Install smtools-windows-x64.msi on the signing runner or set SMCTL_PATH.'
}

function Invoke-GsTool([string]$Exe, [string[]]$ToolArgs, [string]$What) {
  $out = & $Exe @ToolArgs 2>&1 | ForEach-Object { "$_" }
  $code = $LASTEXITCODE
  # never print secrets: smctl/signtool output does not contain them, but mask the API key just in case
  $text = ($out -join "`n"); if ($env:SM_API_KEY) { $text = $text.Replace($env:SM_API_KEY, '[masked]') }
  Write-Host "  $What (exit $code)"; if ($text) { Write-Host ($text -replace '(?m)^', '    ') }
  return @{ Code = $code; Text = $text }
}

# Checks the variables, decodes the client certificate to a temporary .p12 (base64 File variable), runs smctl healthcheck
# and syncs the KSP-backed signing certificate into CurrentUser\My. Throws on any problem: a signing build never falls
# back to unsigned output.
function Initialize-GsSigning {
  foreach ($n in 'SM_HOST', 'SM_API_KEY', 'SM_CLIENT_CERT_FILE', 'SM_CLIENT_CERT_PASSWORD', 'SM_CODE_SIGNING_CERT_SHA1_HASH', 'DIGICERT_KEYPAIR_ALIAS') {
    if (-not [Environment]::GetEnvironmentVariable($n)) { throw "Signing requested but $n is not set (signing jobs run only on protected refs, environment 'signing')." }
  }
  $state = @{ SignTool = (Find-GsSignTool); Smctl = (Find-GsSmctl); Thumbprint = $env:SM_CODE_SIGNING_CERT_SHA1_HASH.Trim().ToUpperInvariant(); TempP12 = $null; OrigCertFile = $env:SM_CLIENT_CERT_FILE; Signed = New-Object System.Collections.Generic.List[string] }
  Write-Host "Signing tools: signtool $($state.SignTool); smctl $($state.Smctl)"
  # A GitLab File variable is a path; the content is the base64 text of the .p12 (or the raw .p12).
  $raw = [IO.File]::ReadAllBytes($env:SM_CLIENT_CERT_FILE)
  $txt = [Text.Encoding]::ASCII.GetString($raw).Trim()
  if ($txt -match '^[A-Za-z0-9+/=\r\n]+$' -and $txt.Length -gt 100) {
    $p12 = Join-Path ([IO.Path]::GetTempPath()) ("gs-sm-client-{0}.p12" -f [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllBytes($p12, [Convert]::FromBase64String(($txt -replace '\s', '')))
    $state.TempP12 = $p12; $env:SM_CLIENT_CERT_FILE = $p12
  }
  $script:GsSignState = $state
  $r = Invoke-GsTool $state.Smctl @('healthcheck') 'smctl healthcheck'
  if ($r.Code -ne 0) { throw "smctl healthcheck failed (exit $($r.Code)): check SM_HOST, SM_API_KEY and the client certificate." }
  # signtool needs the DigiCert KSP for the private-key operation; without it (only smctl.exe installed, no full smtools
  # package) sign with smctl --simple (DigiCert's own Authenticode signer, no KSP/signtool). rc.1: signtool found the cert
  # but "No certificates were found that met all the given criteria" (no KSP).
  # (smctl windows ksp register needs administrator rights: BCryptRegisterProvider 0xc0000022 under the runner account, rc.2)
  $k = Invoke-GsTool $state.Smctl @('windows', 'ksp', 'list') 'smctl windows ksp list'
  $state.Backend = $(if ($env:GS_SIGN_BACKEND) { $env:GS_SIGN_BACKEND } elseif ($k.Text -match '(?i)DigiCert') { 'signtool' } else { 'smctl-simple' })
  Write-Host "Signing backend: $($state.Backend)"
  $r = Invoke-GsTool $state.Smctl @('windows', 'certsync', "--keypair-alias=$($env:DIGICERT_KEYPAIR_ALIAS)") 'smctl windows certsync'
  if ($r.Code -ne 0) { throw "smctl windows certsync failed (exit $($r.Code))." }
  $cert = Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue | Where-Object { $_.Thumbprint -eq $state.Thumbprint } | Select-Object -First 1
  if (-not $cert) { throw "Signing certificate $($state.Thumbprint) is not in CurrentUser\My after certsync." }
  if ($cert.NotAfter -lt (Get-Date)) { throw "Signing certificate $($state.Thumbprint) expired on $($cert.NotAfter)." }
  Write-Host "Signer: $($cert.Subject) (valid to $($cert.NotAfter.ToString('yyyy-MM-dd')), thumbprint $($state.Thumbprint))"
  $state.Subject = $cert.Subject
}

# Signs each file (exe, msi, ps1, psm1, psd1) with SHA-256 and an RFC 3161 timestamp; tries the next timestamp server when
# one fails. Throws if any file cannot be signed.
function Invoke-GsSign([string[]]$Path, [string]$Description = 'Gluesync platform installer') {
  $st = $script:GsSignState
  if (-not $st) { throw 'Invoke-GsSign called before Initialize-GsSigning.' }
  foreach ($p in $Path) {
    $f = (Resolve-Path -LiteralPath $p).Path
    if ($f -match '\.ps[dm]?1$') {
      # Windows PowerShell 5.1 reads a script without a BOM as ANSI: with non-ASCII bytes the signed hash then does not
      # match (HashMismatch, docker-installer-windows-offline.ps1 in v1.3.4-rc.4) and the text is garbled. Save as UTF-8 with BOM.
      $bytes = [IO.File]::ReadAllBytes($f)
      $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
      if (-not $bom -and @($bytes | Where-Object { $_ -gt 127 } | Select-Object -First 1).Count) { throw "$f has non-ASCII characters but no UTF-8 BOM: save it as UTF-8 with BOM before signing." }
    }
    $done = $false; $last = ''
    foreach ($tsa in $script:GsSignTsa) {
      if ($st.Backend -eq 'smctl-simple') {
        $a = @('sign', '--simple', "--keypair-alias=$($env:DIGICERT_KEYPAIR_ALIAS)", '--input', $f, '--ts-server', $tsa, '--digalg', 'SHA256')
        $r = Invoke-GsTool $st.Smctl $a "smctl sign --simple $(Split-Path -Leaf $f) (TSA $tsa)"
        # smctl can exit 0 and report the failure only in its output: trust the file's signature, not the exit code
        $sig = Get-AuthenticodeSignature -LiteralPath $f
        if ($r.Code -eq 0 -and $sig.SignerCertificate -and $sig.SignerCertificate.Thumbprint -eq $st.Thumbprint) { $done = $true; break }
        $last = "$($r.Text) [signature after smctl: $($sig.Status)]"
      }
      else {
        $a = @('sign', '/sha1', $st.Thumbprint, '/fd', 'sha256', '/tr', $tsa, '/td', 'sha256', '/d', $Description, '/du', 'https://molo17.com', $f)
        $r = Invoke-GsTool $st.SignTool $a "signtool sign $(Split-Path -Leaf $f) (TSA $tsa)"
        if ($r.Code -eq 0) { $done = $true; break }
        $last = $r.Text
      }
    }
    if (-not $done) { throw "Signing failed for $f with every timestamp server: $last" }
    $st.Signed.Add($f)
  }
}

# Removes the decoded client certificate and the synced public certificate. Always called from finally.
function Close-GsSigning {
  $st = $script:GsSignState
  if (-not $st) { return }
  if ($st.TempP12 -and (Test-Path -LiteralPath $st.TempP12)) { Remove-Item -LiteralPath $st.TempP12 -Force -ErrorAction SilentlyContinue }
  if ($st.OrigCertFile) { $env:SM_CLIENT_CERT_FILE = $st.OrigCertFile }
  Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue | Where-Object { $_.Thumbprint -eq $st.Thumbprint } | Remove-Item -ErrorAction SilentlyContinue
  Write-Host "Signing session closed: $($st.Signed.Count) file(s) signed."
  $script:GsSignState = $null
}
