# 生成免费的自签名代码签名证书，并导出 PFX / CER
$ErrorActionPreference = "Stop"
$dir = Join-Path $PSScriptRoot "tools"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$existing = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -like "*FileTransfer Local*" } | Select-Object -First 1
if ($existing -and $existing.NotAfter -gt (Get-Date)) {
    Write-Host "证书已存在，指纹: $($existing.Thumbprint)"
    $cert = $existing
} else {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=FileTransfer Local, O=FileTransfer, C=CN" -CertStoreLocation "Cert:\CurrentUser\My" -KeyUsage DigitalSignature -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 -NotBefore (Get-Date).AddDays(-1) -NotAfter (Get-Date).AddYears(3)
    Write-Host "已创建证书，指纹: $($cert.Thumbprint)"
}

[IO.File]::WriteAllText((Join-Path $dir "cert-thumbprint.txt"), $cert.Thumbprint)
Export-Certificate -Cert $cert -FilePath (Join-Path $dir "FileTransfer-CodeSign.cer") | Out-Null

$pwdFile = Join-Path $dir "pfx-password.txt"
if (Test-Path $pwdFile) {
    $plain = [IO.File]::ReadAllText($pwdFile).Trim()
} else {
    $chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@"
    $plain = -join (1..20 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
    [IO.File]::WriteAllText($pwdFile, $plain)
}
$sec = ConvertTo-SecureString -String $plain -AsPlainText -Force
Export-PfxCertificate -Cert $cert -FilePath (Join-Path $dir "FileTransfer-CodeSign.pfx") -Password $sec | Out-Null

Write-Host "证书用途: $($cert.EnhancedKeyUsageList.ObjectId.FriendlyName)"
Write-Host "有效期至: $($cert.NotAfter)"
Write-Host "已导出 tools\FileTransfer-CodeSign.cer 与 .pfx（密码见 tools\pfx-password.txt）"
Write-Host "把 .cer 导入受信任根证书（当前用户）后，本机双击 exe 不再提示未知发布者。"