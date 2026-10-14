# Copy a local file to the KV260 over the serial console (no network needed).
# The shell on the board reads exactly N base64 characters with echo and line editing
# off, decodes them, restores the terminal and prints the SHA-256, which is compared
# with the local SHA-256.
param(
  [string]$Port = 'COM8',
  [Parameter(Mandatory = $true)][string]$LocalFile,
  [Parameter(Mandatory = $true)][string]$RemotePath,
  [int]$Baud = 115200
)
$ErrorActionPreference = 'Stop'
$bytes = [System.IO.File]::ReadAllBytes($LocalFile)
$b64 = [Convert]::ToBase64String($bytes)
$sha = (Get-FileHash -Algorithm SHA256 -Path $LocalFile).Hash.ToLower()
$n = $b64.Length
Write-Output "sending $($bytes.Length) bytes as $n base64 characters, sha256 $sha"

$p = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$p.Encoding = [System.Text.Encoding]::ASCII
$p.ReadTimeout = 200
$p.WriteTimeout = 600000
$p.DtrEnable = $false
$p.RtsEnable = $false
$p.Open()
try {
  [void]$p.ReadExisting()
  $command = "stty -echo -icanon; head -c $n | base64 -d > $RemotePath; stty echo icanon; sha256sum $RemotePath; echo `"__RC=`$?__`""
  $p.Write($command + "`r")
  Start-Sleep -Milliseconds 800
  [void]$p.ReadExisting()
  $started = Get-Date
  $chunk = 2048
  for ($i = 0; $i -lt $n; $i += $chunk) {
    $p.Write($b64.Substring($i, [Math]::Min($chunk, $n - $i)))
    if ((($i / $chunk) % 200) -eq 0 -and $i -gt 0) {
      $pct = [Math]::Round(100.0 * $i / $n, 1)
      $rate = [Math]::Round($i / ((Get-Date) - $started).TotalSeconds / 1024, 1)
      Write-Output "  $pct% ($rate KiB/s)"
      [void]$p.ReadExisting()
    }
  }
  $buffer = New-Object System.Text.StringBuilder
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline) {
    $text = $p.ReadExisting()
    if ($text) { [void]$buffer.Append($text); if ($buffer.ToString() -match '__RC=(\d+)__') { break } } else { Start-Sleep -Milliseconds 50 }
  }
  $out = $buffer.ToString() -replace "`r", ''
  Write-Output $out
  $seconds = [Math]::Round(((Get-Date) - $started).TotalSeconds, 1)
  if ($out -match "([0-9a-f]{64})\s+$([regex]::Escape($RemotePath))" -and $Matches[1] -eq $sha) {
    Write-Output "TRANSFER_OK $RemotePath in $seconds s"
    exit 0
  }
  Write-Output "TRANSFER_MISMATCH (expected $sha)"
  exit 1
} finally {
  if ($p.IsOpen) { $p.Close() }
}
