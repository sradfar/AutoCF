# AutoCF Desktop 2.2

AutoCF Desktop uses one scientific workflow and selects a managed runtime for
the host operating system. Installation, status, restart handling, job output,
and command input are exposed in the UI; users do not need to enter setup
commands in a terminal.

| Host | Managed backend | Runtime installation |
| --- | --- | --- |
| Windows 10/11 | WSL2 or Docker Desktop | Selected, installed, and verified from AutoCF Settings |
| Linux | Docker or Podman | Local runtime image built by the UI |
| macOS (Intel or Apple Silicon) | Docker or Podman | Linux/AMD64 runtime image built by the UI |

The Windows, Linux, and macOS desktop executables must be built on their target
operating systems. They use the same Python UI source and YAML workflow.

Prepared distributions are kept separately as
`AutoCF_Windows_x64_Final`, `AutoCF_Docker_Runtime`,
`AutoCF_macOS_AppleSilicon`, and `AutoCF_macOS_Intel`. The macOS folders are
build-ready source distributions; `packaging/build_macos.sh` must run on the
matching Mac architecture to create and ad-hoc sign the `.app` and DMG.

## New and imported model workflows

- **Start new model** begins a guided setup: select the event first, save the
  automatically created draft YAML, then define the Study Area.
- **Import existing project** loads an existing YAML and reports only settings
  that are missing or incompatible; it does not force the guided new-model path.
- YAML can be saved while setup is incomplete. Final scientific validation is
  enforced before Build & Run, not while saving a draft.
- In Study Area, **Save study area & continue** writes the project GeoJSON and
  advances to Terrain.

## Model workspace organization

Selecting **Working directory for new models** now guarantees that each new
model is kept below that directory in one traceable event/project folder:

```text
<working directory>/
  Ida_2021/
    Ida_2021.yml
    Ida_2021_study_area.geojson
    runs/
    cache/
```

AutoCF does not create a second `_study_area` workspace. If `Ida_2021` already
exists, a new model uses `Ida_2021_02`, then `_03`, without overwriting the
existing project. Imported YAML files stay in their existing folder; relative
paths such as `./runs` and `./cache` are resolved relative to that YAML.

Each future run created by Desktop 2.2 is marked with
`project.layout_version: 2` and uses this structure:

```text
<run>/
  data/
    model/main/                 SFINCS engine workspace
    forcing/{netcdf,tables}/
    terrain/rasters/
    surface/{rasters,tables}/
    geospatial/vectors/
    validation/water_level/
    attribution/{scenarios,exposure}/
    previews/{terrain,surface,forcing}/
  results/
    simulation/{figures,tables,reports}/
    evaluation/{figures,tables,reports,rasters}/
    attribution/{figures,tables,reports,rasters}/
    exposure/{figures,tables,reports,vectors}/
  metadata/{run_state.json,artifact_catalog.json}
  logs/{build,simulation,analysis}/
  work/hydromt/
```

Format subfolders are created only when a producer needs them. The Results page
indexes only displayable PNG/JPG, CSV, and JSON products below `results`, and
combines files with the same product name into one row with Figure, Table, and
Overview tabs. NetCDF, TIFF, GeoParquet, caches, logs, and attribution scenario
workspaces stay available on disk but do not clutter the Results page.

Configurations without `project.layout_version: 2` retain the previous paths,
so existing runs can still be opened and resumed. AutoCF does not migrate or
rename an existing run automatically.

## Vertical datums

AutoCF records the native datum of every built-in terrain source. CUDEM and
USGS 3DEP are NAVD88; GEDTM30 and Copernicus GLO-30 are EGM2008; GEBCO is
treated as assumed MSL because its compiled inputs can vary locally. The model
target must match Source 1, so AutoCF never silently relabels the primary DEM.

