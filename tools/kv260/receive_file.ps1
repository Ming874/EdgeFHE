# Copy a file from the KV260 to this PC over the serial console.
# The board prints its SHA-256 and the base64 text between two markers; this script
# decodes it, writes LocalFile and compares the SHA-256.
param(
  [string]$Port = 'COM8',
  [Parameter(Mandatory = $true)][string]$RemotePath,
  [Parameter(Mandatory = $true)][string]$LocalFile,
  [int]$TimeoutSec = 600,
  [int]$Baud = 115200
)
$ErrorActionPreference = 'Stop'
$p = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$p.Encoding = [System.Text.Encoding]::ASCII
$p.ReadTimeout = 200
$p.DtrEnable = $false
$p.RtsEnable = $false
$p.ReadBufferSize = 1048576
$p.Open()
try {
  [void]$p.ReadExisting()
  $command = "sha256sum $RemotePath | cut -c1-64 | sed 's/^/__SHA=/'; echo __B64BEGIN__; base64 -w 0 $RemotePath; echo; echo __B64END__"
  $p.Write($command + "`r")
  $buffer = New-Object System.Text.StringBuilder
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $done = $false
  while ((Get-Date) -lt $deadline) {
    $text = $p.ReadExisting()
    if ($text) {
      [void]$buffer.Append($text)
      if ($buffer.Length -gt 40 -and $buffer.ToString($buffer.Length - [Math]::Min(200, $buffer.Length), [Math]::Min(200, $buffer.Length)) -match "`n__B64END__") { $done = $true; break }
    } else { Start-Sleep -Milliseconds 40 }
  }
  if (-not $done) { Write-Output "RECEIVE_TIMEOUT after $TimeoutSec s"; exit 2 }
  $all = $buffer.ToString() -replace "`r", ''
  if ($all -notmatch "`n__SHA=([0-9a-f]{64})") { Write-Output 'RECEIVE_NO_SHA'; exit 1 }
  $sha = $Matches[1]
  $begin = $all.IndexOf("`n__B64BEGIN__`n")
  $end = $all.IndexOf("`n__B64END__")
  $b64 = $all.Substring($begin + 14, $end - $begin - 14).Trim()
  $bytes = [Convert]::FromBase64String($b64)
  [System.IO.File]::WriteAllBytes($LocalFile, $bytes)
  $local = (Get-FileHash -Algorithm SHA256 -Path $LocalFile).Hash.ToLower()
  if ($local -eq $sha) { Write-Output "RECEIVE_OK $LocalFile $($bytes.Length) bytes sha256 $sha"; exit 0 }
  Write-Output "RECEIVE_MISMATCH board=$sha local=$local"
  exit 1
} finally {
  if ($p.IsOpen) { $p.Close() }
}
