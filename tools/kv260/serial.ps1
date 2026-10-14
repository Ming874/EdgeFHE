# Talk to the KV260 serial console from Windows.
#   -Send TEXT [-Enter]        write text (optionally followed by Enter)
#   -Command CMD               run a shell command and wait for its exit code marker
#   -Expect REGEX              stop reading when the output matches
#   -Listen                    only read (no write)
# Prints everything received (ANSI escapes removed). Exit code 0, or 2 on timeout.
param(
  [string]$Port = 'COM8',
  [string]$Send = $null,
  [switch]$Enter,
  [string]$Command = $null,
  [string]$CommandFile = $null,
  [string]$Expect = $null,
  [int]$TimeoutSec = 10,
  [switch]$Listen,
  [int]$Baud = 115200
)
$ErrorActionPreference = 'Stop'
$p = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$p.Encoding = New-Object System.Text.UTF8Encoding $false
$p.ReadTimeout = 200
$p.WriteTimeout = 5000
$p.DtrEnable = $false
$p.RtsEnable = $false
$p.Open()
$code = 0
try {
  [void]$p.ReadExisting()  # drop anything stale
  if ($CommandFile) { $Command = ([System.IO.File]::ReadAllText($CommandFile)).Trim() -replace "`r?`n", ' ' }
  if ($Command) {
    $Send = $Command + '; echo "__RC=$?__"'
    $Enter = $true
    if (-not $Expect) { $Expect = '__RC=(\d+)__' }
  }
  $text = ''
  if ($Send) { $text = $Send }
  if ($Enter) { $text += "`r" }
  if (-not $Listen -and $text.Length -gt 0) {
    # Write in small pieces so the board's tty input buffer never overflows.
    for ($i = 0; $i -lt $text.Length; $i += 256) {
      $p.Write($text.Substring($i, [Math]::Min(256, $text.Length - $i)))
      Start-Sleep -Milliseconds 25
    }
  }
  $buffer = New-Object System.Text.StringBuilder
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $matched = $false
  while ((Get-Date) -lt $deadline) {
    $chunk = $p.ReadExisting()
    if ($chunk) {
      [void]$buffer.Append($chunk)
      if ($Expect -and ($buffer.ToString() -match $Expect)) { $matched = $true; Start-Sleep -Milliseconds 150; [void]$buffer.Append($p.ReadExisting()); break }
    } else {
      Start-Sleep -Milliseconds 40
    }
  }
  $out = [regex]::Replace($buffer.ToString(), "\x1B\[[0-9;?]*[ -/]*[@-~]|\x1B[()][A-Za-z0-9]|\x07", '')
  $out = $out -replace "`r", ''
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  Write-Output $out
  if ($Expect -and -not $matched) { Write-Output "<<TIMEOUT after $TimeoutSec s waiting for: $Expect>>"; $code = 2 }
  if ($Command -and $matched -and ($out -match '__RC=(\d+)__')) { Write-Output "<<EXIT $($Matches[1])>>" }
} finally {
  if ($p.IsOpen) { $p.Close() }
}
exit $code