When Source 2 uses a different datum, `topobathy.datum_engine: auto` first tries
the packaged Transformez grid helper on the managed Windows/WSL backend or in
the managed Docker image. The
helper is a separate Python 3.12+ executable; it does not install, remove, or
upgrade anything in AutoCF's validated Python 3.11 HydroMT/SFINCS runtime. It
also embeds NOAA HTDP 3.6.0 for the required horizontal reference-frame step.
The
first rigorous conversion downloads the needed transformation grids into the
shared project cache, and later runs reuse them. If the selected execution
backend cannot access the helper or Transformez cannot cover the conversion,
AutoCF retains the previous robust overlap-derived offset as an explicitly
approximate fallback. The clipped output is cached and
`vertical_datum_provenance.json` records the engine, fallback reason, offset,
sample count, and spread diagnostics. `datum_engine: transformez` makes the
rigorous engine mandatory; `local_alignment` explicitly selects the fallback.

The Terrain tab also accepts optional acquisition dates and warns when a source
postdates the modeled event. It does not treat a product edition year as an
acquisition date. Preparation writes `topobathy_source_provenance.json` and a
model-grid provenance GeoTIFF (0=no data, 1=Source 1, 2=Source 2), while the
technical GeoTIFF remains outside the curated Results list. NOAA Digital Coast
Data Access Viewer is linked as an optional source of newer U.S. coastal data.

## Install from the UI

1. Open the AutoCF desktop application for the current operating system.
2. Open **AutoCF Settings**. On Windows, select **Managed WSL2** or
   **Docker Desktop** under **Execution backend**.
3. Select **Install / repair runtime**.
4. Approve the operating system permission prompt when it appears.
5. If AutoCF displays **Restart to continue installation**, save other work and
   select that button. Reopen AutoCF after the restart and select **Install /
   repair runtime** again.
6. Select **Check system status**. Do not begin a model run until the status
   reports that the AutoCF runtime is installed.

Windows manages WSL2 automatically when that backend is selected. Windows,
Linux, and macOS can use an installed Docker or Podman engine; AutoCF builds its
runtime image from the bundled, validated payloads. Apple Silicon hosts run the
validated AMD64 scientific runtime using the container engine's architecture
emulation.

The **SFINCS compute** setting offers Automatic and CPU on every platform.
Windows also exposes NVIDIA GPU, but enables it only when compatible NVIDIA
hardware and a separately validated SFINCS GPU payload are both present.
Automatic selection safely falls back to the bundled CPU/OpenMP solver. macOS
always uses CPU because CUDA/NVIDIA execution is not available there.

## Windows package details

AutoCF for Windows packages the validated Linux AutoCF, HydroMT-SFINCS, GDAL,
Python, and CPU SFINCS runtimes. The Settings page can run that payload through
either a managed WSL2 distribution or Docker Desktop. Users do not install
Python, Conda, HydroMT, GDAL, or SFINCS separately. Docker Desktop must already
be installed and running when the Docker backend is selected.

## Requirements

- 64-bit Windows 10 21H2 or newer, or Windows 11
- Hardware virtualization enabled
- Administrator access for the first installation
- Internet access only when an AutoCF configuration downloads model inputs
- Docker Desktop or Podman when the Windows Docker backend is selected

The Windows/WSL kernel remains a host component. Everything else used by AutoCF
is included in the package.

## Command-line Windows installation

Double-click `Install-AutoCF.cmd`. If Windows enables WSL2, restart the
computer and run the installer once more. The installer imports the bundled
runtime under `%LOCALAPPDATA%\AutoCF\WSL` and verifies Python, HydroMT, GDAL,
Shapely, Cartopy, Overture Maps, and SFINCS linkage.

## Run

Use `AutoCF-Desktop.exe` when it is included in the package, or run:

```bat
AutoCF.cmd examples\sfincs_agent_subgrid_config.yml build
AutoCF.cmd examples\sfincs_agent_subgrid_config.yml run
AutoCF.cmd examples\sfincs_agent_subgrid_config.yml evaluate
AutoCF.cmd examples\sfincs_agent_subgrid_config.yml impact
```

