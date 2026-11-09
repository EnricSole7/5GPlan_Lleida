
import json
import math
import random
from pathlib import Path

import folium
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from pyproj import Transformer
from shapely.geometry import Point, shape, mapping
from shapely.ops import transform, unary_union
from shapely.prepared import prep


# ---------------------------------------------------------
# 1. INPUT DATA HANDLING
# ---------------------------------------------------------

ALIASES = {
    "site_id": ["site_id", "id", "codigo", "código",
                "identificador", "id_estacion", "id_estación"],
    "latitude": ["latitude", "lat", "latitud", "y"],
    "longitude": ["longitude", "lon", "lng", "longitud", "x"],
    "operator": ["operator", "operador", "titular",
                 "company", "empresa"],
    "technology": ["technology", "tecnologia", "tecnología",
                   "sistema", "standard"],
    "band": ["band", "banda", "frecuencia"],
    "site_type": ["site_type", "tipo", "tipo_estacion",
                  "tipo_estación"]
}


def load_study_area(path):
    """Load a GeoJSON study-area polygon."""
    data = json.loads(path.read_text(encoding="utf-8"))

    if data["type"] == "FeatureCollection":
        geometries = [
            shape(feature["geometry"])
            for feature in data["features"]
            if feature.get("geometry")
        ]
        if not geometries:
            raise ValueError("No geometries found in study-area file.")
        return unary_union(geometries)

    if data["type"] == "Feature":
        return shape(data["geometry"])

    return shape(data)


def project_geometry(geometry, transformer):
    """Transform geometry from WGS84 to the projected CRS."""
    return transform(transformer.transform, geometry)


def normalise_columns(df):
    """Map common source column names to standard names."""
    lookup = {
        str(column).strip().lower(): column
        for column in df.columns
    }

    rename = {}

    for target, candidates in ALIASES.items():
        for candidate in candidates:
            if candidate.lower() in lookup:
                rename[lookup[candidate.lower()]] = target
                break

    return df.rename(columns=rename)


def load_infoantenas(path):
    """Read Infoantenas data from GeoJSON, JSON or CSV."""

    if not path.exists():
        print(
            f"[INFO] No Infoantenas file found at {path}. "
            "Using synthetic RRHs only."
        )
        return pd.DataFrame()

    suffix = path.suffix.lower()

    if suffix in [".geojson", ".json"]:
        data = json.loads(path.read_text(encoding="utf-8"))

        if data.get("type") == "FeatureCollection":
            rows = []

            for feature in data.get("features", []):
                properties = dict(feature.get("properties") or {})
                geometry = feature.get("geometry") or {}

                if geometry.get("type") == "Point":
                    properties["longitude"] = geometry["coordinates"][0]
                    properties["latitude"] = geometry["coordinates"][1]

                rows.append(properties)

            df = pd.DataFrame(rows)

        elif isinstance(data, list):
            df = pd.DataFrame(data)

        elif isinstance(data, dict) and isinstance(data.get("records"), list):
            df = pd.DataFrame(data["records"])

        else:
            raise ValueError("Unsupported JSON format.")

    elif suffix == ".csv":
        df = pd.read_csv(path)

    else:
        raise ValueError(f"Unsupported file extension: {suffix}")

    df = normalise_columns(df)

    for column in [
        "site_id", "latitude", "longitude",
        "operator", "technology", "band", "site_type"
    ]:
        if column not in df.columns:
            df[column] = None

    df["latitude"] = pd.to_numeric(
        df["latitude"], errors="coerce"
    )
    df["longitude"] = pd.to_numeric(
        df["longitude"], errors="coerce"
    )

    df = df.dropna(subset=["latitude", "longitude"]).copy()
    df["site_id"] = df["site_id"].fillna("").astype(str)

    return df


# ---------------------------------------------------------
# 2. CLEANING AND PREPARING REAL RRHs
# ---------------------------------------------------------

