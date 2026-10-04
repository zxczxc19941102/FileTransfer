# Sign dist\FileTransfer.exe with a free self-signed code-signing certificate.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$exe = Join-Path $root "dist\FileTransfer.exe"

if (-not (Test-Path $exe)) {
    Write-Output "[ERROR] exe not found: $exe"
    exit 1
}

# Locate the certificate: prefer thumbprint file, fall back to certificate store
$thumb = $null
$thumbFile = Join-Path $root "tools\cert-thumbprint.txt"
if (Test-Path $thumbFile) { $thumb = [IO.File]::ReadAllText($thumbFile).Trim() }

$cert = $null
if ($thumb) { $cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Thumbprint -eq $thumb } | Select-Object -First 1 }
if (-not $cert) {
    $cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -like "*FileTransfer Local*" } | Select-Object -First 1
}
if (-not $cert) {
    Write-Output "[ERROR] signing certificate not found. Run make_cert.ps1 first."
    exit 1
}

Write-Output "Certificate: $($cert.Subject)"
Write-Output "Thumbprint : $($cert.Thumbprint)"

try {
    $sig = Set-AuthenticodeSignature -FilePath $exe -Certificate $cert -HashAlgorithm SHA256 -TimestampServer "http://timestamp.digicert.com"
    Write-Output "Status      : $($sig.Status)"
    Write-Output "StatusMessage: $($sig.StatusMessage)"
    if ($sig.TimeStamperCertificate) { Write-Output "Timestamp   : $($sig.TimeStamperCertificate.Subject)" }
} catch {
    Write-Output "[ERROR] $($_.Exception.Message)"
    exit 1
}