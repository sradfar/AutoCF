param(
    [Parameter(Mandatory = $true)][string]$Repository,
    [string]$Tag = "runtime-assets-1.0.0"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $root "AutoCF-runtime-assets-1.0.0.zip"
python (Join-Path $PSScriptRoot "runtime_assets.py") create (Join-Path $root "windows\runtime") $archive
gh release view $Tag --repo $Repository *> $null
if ($LASTEXITCODE -ne 0) {
    gh release create $Tag --repo $Repository --title "AutoCF managed runtime assets 1.0.0" `
        --notes "Pinned runtime payload used by reproducible AutoCF desktop and Docker builds."
}
gh release upload $Tag $archive --repo $Repository --clobber

