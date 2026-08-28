# AutoCF / SFINCS Agent V2

This package extends the working SFINCS hydraulic workflow without rewriting the hydraulic implementation. The established builder/runner is retained as `sfincs_hydraulic_core.py`; the new user-facing entry point is `autocf.py`.

## What changed

### 1. One canonical configuration

Normal users edit only `sfincs_agent_config.yml` or a small `extends:` overlay such as `sfincs_agent_subgrid_config.yml`.

`sfincs_config.py` owns the backend defaults and derives the internal HydroMT/SFINCS configuration. Settings such as the model vertical datum are defined once. The same `model.vertical_datum` is inherited by topobathy, NOAA boundary forcing, NOAA validation, and HWM datum checks unless an advanced override is explicitly supplied.

The generated internal configuration is written to:

```text
<run>/agent_work/_autocf_resolved_legacy.yml
<run>/agent_work/_autocf_resolved_canonical.yml
```

### 2. Production validation has no SAR/flood-extent branch

`sfincs_eval.py` contains only:

- maximum-depth mapping
- water-level gauge validation
- HWM validation

It does **not** calculate OPERA/Sentinel-1, Copernicus GFM, Google Flood Hub, or other remote-sensing flood-extent CSI/POD/FAR/F1 metrics.

Automatic water-level provider order is:

```text
NOAA CO-OPS -> GESLA-4.1 -> UHSLC/JASL Research Quality -> UHSLC Fast Delivery
```

For a gauge whose vertical datum matches `model.vertical_datum`, AutoCF performs absolute validation. Otherwise it performs datum-neutral event validation:

1. estimate one constant offset from `datum_alignment_window_hours`;
2. freeze the offset;
3. exclude the alignment window from event scores by default;
4. report event RMSE/MAE/bias/correlation, anomaly metrics, peak magnitude error, peak timing error, and amplitude error.

No event-time offset fitting is performed.

A user CSV + station GeoJSON remains supported with `validation.water_level.provider: file`.

### 3. Exact compound-flood factorial analysis

Drivers are:

```text
C = coastal forcing = ocean-boundary variability + wind + pressure
P = precipitation
R = river discharge
```

AutoCF constructs the exact 2^3 factorial:

```text
NONE, C, P, R, CP, CR, PR, CPR
```

The completed parent run is reused as `CPR`, so only seven additional SFINCS simulations are required.

Excluded drivers are placed at a pre-event neutral background, not calibrated to event observations:

- ocean boundary -> quiet-window median constant
- wind -> zero
- pressure -> quiet-window median spatial field
- precipitation -> zero
- river -> quiet-window median

Static SFINCS inputs are hard-linked where possible. A forcing file is detached before a scenario-specific edit, so the parent run is never modified.

### 4. Scientific analysis products

The analysis engine generates:

- exact Shapley maximum-depth attribution for C/P/R
- Shapley fraction maps and a dominant-driver map
- exact pairwise and three-way factorial interaction maps
- Shapley and factorial closure audits
- compound amplification
  `H_CPR - max(H_C, H_P, H_R)`
- relative compound amplification
- flooded-area depth-threshold curves
- Overture building exposure-depth curves
- WorldPop population exposure-depth curves
- Shapley attribution of flooded area, exposed buildings, and exposed population
- explicit pairwise/three-way interaction terms for those scalar impact metrics
- compound amplification of hazard/exposure metrics

The interaction decomposition and Shapley decomposition answer different questions. Factorial interaction terms retain synergy/antagonism explicitly; Shapley values allocate the total change, including interactions, back to the three drivers.

Population exposure uses WorldPop API v2. Building exposure uses Overture Maps footprints. The default building observation operator samples maximum depth at a representative point guaranteed to lie inside each footprint. No damage curve is imposed.

Attribution currently operates on the SFINCS computational grid. The regular evaluation product still uses the established subgrid downscaling for the publication-quality maximum-depth map.

## Files

```text
autocf.py                         user-facing orchestrator
sfincs_hydraulic_core.py          retained working hydraulic builder/runner
sfincs_config.py                  canonical config + legacy adapter
sfincs_global_validation.py       NOAA/GESLA/UHSLC/file water-level validation
sfincs_eval.py                    production evaluator, no flood-extent RS
sfincs_attribution.py             factorial/Shapley/exposure engine
sfincs_agent_config.yml           streamlined base case
sfincs_agent_subgrid_config.yml   small subgrid+GCN250 overlay
```

## Della workflow

Always activate the established environment first:

```bash
module purge
module load anaconda3/2024.10
conda activate /scratch/gpfs/NLIN/sradfar/conda_envs/sfincs_agent
cd /path/to/autocf_v2
```

### Existing completed case: prepare global validation and evaluate

For the supplied subgrid overlay, the resolved run directory is the existing Tampa/Helene GCN250 subgrid run. No SFINCS rerun is required for evaluation.

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml prepare
python -u autocf.py --config sfincs_agent_subgrid_config.yml evaluate
```

If that completed model already has observation points, `prepare` preserves their exact `point_zs` ordering rather than silently discovering a different station order.

### New case: build on login node

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml build
```

### Run on interactive GPU node

No internet discovery occurs in this command.

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml run
```

### Evaluate on login node

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml evaluate
```

Useful evaluator options can be passed after `evaluate`, for example:

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml evaluate -- --skip-hwm
```

## Factorial attribution + exposure

Run after the full parent SFINCS event is complete.

On the login node, install Overture once if it is not already available:

```bash
conda install -c conda-forge overturemaps
```

Then prepare the seven counterfactual scenarios and cache building footprints:

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml analysis-prepare
```

