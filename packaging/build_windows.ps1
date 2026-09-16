param([string]$Version = "1.0.0")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

python -m pip install --upgrade pip
python -m pip install -r packaging/requirements-build.txt
$env:AUTOCF_VERSION = $Version
Remove-Item -LiteralPath build,dist -Recurse -Force -ErrorAction SilentlyContinue
python -m PyInstaller --noconfirm --clean packaging/autocf_desktop.spec

$exe = Join-Path $root "dist\AutoCF-Desktop.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Windows executable was not produced." }
if ((Get-Item -LiteralPath $exe).Length -lt 150MB) {
    throw "Incomplete Windows Qt bundle: executable is smaller than 150 MB."
}
$release = Join-Path $root "dist\AutoCF-$Version-Windows-x64"
New-Item -ItemType Directory -Force -Path $release | Out-Null
Copy-Item -LiteralPath $exe -Destination (Join-Path $release "AutoCF-Desktop.exe") -Force
foreach ($name in @("app","windows","host","vendor","data","examples","logo")) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $release -Recurse -Force
}
foreach ($name in @("README.md","RELEASE-NOTES.txt","START-HERE.txt","AutoCF.cmd","Install-AutoCF.cmd")) {
    if (Test-Path -LiteralPath (Join-Path $root $name)) {
        Copy-Item -LiteralPath (Join-Path $root $name) -Destination $release -Force
    }
}
$zip = Join-Path $root "dist\AutoCF-$Version-Windows-x64.zip"
Compress-Archive -LiteralPath $release -DestinationPath $zip -Force
(Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash + "  " + (Split-Path -Leaf $zip) |
    Set-Content -LiteralPath "$zip.sha256" -Encoding ascii

