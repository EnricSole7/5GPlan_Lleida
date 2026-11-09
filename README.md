
# 5GPlan – Week 1 Milestone

## Scenario Definition and Synthetic RRH and Initial BBU Pool Assignment

This project implements the five main work packages of the first milestone.

### 1. Geographical scenario definition

- Load the study-area polygon from GeoJSON.
- Project coordinates to EPSG:25831 (ETRS89 / UTM zone 31N).
- Calculate the geographical area, bounding-box dimensions and centroid.

### 2. Real network-site data

- Import Infoantenas data from GeoJSON, JSON or CSV.
- Standardise column names.
- Filter records geographically to the study area.
- Optionally retain only records identified as 4G/5G, LTE or NR.

### 3. Real RRH data preparation

- Remove records with invalid coordinates.
- Remove duplicate source IDs.
- Remove geographically duplicated records using a configurable distance tolerance.
- Assign unique identifiers to the remaining real RRHs.

### 4. Synthetic RRH generation

- Generate a configurable number of synthetic RRHs.
- Use reproducible random placement.
- Generate locations uniformly within the study-area bounding box.
- Avoid placing synthetic RRHs too close to existing real RRHs.

### 5. Initial BBU pool selection and assignment

- Consider every RRH location as a potential BBU pool location.
- Select pool locations using farthest-first k-center initialisation.
- Assign each RRH to its nearest selected pool.
- Evaluate maximum, mean and median RRH-to-pool distances.
- Calculate the number of RRHs assigned to each pool.

## Modelling assumptions

1. Each cleaned Infoantenas physical-infrastructure record is treated as one RRH in this initial model. This is a simplifying assumption, not a claim that each physical site corresponds to exactly one sector or RRH.

2. Synthetic RRHs are placed uniformly in the bounding box by default. They can therefore fall outside the polygon. Set `synthetic_inside_polygon` to `true` in config.json to constrain them to the polygon.

3. Each selected BBU pool is located at an existing RRH coordinate.

4. The first BBU pool is the RRH closest to the scenario centroid. Subsequent pools are selected using the farthest-first algorithm.

5. This is a geographical baseline. Capacity, fibre topology, latency, equipment cost and the more advanced assignment strategies are not optimised at this stage.

## Installation

Python 3.10 or newer is recommended.

Create a virtual environment:

Windows:

    python -m venv .venv
    .venv\Scripts\activate

Linux/macOS:

    python -m venv .venv
    source .venv/bin/activate

Install dependencies:

    pip install -r requirements.txt

## Input data

Place your Infoantenas export in:

    data/input/

The default expected filename is:

    infoantenas.geojson

If your file has another name, update `input_file` in config.json.

Official portal:

https://geoportal.minetur.gob.es/VCTEL/vcne.do

The project brief does not specify a verified API endpoint or request schema, so the project does not assume one.

If no Infoantenas file is present, the pipeline runs with synthetic RRHs only. This is useful for testing, but it is not a replacement for the real-data stage.

## Execution

From the project root directory, execute:

    python main.py

## Generated outputs

### Scenario

- `results/scenario_summary.json`

Contains area, dimensions and centroid.

### RRH data

- `results/tables/rrhs_prepared.csv`
- `results/rrhs_prepared.geojson`

Contains the combined real and synthetic RRHs.

### BBU assignments

- `results/tables/assignments_K*.csv`
- `results/tables/bbu_pools_K*.csv`

Contains RRH assignments and selected BBU pool locations for each tested K.

### Comparison

- `results/tables/bbu_k_comparison.csv`

Contains the distance and load metrics for the different values of K.

- `results/figures/distance_vs_K.png`

Plots maximum and mean RRH-to-pool distance against the number of BBU pools.

### Maps

- `results/maps/assignments_K*.html`

Interactive maps showing:
- Blue: real RRHs.
- Orange: synthetic RRHs.
- Green: selected BBU pools.

Open the HTML files in a web browser.

## Configuration

The main parameters can be modified in config.json:

- `synthetic_rrh_count`: number of synthetic RRHs.
- `random_seed`: random generator seed.
- `minimum_synthetic_separation_m`: minimum separation between RRHs.
- `synthetic_inside_polygon`: constrain synthetic RRHs to the polygon.
- `pool_counts_to_test`: values of K to evaluate.
- `deduplication_tolerance_m`: spatial duplicate tolerance.
- `filter_to_4g_5g_when_known`: filter known technology records.