The available operations are:

- `build`: prepare/download data and build the SFINCS project.
- `run`: run an already-built project with the bundled OpenMP CPU SFINCS.
- `full`: build and run in one operation.
- `evaluate`: create flood-depth maps and observation/gauge plots and metrics.
- `impact`: attribute flood depths to Overture Maps buildings and roads and to
  WorldPop Global2 v2 population.

Outputs are written into this AutoCF package's `runs` directory unless the YAML
configuration specifies another location.

## Desktop workspace

The desktop application is organized as a project workspace rather than one
long form. The left navigation opens these panels:

- **AutoCF Settings**: CPU thread count, folders, persistent Dark, Light, or
  System theme, runtime status, and the SFINCS software manager. Theme changes
  apply immediately.
- **Project**: project name, output/cache paths, overwrite policy, YAML
  validation, and comment-preserving save controls.
- **Study Area**: select a GeoJSON file, validate its bounds, and preview it on
  an interactive map.
- **Event & Time**: use calendar controls or load the live NOAA IBTrACS storm
  catalog, filter by basin/coastal region, year, and storm, then apply its track
  and recommended simulation dates.
- **Terrain**, **Roughness & Infiltration**, and **Forcing**: configure model
  inputs without editing YAML manually.
- **SFINCS Parameters**: edit common model controls and advanced exact
  `sfincs.inp` keywords such as `storevel`, `storevelmax`, `storefluxmax`,
  `storetzsmax`, `dtmaxout`, `huthresh`, and `advection`. A generated input
  preview and dependency validation are shown beside the editor.
- **Build & Run**: build or run with the bundled CPU/OpenMP backend; the CPU
  thread setting is passed to SFINCS.
- **Evaluation**, **Impact Attribution**, and **Results**: run post-processing,
  configure Overture/WorldPop attribution, and inspect curated maps, tables,
  and reports inside the app. Technical GIS/model artifacts remain available
  in the run folder without appearing as result rows.

The basemap and first IBTrACS catalog download require Internet access. The
catalog is cached under `cache` for subsequent sessions. The map and storm
selectors only update configuration values; they do not change the existing
AutoCF defaults unless the user explicitly applies and saves a selection.

The **Jobs and log** dock includes an input field below the read-only output.
When a running command requests a response, type it there and press Enter or
select **Send**. The input is enabled only while a job is running.

## SFINCS software manager

The Settings page asks whether SFINCS is already available. For an existing
installation or model, select its directory. AutoCF recognizes a SFINCS
executable, source tree, or a model folder containing `sfincs.inp`, detects a
version from release metadata and documentation without launching the selected
executable, and checks the official Deltares/SFINCS GitHub releases. If a newer
release exists, the user explicitly chooses whether to continue with the
current version or download the updated source.

When the answer is No, **Download latest source** installs the official GitHub
source archive under `Software/SFINCS/<release>` and writes release metadata in
that folder. Downloads run in the background, report progress, can be
cancelled, and reject archive paths that escape the destination.

GitHub distributes the GPL source, not the runnable Windows executable.
Accordingly, **Get runnable SFINCS** opens the official Deltares Windows
download page on Windows or the official Deltares Docker image on Linux and
macOS. Existing AutoCF model runs continue through the package's validated
managed runtime, so selecting or downloading optional SFINCS software does not
silently change scientific execution.

## Desktop evaluation and impact controls

`AutoCF-Desktop.exe` lists CPU explicitly for all SFINCS run operations. Its
Evaluation tab controls flood-map generation, gauge comparison, time matching,
and map depth range. **Attribution & Analysis** is divided into Status, Setup,
Simulations, Report, and Single-run tool tabs. Status identifies the active
YAML and parent run, counts prepared and completed scenarios, detects an
existing report, and recommends one next action. Setup controls Overture,
WorldPop, CDC SVI, duration, depth thresholds, and persistent-duration
thresholds. The independent one-model impact workflow is isolated under
Single-run tool instead of being mixed with factorial attribution.