On the interactive GPU node:

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml analysis-run
```

Back on the login node, compute attribution and exposure:

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml analysis-report
```

For a quick report without WorldPop API calls:

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml analysis-report --skip-population
```

## Main analysis outputs

```text
<run>/agent_analysis/results/
    shapley_max_depth_coastal.tif
    shapley_max_depth_precipitation.tif
    shapley_max_depth_river.tif
    shapley_fraction_max_depth_*.tif
    dominant_shapley_driver.tif
    dominant_shapley_fraction.tif
    factorial_interaction_*_max_depth.tif
    compound_amplification_max_depth.tif
    compound_amplification_relative.tif
    scenario_hazard_exposure.csv
    shapley_hazard_exposure.csv
    factorial_interactions_hazard_exposure.csv
    compound_amplification_hazard_exposure.csv
    exposure_depth_curve_*.png
    attribution_summary.json
```

The dominant-driver raster uses: 1 = coastal, 2 = precipitation, 3 = river, 0 = undefined.

## Internal tests included in development

The package was syntax-compiled and tested with:

- exact synthetic Shapley closure
- exact factorial main/pairwise/three-way interaction recovery
- synthetic counterfactual forcing neutralization
- synthetic GESLA provider selection/coverage
- datum-neutral frozen-offset validation
- user-file absolute-datum validation
- legacy-config audit confirming the production external-validation block contains HWM only

These tests do not replace event-specific hydraulic validation of new study regions.


## V2.1 attribution/exposure corrections

`analysis-report` now evaluates hazard and exposure on **normally dry land**. ESA
WorldCover permanent-water cells are excluded first. At each depth threshold, a
cell/building is then eligible only when the `000_none` background scenario is
below that same threshold. This makes the NONE exposure baseline zero by
construction and prevents open water or pre-existing background inundation from
being counted as event exposure.

The 0.05-m threshold remains in CSV outputs as a sensitivity test, while default
figures use 0.10-2.0 m. Compound amplification is now summarized at absolute
thresholds of 0.01/0.05/0.10/0.25 m and relative thresholds of 5/10/25%, with a
minimum 0.10-m full-event depth for relative metrics. The raw `A>0` area is kept
only as a diagnostic. Building sampling is vectorized and WorldPop polygon jobs
use bounded concurrency with the existing cache, substantially reducing report
runtime.


## V2.2 fixed normally-dry exposure universe

V2.2 corrects the event-induced exposure operator used in V2.1.

V2.1 changed the eligible spatial mask with every reporting depth threshold,
which could produce mathematically invalid exposure-depth curves that increased
at deeper thresholds. V2.2 defines the exposure universe once:

`fixed eligible = static land AND H_NONE < normally_dry_max_depth_m`

and reuses that identical mask for every depth threshold, every factorial
scenario, flooded area, Overture buildings, and WorldPop population.

Canonical configuration:

```yaml
analysis:
  attribution:
    depth_thresholds_m: [0.05, 0.10, 0.25, 0.50, 1.00, 2.00]
    primary_min_depth_m: 0.10

  land_mask:
    enabled: true
    worldcover_water_classes: [80]
    min_land_fraction: 0.50
    event_induced_only: true
    normally_dry_max_depth_m: 0.05
```

`primary_depth_thresholds_m` and `sensitivity_depth_thresholds_m` are now
derived from `depth_thresholds_m` and `primary_min_depth_m`; they no longer need
to be repeated in the YAML.

The report stage now performs an automatic monotonicity quality-control test.
Flooded area and building exposure must be non-increasing with increasing depth
threshold. A violation raises an error rather than silently creating misleading
figures. Population monotonicity is also recorded in `attribution_summary.json`,
with a small tolerance for the external WorldPop polygon-statistics service.

No SFINCS reruns are required when upgrading from V2.1 to V2.2. Re-run only:

```bash
python -u autocf.py --config sfincs_agent_subgrid_config.yml analysis-report
```


## V2.3 event-induced excess-depth exposure

V2.3 replaces the V2.2 `H_NONE < 0.05 m` land-eligibility gate. In the Tampa
case that gate eliminated every static-land cell because the NONE run contained
at least 0.05 m maximum depth on all analyzed land cells.

The default exposure depth is now:

`delta_h_S = max(H_S - H_NONE, 0)`

on one static ESA-WorldCover land universe. `000_none` is therefore zero by
definition, but no land cells are discarded simply because the NONE simulation
contains persistent or numerical background depth.

Thresholded flooded area is also land-fraction weighted at the SFINCS
computational-cell scale:

`A(d) = sum(cell_area * land_fraction * I[delta_h >= d])`

instead of counting every mixed land/water computational cell as a full land
cell.

Canonical configuration:

```yaml
analysis:
  attribution:
    depth_thresholds_m: [0.05, 0.10, 0.25, 0.50, 1.00, 2.00]
    primary_min_depth_m: 0.10

  land_mask:
    enabled: true
    worldcover_water_classes: [80]
    min_land_fraction: 0.50

  exposure:
    enabled: true
    depth_definition: excess_over_none
```

The report writes `event_depth_<scenario>.tif` for all eight factorial
scenarios, records NONE-on-land depth quantiles in `attribution_summary.json`,
and stops instead of producing figures if the full event has zero positive
event-induced depth on land.

No SFINCS reruns are required.