def prepare_real_rrhs(
    df, study_area_projected, transformer, config
):
    """Filter, clean and standardise real network records."""

    columns = [
        "rrh_id", "latitude", "longitude", "x_m", "y_m",
        "operator", "technology", "band", "site_type", "is_real"
    ]

    if df.empty:
        return pd.DataFrame(columns=columns)

    out = df.copy()

    out["x_m"], out["y_m"] = transformer.transform(
        out["longitude"].to_numpy(),
        out["latitude"].to_numpy()
    )

    # Keep only records inside the study area.
    inside = [
        study_area_projected.covers(Point(x, y))
        for x, y in zip(out["x_m"], out["y_m"])
    ]

    out = out.loc[inside].copy()

    # If technology is known, optionally keep only 4G/5G records.
    if config.get("filter_to_4g_5g_when_known", True):
        known = (
            out["technology"].notna()
            & out["technology"].astype(str).str.strip().ne("")
        )

        technology = out["technology"].astype(str).str.lower()
        is_4g5g = technology.str.contains(
            r"4g|5g|lte|nr", regex=True
        )

        out = out.loc[~known | is_4g5g].copy()

    # Remove repeated source IDs.
    has_id = out["site_id"].str.strip().ne("")

    out = pd.concat([
        out.loc[~has_id],
        out.loc[has_id].drop_duplicates(
            subset=["site_id"], keep="first"
        )
    ], ignore_index=True)

    # Remove spatial duplicates within the configured tolerance.
    tolerance = float(
        config.get("deduplication_tolerance_m", 30)
    )

    kept = []

    for _, row in out.iterrows():
        point = Point(float(row["x_m"]), float(row["y_m"]))

        duplicate = any(
            point.distance(
                Point(float(item["x_m"]), float(item["y_m"]))
            ) < tolerance
            for item in kept
        )

        if not duplicate:
            kept.append(row.to_dict())

    out = pd.DataFrame(kept)

    out["rrh_id"] = [
        f"REAL_{i:05d}"
        for i in range(1, len(out) + 1)
    ]

    out["is_real"] = True

    return out[columns]


# ---------------------------------------------------------
# 3. GENERATING SYNTHETIC RRHs
# ---------------------------------------------------------

def generate_synthetic_rrhs(
    count, study_area, transformer, config, real_rrhs
):
    """Generate synthetic RRHs uniformly in the bounding box."""

    columns = [
        "rrh_id", "latitude", "longitude", "x_m", "y_m",
        "operator", "technology", "band", "site_type", "is_real"
    ]

    if count <= 0:
        return pd.DataFrame(columns=columns)

    rng = random.Random(int(config.get("random_seed", 42)))
    min_separation = float(
        config.get("minimum_synthetic_separation_m", 250)
    )

    inside_only = bool(
        config.get("synthetic_inside_polygon", False)
    )

    min_lon, min_lat, max_lon, max_lat = study_area.bounds
    prepared_area = prep(study_area)

    # Start with real-site coordinates to avoid placing synthetic
    # RRHs too close to existing real RRHs.
    occupied = []

    if not real_rrhs.empty:
        occupied = list(
            zip(real_rrhs["x_m"], real_rrhs["y_m"])
        )

    rows = []
    attempts = 0
    max_attempts = max(10000, count * 1000)

    while len(rows) < count and attempts < max_attempts:
        attempts += 1

        lon = rng.uniform(min_lon, max_lon)
        lat = rng.uniform(min_lat, max_lat)

        if inside_only and not prepared_area.covers(Point(lon, lat)):
            continue

        x, y = transformer.transform(lon, lat)

        too_close = any(
            math.hypot(x - px, y - py) < min_separation
            for px, py in occupied
        )

        if too_close:
            continue

        occupied.append((x, y))

        rows.append({
            "rrh_id": f"SYN_{len(rows) + 1:05d}",
            "latitude": lat,
            "longitude": lon,
            "x_m": x,
            "y_m": y,
            "operator": "SYNTHETIC",
            "technology": "5G",
            "band": None,
            "site_type": "synthetic",
            "is_real": False
        })

    if len(rows) < count:
        raise RuntimeError(
            f"Could place only {len(rows)} of {count} synthetic RRHs. "
            "Reduce the minimum separation or synthetic count."
        )

    return pd.DataFrame(rows, columns=columns)


# ---------------------------------------------------------
# 4. BBU POOL SELECTION: FARTHEST-FIRST K-CENTER
# ---------------------------------------------------------

def select_farthest_first(rrhs, k, centroid_xy):
    """
    Select BBU locations from existing RRH coordinates.

    First pool: RRH closest to scenario centroid.
    Further pools: RRH with largest distance to its nearest
    already selected pool.
    """

    coordinates = rrhs[["x_m", "y_m"]].to_numpy(dtype=float)

    if len(coordinates) == 0:
        raise ValueError("There are no RRHs available.")

    k = min(int(k), len(coordinates))

    first = int(np.argmin(
        np.linalg.norm(
            coordinates - np.asarray(centroid_xy),
            axis=1
        )
    ))

    selected = [first]

    minimum_distances = np.linalg.norm(
        coordinates - coordinates[first],
        axis=1
    )

    while len(selected) < k:
        next_index = int(np.argmax(minimum_distances))
        selected.append(next_index)

        new_distances = np.linalg.norm(
            coordinates - coordinates[next_index],
            axis=1
        )

        minimum_distances = np.minimum(
            minimum_distances, new_distances
        )

    return selected