Impact options are additive and do not change the existing model defaults. The
example YAML contains an `impact:` section with these keys:

```yaml
impact:
  enabled: false
  flood_depth_thresholds_m: [0.15, 0.3, 0.6, 1.0, 2.0]
  analysis_resolution_m: 30.0
  buildings: {enabled: true, source: overture_maps}
  roads: {enabled: true, source: overture_maps}
  population:
    enabled: true
    source: worldpop
    api_url: https://api.worldpop.org/v2
    year: simulation
    resolution: 100m
```

Population attribution uses WorldPop Global2 v2 for simulation years
2015-2030. AutoCF identifies the intersecting country or countries from the
model AOI using the bundled Natural Earth 1:50m Admin 0 boundary layer. If the
WorldPop v2 polygon API returns an invalid zero for an evidently populated AOI,
AutoCF validates and computes the report from the matching official Global2 v2
country raster instead. Downloaded rasters are cached for later reports; the
first fallback download can be large. Natural Earth data are public domain.

The impact action caches provider data in `data/exposure/source` and writes
its display products to `results/exposure`; technical vector outputs remain in
the corresponding format subfolder. Products include `impact_summary.csv`,
`impact_report.json`, an impact map, a summary plot, flood-footprint GeoParquet,
and impacted building/road GeoParquet files. A
WorldPop API key is optional; if provided, set it as `WORLDPOP_API_KEY` before
starting AutoCF.

## Factorial impact and equity reporting

The **Attribution & Analysis** report reuses the existing eight C/P/R factorial
scenario outputs. The additions below are post-processing only: they require
**zero additional SFINCS runs**.

- `absolute_impact_summary.csv` and `.png` report FULL absolute flooded area,
  buildings, population, the NONE baseline, and the FULL-minus-NONE increment.
  `scenario_hazard_exposure.csv` and `shapley_hazard_exposure.csv` use the same
  absolute-impact definition. Shapley values therefore sum to
  `f(FULL) - f(NONE)`.
- `population_affected_pct` uses WorldPop population within the fixed
  analysis-land mask as its denominator. The denominator is queried once and
  cached.
- `cdc_svi_burden_summary.csv`, `.json`, and `.png` overlay the FULL flood
  footprint with the 2022 CDC/ATSDR census-tract SVI. They report affected
  population by national SVI quartile, the percent of each quartile affected,
  and a burden concentration ratio. CDC SVI is marked not applicable rather
  than failing the core report for study areas outside the United States.
- `scenario_duration_metrics.csv`, `shapley_duration_metrics.csv`, and
  `flood_duration_summary.csv` use the saved hourly SFINCS depth fields.
  Threshold-crossing time is linearly interpolated between outputs. Reported
  metrics include flooded area-hours, building-hours, and areas/buildings that
  remain flooded for 6, 12, 24, and 48 hours. GeoTIFFs contain FULL duration
  and each driver's duration Shapley attribution.
- `driver_impact_shift.csv` and its plots report **Driver Impact Shift (DIS)**:

  `DIS(d,M) = 100 * [ShapleyShare(d,M) - ShapleyShare(d,FloodedArea)]`

  A positive DIS means a driver is consequence-heavy relative to its footprint;
  a negative value means it is footprint-heavy. DIS sums to zero percentage
  points across the three drivers whenever both FULL-minus-NONE denominators
  are nonzero. Negative Shapley values are retained rather than clipped.

For backward compatibility, `scenario_event_induced_excess_depth.csv` and the
`event_excess_depth_*.tif` files retain the former
`max(H_scenario - H_NONE, 0)` diagnostic. They are explicitly separated from
the absolute headline impacts.

