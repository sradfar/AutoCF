$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$checksumFile = Join-Path $root 'SHA256SUMS.txt'
$required = @(
    'AutoCF-Desktop.exe',
    'Install-AutoCF.cmd',
    'AutoCF.cmd',
    'windows\AutoCF-Windows.ps1',
    'windows\runtime\autocf-wsl-rootfs.tar.gz',
    'windows\runtime\sfincs-cpu-rootfs.tar.gz',
    'windows\runtime\sfincs-cpu-launcher.sh',
    'windows\runtime\Transformez-Helper.exe'
)

$failures = [System.Collections.Generic.List[string]]::new()
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relative) -PathType Leaf)) {
        $failures.Add("Missing required file: $relative")
    }
}
if (-not (Test-Path -LiteralPath $checksumFile -PathType Leaf)) {
    $failures.Add('Missing checksum manifest: SHA256SUMS.txt')
}

if ($failures.Count -eq 0) {
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $checksumFile) {
        $lineNumber++
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9A-Fa-f]{64})  (.+)$') {
            $failures.Add("Malformed checksum line $lineNumber")
            continue
        }
        $expected = $Matches[1].ToUpperInvariant()
        $relative = $Matches[2].Replace('/', [IO.Path]::DirectorySeparatorChar)
        $path = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $failures.Add("Missing checksummed file: $relative")
            continue
        }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash
        if ($actual -ne $expected) {
            $failures.Add("Checksum mismatch: $relative")
        }
    }
}

if ($failures.Count) {
    Write-Host 'AutoCF package verification: FAILED' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}

Write-Host 'AutoCF package verification: PASS' -ForegroundColor Green
Write-Host 'All required files are present and every recorded SHA-256 matches.'