def assign_to_pools(rrhs, selected_indices):
    """Assign each RRH to its nearest selected BBU pool."""

    coordinates = rrhs[["x_m", "y_m"]].to_numpy(dtype=float)
    pool_coordinates = coordinates[selected_indices]

    distances = np.linalg.norm(
        coordinates[:, None, :] - pool_coordinates[None, :, :],
        axis=2
    )

    assignments = np.argmin(distances, axis=1)

    assigned_distances = distances[
        np.arange(len(coordinates)), assignments
    ]

    result = rrhs.copy()

    result["pool_id"] = [
        f"BBU_{i + 1:02d}" for i in assignments
    ]

    result["distance_to_pool_m"] = assigned_distances

    return result, pool_coordinates


# ---------------------------------------------------------
# 5. MAP GENERATION
# ---------------------------------------------------------

def save_map(
    rrhs, pool_coordinates, study_area,
    transformer_to_wgs84, output_path
):
    """Create an interactive map of RRHs and BBU pools."""

    centre = study_area.centroid

    fmap = folium.Map(
        location=[centre.y, centre.x],
        zoom_start=10,
        control_scale=True
    )

    folium.GeoJson(
        mapping(study_area),
        name="Study area",
        style_function=lambda _: {
            "color": "#333333",
            "weight": 2,
            "fillOpacity": 0.05
        }
    ).add_to(fmap)

    for _, row in rrhs.iterrows():
        color = "blue" if bool(row["is_real"]) else "orange"

        folium.CircleMarker(
            [row["latitude"], row["longitude"]],
            radius=3,
            color=color,
            fill=True,
            fill_opacity=0.75,
            popup=(
                f'{row["rrh_id"]} | {row["pool_id"]} | '
                f'{row["distance_to_pool_m"]:.0f} m'
            )
        ).add_to(fmap)

    for i, (x, y) in enumerate(pool_coordinates, start=1):
        lon, lat = transformer_to_wgs84.transform(x, y)

        folium.Marker(
            [lat, lon],
            tooltip=f"BBU_{i:02d}",
            icon=folium.Icon(
                color="green",
                icon="server",
                prefix="fa"
            )
        ).add_to(fmap)

    folium.LayerControl().add_to(fmap)
    fmap.save(str(output_path))


# ---------------------------------------------------------
# 6. COMPLETE PIPELINE
# ---------------------------------------------------------