Repeated use of **Prepare analysis workspace** is safe. A counterfactual folder
that already contains `sfincs_map.nc` is preserved without changing its inputs
or outputs. Selecting **Recreate existing prepared counterfactual folders** is
the explicit action that replaces those folders and their results.

## Riva research assistant

Riva appears as an animated pet in a fixed anchor dock at the lower-right of the
AutoCF window, directly beside **Jobs and log**. She stays in that anchor and
animates at a relaxed pace rather than sliding across the screen. The anchor
and chatbot use the same neutral panel colors as AutoCF and follow the selected
Dark, Light, or System theme.
Her 3D-rendered animation includes moving legs, arms and hands, body, head,
antenna, eyes, and facial expressions. Her actions follow her current task: she
rests and blinks while idle, waves when greeted, acts thoughtful while reading
evidence or installing local AI, and talks when presenting an answer. Click her
to open or close the docked research assistant.
If an AutoCF stage or setup data service fails, Riva says "I panicked" and uses
her hand-on-heart pose for eight seconds before returning to her normal state.
Riva searches the SFINCS manual shipped with AutoCF, the installed
HydroMT-SFINCS API documentation, and a curated AutoCF workflow and terminology
guide. Common questions such as how to start, what the current page does, or
what RMSE and DIS mean receive short built-in answers instead of raw API
docstrings. Technical HydroMT method questions still use the installed API
documentation, and relevant online documentation sources remain clickable.
Riva also loads `windows/riva_qa_database.json`, a portable deterministic
knowledge database that works without Qwen. Its first release contains 34
scientific and application rules, 23 traceable references, and 7 geographically
indexed SFINCS case studies. Scientific answers show citations in
`Author et al. YEAR (link)` style when applicable. Questions whose answer is
site dependent, including mesh size, topobathy buffer, Manning roughness, NBS
roughness, infiltration, and spin-up, return bounded decision guidance instead
of claiming one universal best value. When a study-area boundary is loaded,
Riva can add applicable nearby case studies while explicitly warning that a
published setting is not an automatic calibration value for the active model.


Questions about the active model are answered directly from saved JSON and CSV
products. Riva can compare evaluation gauges and high-water marks; explain
RMSE, MAE, bias, correlation, and peak errors; identify leading Shapley drivers
for hazard, buildings, population, and duration; interpret Driver Impact Shift,
CDC SVI burden, interactions, compound amplification, and single-run exposure.
These calculations are deterministic, so a language model is not responsible
for scientific values. Local result paths remain hidden in the conversation.

Riva works immediately without Qwen: all curated AutoCF answers and saved-result
interpretation remain available. The optional **Install local AI** button
downloads the Apache-2.0 Qwen3.5-0.8B Q4_0 model (563 MB) and the Windows CPU
llama.cpp engine into the current user's local AutoCF application-data folder.
Qwen is used only to synthesize selected SFINCS and HydroMT-SFINCS documentation
into natural wording. Inference remains on the computer and requires no API key.
The model is loaded on demand, unloaded when scientific work starts or AutoCF
closes, and is not used while an AutoCF job is running. Riva does not edit YAML
or launch simulations.

## Runtime design

The validated AutoCF environment is combined with the official Deltares SFINCS
CPU runtime. Windows users select either the managed WSL installation or the
same container route used on Linux and macOS:

- `autocf-wsl-rootfs.tar.gz`: exact AutoCF Python/HydroMT/GDAL environment.
- `sfincs-cpu-rootfs.tar.gz`: official Deltares SFINCS CPU runtime and libraries.

Both launchers run these Linux binaries on the computer's CPU by default. WSL2
requires no container engine; the Docker option requires Docker Desktop or
Podman. Neither backend requires an NVIDIA GPU. A GPU backend is selected only
when its separately distributed, provenance-recorded payload has passed the
same numerical regression checks as the bundled CPU solver.
