[CmdletBinding()]
param(
    [ValidateSet('Install', 'Status', 'Run', 'Evaluate', 'Impact', 'ForcingPreview', 'AnalysisPrepare', 'AnalysisPrepareExposure', 'AnalysisRun', 'AnalysisReport')]
    [string]$Action = 'Status',

    [string]$Config,

    [ValidateSet('build', 'run', 'full', 'terrain', 'surface', 'atmosphere')]
    [string]$Mode = 'full',

    [ValidateRange(0, 1024)]
    [int]$CpuThreads = 0,

    [ValidateSet('auto', 'cpu', 'gpu')]
    [string]$ComputeBackend = 'auto',

    [double]$EvaluationToleranceMinutes = 4.0,

    [double]$EvaluationHmaxVmax = 3.0,

    [switch]$SkipEvaluationHmax,

    [switch]$SkipEvaluationObservations,

    [string]$ImpactThresholds = '0.15,0.3,0.6,1.0,2.0',

    [double]$ImpactAnalysisResolution = 30.0,

    [switch]$SkipImpactBuildings,

    [switch]$SkipImpactRoads,

    [switch]$SkipImpactPopulation,

    [switch]$ForceImpactDownloads,

    [switch]$SkipAnalysisPopulation,

    [switch]$ForceAnalysis,

    [switch]$ForceReinstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

$DistroName = 'AutoCF'
$WindowsDir = $PSScriptRoot
$PackageRoot = (Resolve-Path (Join-Path $WindowsDir '..')).Path
$RuntimeDir = Join-Path $WindowsDir 'runtime'
$RootfsArchive = Join-Path $RuntimeDir 'autocf-wsl-rootfs.tar.gz'
$SfincsCpuRootfs = Join-Path $RuntimeDir 'sfincs-cpu-rootfs.tar.gz'
$SfincsCpuLauncher = Join-Path $RuntimeDir 'sfincs-cpu-launcher.sh'
$SfincsGpuRootfs = Join-Path $RuntimeDir 'sfincs-gpu-rootfs.tar.gz'
$SfincsGpuLauncher = Join-Path $RuntimeDir 'sfincs-gpu-launcher.sh'
$SfincsGpuManifest = Join-Path $RuntimeDir 'sfincs-gpu-manifest.json'
$PythonWheels = Join-Path $RuntimeDir 'wheels'
$TransformezHelper = Join-Path $RuntimeDir 'Transformez-Helper.exe'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'AutoCF\WSL'

function Write-Heading([string]$Text) {
    Write-Host ''
    Write-Host ('=' * 68) -ForegroundColor DarkCyan
    Write-Host (' ' + $Text) -ForegroundColor Cyan
    Write-Host ('=' * 68) -ForegroundColor DarkCyan
}

function Convert-ToWslPath([string]$WindowsPath) {
    $full = [System.IO.Path]::GetFullPath($WindowsPath)
    if ($full -match '^([A-Za-z]):\\(.*)$') {
        $drive = $Matches[1].ToLowerInvariant()
        $tail = $Matches[2].Replace('\', '/')
        return "/mnt/$drive/$tail"
    }
    throw "AutoCF currently requires package and configuration files on a local Windows drive: $full"
}

function Invoke-WslProbe([string]$Arguments) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = (Join-Path $env:SystemRoot 'System32\wsl.exe')
    $start.Arguments = $Arguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        StdOut = ($stdout -replace "`0", '')
        StdErr = ($stderr -replace "`0", '')
    }
}