def run_pipeline(config_path="config.json"):

    root = Path(__file__).resolve().parents[1]

    config_path = Path(config_path)

    if not config_path.is_absolute():
        config_path = root / config_path

    config = json.loads(
        config_path.read_text(encoding="utf-8")
    )

    study_area_path = root / config["study_area_file"]
    input_path = root / config["input_file"]
    output_root = root / config.get("output_directory", "results")

    for folder in ["maps", "tables", "figures"]:
        (output_root / folder).mkdir(
            parents=True, exist_ok=True
        )

    # Load study area.
    study_wgs84 = load_study_area(study_area_path)

    # Use projected coordinates for all distance calculations.
    crs = config.get("projected_crs", "EPSG:25831")

    to_projected = Transformer.from_crs(
        "EPSG:4326", crs, always_xy=True
    )

    to_wgs84 = Transformer.from_crs(
        crs, "EPSG:4326", always_xy=True
    )

    study_projected = project_geometry(
        study_wgs84, to_projected
    )

    centroid = study_projected.centroid

    min_x, min_y, max_x, max_y = study_projected.bounds

    scenario_summary = {
        "scenario_name": config.get("scenario_name", "scenario"),
        "projected_crs": crs,
        "area_km2": study_projected.area / 1e6,
        "width_km": (max_x - min_x) / 1000,
        "height_km": (max_y - min_y) / 1000,
        "centroid_x_m": centroid.x,
        "centroid_y_m": centroid.y
    }

    (output_root / "scenario_summary.json").write_text(
        json.dumps(scenario_summary, indent=2),
        encoding="utf-8"
    )

    # Import and clean real sites.
    source = load_infoantenas(input_path)

    real_rrhs = prepare_real_rrhs(
        source, study_projected, to_projected, config
    )

    # Generate synthetic sites.
    synthetic_rrhs = generate_synthetic_rrhs(
        int(config.get("synthetic_rrh_count", 0)),
        study_wgs84,
        to_projected,
        config,
        real_rrhs
    )

    # Combine both datasets.
    rrhs = pd.concat(
        [real_rrhs, synthetic_rrhs],
        ignore_index=True
    )

    if rrhs.empty:
        raise RuntimeError(
            "No RRHs available. Add real data or increase "
            "synthetic_rrh_count in config.json."
        )

    rrhs.to_csv(
        output_root / "tables" / "rrhs_prepared.csv",
        index=False
    )

    # Export a proper GeoJSON FeatureCollection.
    features = []

    for _, row in rrhs.iterrows():
        properties = row.to_dict()
        properties["is_real"] = bool(properties["is_real"])

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [
                    float(row["longitude"]),
                    float(row["latitude"])
                ]
            },
            "properties": properties
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    (output_root / "rrhs_prepared.geojson").write_text(
        json.dumps(geojson, indent=2, default=str),
        encoding="utf-8"
    )

    # Test different numbers of BBU pools.
    summary_rows = []
    centroid_xy = (centroid.x, centroid.y)

    for requested_k in config.get(
        "pool_counts_to_test", [1, 2, 3, 4, 5]
    ):

        k = min(int(requested_k), len(rrhs))

        selected = select_farthest_first(
            rrhs, k, centroid_xy
        )

        assigned, pool_coordinates = assign_to_pools(
            rrhs, selected
        )

        pool_rows = []

        for i, (x, y) in enumerate(pool_coordinates, start=1):

            pool_id = f"BBU_{i:02d}"

            members = assigned[
                assigned["pool_id"] == pool_id
            ]

            pool_rows.append({
                "pool_id": pool_id,
                "x_m": x,
                "y_m": y,
                "latitude": float(members.iloc[0]["latitude"]),
                "longitude": float(members.iloc[0]["longitude"]),
                "assigned_rrh_count": int(len(members))
            })

        assigned.to_csv(
            output_root / "tables" / f"assignments_K{k}.csv",
            index=False
        )

        pd.DataFrame(pool_rows).to_csv(
            output_root / "tables" / f"bbu_pools_K{k}.csv",
            index=False
        )

        distances = assigned["distance_to_pool_m"].to_numpy()

        summary_rows.append({
            "K": k,
            "number_of_rrhs": len(rrhs),
            "max_distance_km": float(np.max(distances) / 1000),
            "mean_distance_km": float(np.mean(distances) / 1000),
            "median_distance_km": float(np.median(distances) / 1000),
            "max_pool_load_rrhs": int(
                max(p["assigned_rrh_count"] for p in pool_rows)
            )
        })

        save_map(
            assigned,
            pool_coordinates,
            study_wgs84,
            to_wgs84,
            output_root / "maps" / f"assignments_K{k}.html"
        )

    # Save comparison table.
    summary = pd.DataFrame(summary_rows)

    summary = (
        summary.drop_duplicates(subset=["K"])
        .sort_values("K")
    )

    summary.to_csv(
        output_root / "tables" / "bbu_k_comparison.csv",
        index=False
    )

    # Plot distance versus number of pools.
    fig, ax = plt.subplots(figsize=(8, 5))

    ax.plot(
        summary["K"],
        summary["max_distance_km"],
        marker="o",
        label="Maximum distance"
    )

    ax.plot(
        summary["K"],
        summary["mean_distance_km"],
        marker="s",
        label="Mean distance"
    )

    ax.set_xlabel("Number of BBU pools (K)")
    ax.set_ylabel("RRH-to-pool distance (km)")
    ax.set_title("Fronthaul distance versus number of BBU pools")
    ax.grid(True, alpha=0.3)
    ax.legend()

    fig.tight_layout()

    fig.savefig(
        output_root / "figures" / "distance_vs_K.png",
        dpi=160
    )

    plt.close(fig)

    print("\nPipeline completed successfully.")
    print("\nScenario summary:")
    print(json.dumps(scenario_summary, indent=2))
    print("\nBBU comparison:")
    print(summary.to_string(index=False))

    return summary


if __name__ == "__main__":
    run_pipeline()