function Test-WslEnabled {
    return (Invoke-WslProbe '--status').ExitCode -eq 0
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-WslDistros {
    $probe = Invoke-WslProbe '--list --quiet'
    if ($probe.ExitCode -ne 0) {
        return @()
    }
    return @($probe.StdOut -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Test-DistroInstalled {
    return (Get-WslDistros) -contains $DistroName
}

function Test-GpuPayloadPackaged {
    return (Test-Path -LiteralPath $SfincsGpuRootfs -PathType Leaf) -and `
        (Test-Path -LiteralPath $SfincsGpuLauncher -PathType Leaf) -and `
        (Test-Path -LiteralPath $SfincsGpuManifest -PathType Leaf)
}

function Test-NvidiaGpuVisible {
    if (-not (Test-DistroInstalled)) { return $false }
    & wsl.exe -d $DistroName -u root -- /bin/sh -lc `
        'test -e /dev/dxg || command -v nvidia-smi >/dev/null 2>&1' 2>$null
    return $LASTEXITCODE -eq 0
}

function Test-InstalledGpuRuntime {
    if (-not (Test-DistroInstalled)) { return $false }
    & wsl.exe -d $DistroName -u root -- /usr/bin/test -x /usr/local/bin/sfincs-gpu 2>$null
    return $LASTEXITCODE -eq 0
}

function Resolve-SfincsExecutable {
    $gpuReady = (Test-InstalledGpuRuntime) -and (Test-NvidiaGpuVisible)
    if ($ComputeBackend -eq 'gpu' -and -not $gpuReady) {
        throw 'NVIDIA GPU was requested, but compatible hardware and a validated SFINCS GPU payload are not both available. Select CPU or Automatic.'
    }
    if ($ComputeBackend -eq 'gpu' -or ($ComputeBackend -eq 'auto' -and $gpuReady)) {
        Write-Host 'SFINCS compute: NVIDIA GPU (validated payload)' -ForegroundColor Green
        return '/usr/local/bin/sfincs-gpu'
    }
    if ($ComputeBackend -eq 'auto') {
        Write-Host 'SFINCS compute: CPU (no validated GPU runtime detected)' -ForegroundColor Yellow
    }
    else {
        Write-Host 'SFINCS compute: CPU (explicit selection)'
    }
    return '/usr/local/bin/sfincs-cpu'
}

function Invoke-WslCommand([string[]]$Arguments) {
    & wsl.exe -d $DistroName -u root -- @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "AutoCF Linux runtime command failed with exit code $LASTEXITCODE."
    }
}

function Install-AutoCFRuntime {
    Write-Heading 'AutoCF Windows runtime installation'

    foreach ($required in @($RootfsArchive, $SfincsCpuRootfs, $SfincsCpuLauncher)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Required bundled runtime is missing: $required"
        }
    }
    if (-not (Test-Path -LiteralPath $PythonWheels -PathType Container)) {
        throw "Required bundled Python components are missing: $PythonWheels"
    }

    if (-not (Test-WslEnabled)) {
        if (-not (Test-IsAdministrator)) {
            Write-Host 'Administrator permission is required once to enable WSL2.' -ForegroundColor Yellow
            $argumentLine = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action Install"
            $elevated = Start-Process `
                -FilePath 'powershell.exe' `
                -Verb RunAs `
                -ArgumentList $argumentLine `
                -Wait `
                -PassThru
            exit $elevated.ExitCode
        }
        Write-Host 'WSL2 is not enabled. AutoCF will ask Windows to enable it.' -ForegroundColor Yellow
        Write-Host 'A Windows restart may be required; the AutoCF UI will guide the next step.' -ForegroundColor Yellow
        & wsl.exe --install --no-distribution
        if ($LASTEXITCODE -ne 0) {
            throw 'Windows could not enable WSL automatically. Select Install / repair runtime in AutoCF and approve the administrator prompt.'
        }
        Write-Host 'WSL was enabled. Restart Windows from AutoCF, reopen the app, and continue installation.' -ForegroundColor Yellow
        exit 3010
    }

    $importRuntime = -not (Test-DistroInstalled)
    if (-not $importRuntime) {
        if (-not $ForceReinstall) {
            Write-Host 'Updating the installed AutoCF runtime for CPU execution...'
        }
        else {
        Write-Host 'Replacing the existing AutoCF runtime...' -ForegroundColor Yellow
        & wsl.exe --terminate $DistroName 2>$null
        & wsl.exe --unregister $DistroName
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not remove the previous AutoCF WSL runtime.'
        }
            $importRuntime = $true
        }
    }

    if ($importRuntime) {
        New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
        Write-Host 'Importing the bundled AutoCF/HydroMT/GDAL runtime...'
        & wsl.exe --import $DistroName $InstallRoot $RootfsArchive --version 2
        if ($LASTEXITCODE -ne 0) {
            throw 'WSL could not import the bundled AutoCF runtime.'
        }
    }

    try {
        $cpuRootfsWsl = Convert-ToWslPath $SfincsCpuRootfs
        $cpuLauncherWsl = Convert-ToWslPath $SfincsCpuLauncher
        Write-Host 'Adding the bundled SFINCS CPU runtime...'
        Invoke-WslCommand @('/bin/mkdir', '-p', '/opt/sfincs-cpu/rootfs')
        Invoke-WslCommand @('/bin/tar', '-xzf', $cpuRootfsWsl, '-C', '/opt/sfincs-cpu/rootfs')
        Invoke-WslCommand @('/usr/bin/install', '-m', '0755', $cpuLauncherWsl, '/usr/local/bin/sfincs-cpu')

        if (Test-GpuPayloadPackaged) {
            $gpuRootfsWsl = Convert-ToWslPath $SfincsGpuRootfs
            $gpuLauncherWsl = Convert-ToWslPath $SfincsGpuLauncher
            Write-Host 'Adding the validated SFINCS GPU runtime...'
            Invoke-WslCommand @('/bin/mkdir', '-p', '/opt/sfincs-gpu/rootfs')
            Invoke-WslCommand @('/bin/tar', '-xzf', $gpuRootfsWsl, '-C', '/opt/sfincs-gpu/rootfs')
            Invoke-WslCommand @('/usr/bin/install', '-m', '0755', $gpuLauncherWsl, '/usr/local/bin/sfincs-gpu')
        }
        else {
            Write-Host 'SFINCS GPU payload: NOT PACKAGED; Automatic mode will use CPU.' -ForegroundColor Yellow
        }

        $pythonWheelsWsl = Convert-ToWslPath $PythonWheels
        Write-Host 'Installing bundled evaluation and impact components...'
        Invoke-WslCommand @(
            '/usr/local/bin/python', '-m', 'pip', 'install',
            '--no-index', '--find-links', $pythonWheelsWsl,
            'cartopy==0.25.0', 'pyshp==3.1.6', 'overturemaps==1.0.2', 'cdsapi==0.7.7'
        )

        Invoke-WslCommand @(
            '/bin/sh', '-lc',
            'printf ''[boot]\nsystemd=false\n\n[automount]\nenabled=true\noptions="metadata,umask=022,fmask=011"\n'' > /etc/wsl.conf'
        )
        Invoke-WslCommand @('/bin/sh', '-lc', 'printf ''AutoCF Windows Runtime 1.0\n'' > /etc/autocf-release')

        Write-Host 'Verifying Python, HydroMT, GDAL, and SFINCS linkage...'
        $pathValue = '/opt/autocf-gdal-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
        Invoke-WslCommand @(
            '/usr/bin/env',
            "PATH=$pathValue",
            'PYTHONNOUSERSITE=1',
            'PYTHONDONTWRITEBYTECODE=1',
            '/usr/local/bin/python', '-c',
            'import numpy, pandas, xarray, geopandas, rasterio, hydromt, hydromt_sfincs, shapely, cartopy, shapefile, pyarrow, overturemaps, requests, cdsapi'
        )
        Write-Host 'AutoCF Python runtime: PASS' -ForegroundColor Green
        Invoke-WslCommand @('/usr/bin/env', "PATH=$pathValue", '/opt/autocf-gdal-bin/gdalwarp', '--version')

        $cpuRoot = '/opt/sfincs-cpu/rootfs'
        $cpuLibraries = "$cpuRoot/usr/local/lib:$cpuRoot/usr/lib/x86_64-linux-gnu:$cpuRoot/lib/x86_64-linux-gnu"
        $linkage = & wsl.exe -d $DistroName -u root -- `
            "$cpuRoot/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" `
            --library-path $cpuLibraries `
            --list "$cpuRoot/usr/local/bin/sfincs"
        if ($LASTEXITCODE -ne 0 -or ($linkage -match 'not found')) {
            $linkage | Write-Host
            throw 'The bundled SFINCS CPU executable has unresolved runtime libraries.'
        }
        Write-Host 'SFINCS CPU runtime linkage: PASS' -ForegroundColor Green
    }
    catch {
        Write-Warning 'Installation did not validate; removing the incomplete AutoCF runtime.'
        & wsl.exe --unregister $DistroName *> $null
        throw
    }
    finally {
        & wsl.exe --terminate $DistroName 2>$null
    }

    Write-Host ''
    Write-Host 'AutoCF Windows installation: PASS' -ForegroundColor Green
    Write-Host 'Open AutoCF-Desktop.exe or run AutoCF.cmd to begin.'
}

function Show-AutoCFStatus {
    Write-Heading 'AutoCF Windows status'

    Write-Host ('Package          : ' + $PackageRoot)
    $payloadsReady = (Test-Path $RootfsArchive) -and `
        (Test-Path $SfincsCpuRootfs) -and `
        (Test-Path $SfincsCpuLauncher) -and `
        (Test-Path $PythonWheels)
    Write-Host ('Runtime payloads : ' + $(if ($payloadsReady) { 'PASS' } else { 'MISSING' }))
    Write-Host ('Datum helper     : ' + $(if (Test-Path -LiteralPath $TransformezHelper -PathType Leaf) {
        'PACKAGED (isolated from Python 3.11)'
    } else {
        'NOT PACKAGED - local alignment fallback remains available'
    }))

    if (-not (Test-DistroInstalled)) {
        Write-Host 'AutoCF runtime   : NOT INSTALLED' -ForegroundColor Yellow
        Write-Host 'Select Install / repair runtime in the AutoCF UI.'
        return
    }

    Write-Host 'AutoCF runtime   : INSTALLED' -ForegroundColor Green
    Write-Host ('Compute request  : ' + $ComputeBackend)
    & wsl.exe -d $DistroName -u root -- /usr/bin/test -x /usr/local/bin/sfincs-cpu
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'SFINCS backend   : CPU (OpenMP)' -ForegroundColor Green
        $cpuThreads = & wsl.exe -d $DistroName -u root -- /usr/bin/getconf _NPROCESSORS_ONLN 2>$null
        Write-Host ('CPU threads      : ' + ($cpuThreads -replace "`0", '').Trim())
        $gpuPayload = Test-GpuPayloadPackaged
        $gpuRuntime = Test-InstalledGpuRuntime
        $gpuHardware = Test-NvidiaGpuVisible
        Write-Host ('GPU payload      : ' + $(if ($gpuPayload) { 'PACKAGED' } else { 'NOT PACKAGED' }))
        Write-Host ('GPU hardware     : ' + $(if ($gpuHardware) { 'VISIBLE TO WSL2' } else { 'NOT DETECTED' }))
        Write-Host ('GPU ready        : ' + $(if ($gpuRuntime -and $gpuHardware) { 'YES' } else { 'NO - CPU fallback' }))

        & wsl.exe -d $DistroName -u root -- /usr/local/bin/python -c `
            'import shapely, cartopy, pyarrow, overturemaps' 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host 'Evaluation tools : INSTALLED' -ForegroundColor Green
            Write-Host 'Impact tools     : Shapely + Overture Maps + WorldPop API v2' -ForegroundColor Green
        }
        else {
            Write-Host 'Postprocessing   : NEEDS REPAIR' -ForegroundColor Yellow
            Write-Host 'Run Install / repair runtime to add evaluation and impact components.'
        }
    }
    else {
        Write-Host 'SFINCS backend   : CPU RUNTIME NOT INSTALLED' -ForegroundColor Yellow
        Write-Host 'Select Install / repair runtime in the AutoCF UI to add it.'
    }
}

function Start-AutoCFRun {
    if (-not (Test-DistroInstalled)) {
        throw 'The AutoCF runtime is not installed. Select Install / repair runtime in the AutoCF UI first.'
    }
    if (-not $Config) {
        throw 'Choose a YAML configuration file with -Config.'
    }

    $configPath = (Resolve-Path -LiteralPath $Config).Path
    $packageWsl = Convert-ToWslPath $PackageRoot
    $configWsl = Convert-ToWslPath $configPath
    $driverWsl = "$packageWsl/windows/autocf_runtime.py"
    $appWsl = "$packageWsl/app/sfincs_agent.py"
    $pathValue = '/opt/autocf-gdal-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    $runCpuThreads = if ($CpuThreads -gt 0) { $CpuThreads } else { [Environment]::ProcessorCount }
    $cdsApiRc = Join-Path $env:USERPROFILE '.cdsapirc'
    $cdsApiEnvironment = @()
    if (Test-Path -LiteralPath $cdsApiRc -PathType Leaf) {
        $cdsApiEnvironment = @("CDSAPI_RC=$(Convert-ToWslPath $cdsApiRc)")
    }
    $transformezEnvironment = @()
    if (Test-Path -LiteralPath $TransformezHelper -PathType Leaf) {
        $transformezEnvironment = @(
            "AUTOCF_TRANSFORMEZ_EXECUTABLE=$(Convert-ToWslPath $TransformezHelper)"
        )
    }
    $sfincsExecutable = Resolve-SfincsExecutable

    Write-Heading "AutoCF $Mode"
    Write-Host "Configuration: $configPath"

    & wsl.exe -d $DistroName -u root --cd $packageWsl -- `
        /usr/bin/env `
        "PATH=$pathValue" `
        "PYTHONPATH=$packageWsl/vendor" `
        "AUTOCF_PACKAGE_ROOT=$packageWsl" `
        "SFINCS_EXECUTABLE=$sfincsExecutable" `
        'PYTHONNOUSERSITE=1' `
        'PYTHONDONTWRITEBYTECODE=1' `
        'XDG_CACHE_HOME=/tmp/autocf-cache' `
        'MPLCONFIGDIR=/tmp/autocf-mpl' `
        'PROJ_NETWORK=OFF' `
        'HDF5_USE_FILE_LOCKING=FALSE' `
        'OMP_DYNAMIC=FALSE' `
        "OMP_NUM_THREADS=$runCpuThreads" `
        @cdsApiEnvironment `
        @transformezEnvironment `
        /usr/local/bin/python -u $driverWsl `
        --app $appWsl `
        --config $configWsl `
        --mode $Mode

    if ($LASTEXITCODE -ne 0) {
        throw "AutoCF failed with exit code $LASTEXITCODE."
    }
    Write-Host 'AutoCF operation: PASS' -ForegroundColor Green
}

function Test-AutoCFPostprocessInputs {
    if (-not (Test-DistroInstalled)) {
        throw 'The AutoCF runtime is not installed. Select Install / repair runtime in the AutoCF UI first.'
    }
    if (-not $Config) {
        throw 'Choose a YAML configuration file with -Config.'
    }
    return (Resolve-Path -LiteralPath $Config).Path
}

function Start-AutoCFEvaluation {
    $configPath = Test-AutoCFPostprocessInputs
    $packageWsl = Convert-ToWslPath $PackageRoot
    $configWsl = Convert-ToWslPath $configPath
    $evalWsl = "$packageWsl/app/sfincs_eval.py"
    $pathValue = '/opt/autocf-gdal-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    $evalArgs = @(
        '--config', $configWsl,
        '--tolerance-min', ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $EvaluationToleranceMinutes)),
        '--hmax-vmax', ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $EvaluationHmaxVmax))
    )
    if ($SkipEvaluationHmax) { $evalArgs += '--skip-hmax' }
    if ($SkipEvaluationObservations) { $evalArgs += '--skip-observations' }

    Write-Heading 'AutoCF evaluation'
    Write-Host "Configuration: $configPath"
    & wsl.exe -d $DistroName -u root --cd $packageWsl -- `
        /usr/bin/env `
        "PATH=$pathValue" `
        "PYTHONPATH=$packageWsl/vendor" `
        "AUTOCF_PACKAGE_ROOT=$packageWsl" `
        'PYTHONNOUSERSITE=1' `
        'PYTHONDONTWRITEBYTECODE=1' `
        'XDG_CACHE_HOME=/tmp/autocf-cache' `
        'MPLCONFIGDIR=/tmp/autocf-mpl' `
        'PROJ_NETWORK=OFF' `
        'HDF5_USE_FILE_LOCKING=FALSE' `
        /usr/local/bin/python -u $evalWsl @evalArgs
    if ($LASTEXITCODE -ne 0) {
        throw "AutoCF evaluation failed with exit code $LASTEXITCODE."
    }
    Write-Host 'AutoCF evaluation: PASS' -ForegroundColor Green
}

function Start-AutoCFImpact {
    $configPath = Test-AutoCFPostprocessInputs
    $packageWsl = Convert-ToWslPath $PackageRoot
    $configWsl = Convert-ToWslPath $configPath
    $impactWsl = "$packageWsl/app/sfincs_impact.py"
    $pathValue = '/opt/autocf-gdal-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    $impactArgs = @(
        '--config', $configWsl,
        '--thresholds-m', $ImpactThresholds,
        '--analysis-resolution-m', ([string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $ImpactAnalysisResolution))
    )
    if ($SkipImpactBuildings) { $impactArgs += '--skip-buildings' }
    if ($SkipImpactRoads) { $impactArgs += '--skip-roads' }
    if ($SkipImpactPopulation) { $impactArgs += '--skip-population' }
    if ($ForceImpactDownloads) { $impactArgs += '--force-downloads' }

    Write-Heading 'AutoCF impact attribution'
    Write-Host "Configuration: $configPath"
    & wsl.exe -d $DistroName -u root --cd $packageWsl -- `
        /usr/bin/env `
        "PATH=$pathValue" `
        "PYTHONPATH=$packageWsl/vendor" `
        'PYTHONNOUSERSITE=1' `
        'PYTHONDONTWRITEBYTECODE=1' `
        'XDG_CACHE_HOME=/tmp/autocf-cache' `
        'MPLCONFIGDIR=/tmp/autocf-mpl' `
        'PROJ_NETWORK=ON' `
        'HDF5_USE_FILE_LOCKING=FALSE' `
        /usr/local/bin/python -u $impactWsl @impactArgs
    if ($LASTEXITCODE -ne 0) {
        throw "AutoCF impact attribution failed with exit code $LASTEXITCODE."
    }
    Write-Host 'AutoCF impact attribution: PASS' -ForegroundColor Green
}

function Start-AutoCFForcingPreview {
    $configPath = Test-AutoCFPostprocessInputs
    $packageWsl = Convert-ToWslPath $PackageRoot
    $configWsl = Convert-ToWslPath $configPath
    $previewWsl = "$packageWsl/app/sfincs_forcing_preview.py"
    $pathValue = '/opt/autocf-gdal-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'

    Write-Heading 'AutoCF forcing map previews'
    Write-Host "Configuration: $configPath"
    & wsl.exe -d $DistroName -u root --cd $packageWsl -- `
        /usr/bin/env `
        "PATH=$pathValue" `
        "PYTHONPATH=$packageWsl/app`:$packageWsl/vendor" `
        'PYTHONNOUSERSITE=1' `
        'PYTHONDONTWRITEBYTECODE=1' `
        'MPLCONFIGDIR=/tmp/autocf-mpl' `
        'PROJ_NETWORK=OFF' `
        'HDF5_USE_FILE_LOCKING=FALSE' `
        /usr/local/bin/python -u $previewWsl --config $configWsl
    if ($LASTEXITCODE -ne 0) {
        throw "AutoCF forcing preview failed with exit code $LASTEXITCODE."
    }
    Write-Host 'AutoCF forcing previews: PASS' -ForegroundColor Green
}

function Start-AutoCFAnalysis([ValidateSet('prepare', 'prepare-exposure', 'run', 'report')][string]$Command) {
    $configPath = Test-AutoCFPostprocessInputs
    $packageWsl = Convert-ToWslPath $PackageRoot
    $configWsl = Convert-ToWslPath $configPath
    $analysisWsl = "$packageWsl/app/sfincs_attribution.py"
    $pathValue = '/opt/autocf-gdal-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    $analysisArgs = @('--config', $configWsl, $Command)
    $sfincsExecutable = if ($Command -eq 'run') {
        Resolve-SfincsExecutable
    }
    else {
        '/usr/local/bin/sfincs-cpu'
    }
    if ($ForceAnalysis -and $Command -in @('prepare', 'prepare-exposure', 'run')) { $analysisArgs += '--force' }
    if ($SkipAnalysisPopulation -and $Command -eq 'report') { $analysisArgs += '--skip-population' }

    Write-Heading "AutoCF attribution analysis: $Command"
    Write-Host "Configuration: $configPath"
    & wsl.exe -d $DistroName -u root --cd $packageWsl -- `
        /usr/bin/env `
        "PATH=$pathValue" `
        "PYTHONPATH=$packageWsl/vendor" `
        "AUTOCF_PACKAGE_ROOT=$packageWsl" `
        "SFINCS_EXECUTABLE=$sfincsExecutable" `
        'PYTHONNOUSERSITE=1' `
        'PYTHONDONTWRITEBYTECODE=1' `
        'XDG_CACHE_HOME=/tmp/autocf-cache' `
        'MPLCONFIGDIR=/tmp/autocf-mpl' `
        'PROJ_NETWORK=OFF' `
        'HDF5_USE_FILE_LOCKING=FALSE' `
        'OMP_DYNAMIC=FALSE' `
        /usr/local/bin/python -u $analysisWsl @analysisArgs
    if ($LASTEXITCODE -ne 0) {
        throw "AutoCF attribution analysis failed with exit code $LASTEXITCODE."
    }
    Write-Host "AutoCF attribution analysis ($Command): PASS" -ForegroundColor Green
}

switch ($Action) {
    'Install'  { Install-AutoCFRuntime }
    'Status'   { Show-AutoCFStatus }
    'Run'      { Start-AutoCFRun }
    'Evaluate' { Start-AutoCFEvaluation }
    'Impact'   { Start-AutoCFImpact }
    'ForcingPreview'         { Start-AutoCFForcingPreview }
    'AnalysisPrepare'         { Start-AutoCFAnalysis 'prepare' }
    'AnalysisPrepareExposure' { Start-AutoCFAnalysis 'prepare-exposure' }
    'AnalysisRun'             { Start-AutoCFAnalysis 'run' }
    'AnalysisReport'          { Start-AutoCFAnalysis 'report' }
}
