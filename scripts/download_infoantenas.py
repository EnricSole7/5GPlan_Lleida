import argparse
import csv
import json
import math
import os
import time
import truststore
truststore.inject_into_ssl()
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET


BASE_URL = "https://geoportal.minetur.gob.es/VCTEL/infoantenasGeoJSON.do"
ZOOM = 7
TILE_WIDTH = 1.9830280064774 - 1.9068961857986
TILE_HEIGHT = 41.47127122711 - 41.414107988584
REQUEST_DELAY_SECONDS = 0.4
SITE_MAX_ATTEMPTS = 3
DETAIL_URL = "https://geoportal.minetur.gob.es/VCTEL/detalleEstacion.do"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

KML_FILE = PROJECT_ROOT / "data" / "input" / "working_area_lleida.kml"

RAW_GEOJSON_DIR = PROJECT_ROOT / "data" / "raw" / "geojson"
RAW_DETAILS_DIR = PROJECT_ROOT / "data" / "raw" / "station_details"
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"

for directory in [
    RAW_GEOJSON_DIR,
    RAW_DETAILS_DIR,
    OUTPUT_DIR
]:  directory.mkdir(parents=True, exist_ok=True)

OUTPUT_CSV = OUTPUT_DIR / "output_antenna_sites.csv"
CHECKPOINT_CSV = OUTPUT_DIR / "output_site_details.partial.csv"
TEMP_OUTPUT_CSV = OUTPUT_CSV.with_suffix(".tmp.csv")

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
DEFAULT_WORKERS = 2
OUTPUT_COLUMNS = [
    "Gis_ID",
    "Gis_Codigo",
    "direccion",
    "operador",
    "bandas_MHz",
    "numero_banda",
    "bandas",
    "tecnologia",
    "clasificacion_asumida",
    "estado",
    "error",
]
SUCCESS_STATUSES = {"OK", "SIN_DATOS"}
OPERATOR_ALIASES = {
    "vodafone españa, s.a.": "VODAFONE ESPAÑA, S.A.U.",
    "vodafone españa, s.a.u.": "VODAFONE ESPAÑA, S.A.U.",
}
BAND_FREQUENCY_RANGES = {
    "LTE": {
        "B1": ((1920, 1980), (2110, 2170)),
        "B3": ((1710, 1785), (1805, 1880)),
        "B4": ((1710, 1755), (2110, 2155)),
        "B7": ((2500, 2570), (2620, 2690)),
        "B8": ((880, 915), (925, 960)),
        "B20": ((791, 821), (832, 862)),
        "B28": ((703, 748), (758, 803)),
        "B38": ((2570, 2620),),
        "B40": ((2300, 2400),),
        "B41": ((2496, 2690),),
        "B42": ((3400, 3600),),
        "B43": ((3600, 3800),),
    },
    "NR": {
        "n1": ((1920, 1980), (2110, 2170)),
        "n3": ((1710, 1785), (1805, 1880)),
        "n7": ((2500, 2570), (2620, 2690)),
        "n8": ((880, 915), (925, 960)),
        "n20": ((791, 821), (832, 862)),
        "n28": ((703, 748), (758, 803)),
        "n38": ((2570, 2620),),
        "n77": ((3300, 4200),),
        "n78": ((3300, 3800),),
    },
}


def parse_coordinate_ring(text):
    points = []
    for coordinate in text.split():
        parts = coordinate.split(",")
        if len(parts) >= 2:
            points.append((float(parts[0]), float(parts[1])))
    if len(points) < 3:
        raise ValueError("El KML contiene un anillo con menos de tres coordenadas.")
    return points


def load_polygons(path):
    root = ET.parse(path).getroot()
    polygons = []
    for polygon in root.findall(".//{*}Polygon"):
        outer = polygon.find("./{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
        if outer is None or not outer.text:
            continue
        outer_ring = parse_coordinate_ring(outer.text)
        holes = []
        for inner in polygon.findall("./{*}innerBoundaryIs/{*}LinearRing/{*}coordinates"):
            if inner.text:
                holes.append(parse_coordinate_ring(inner.text))
        polygons.append((outer_ring, holes))
    if not polygons:
        raise ValueError(f"No se encontraron polígonos en {path}.")
    return polygons


def point_in_ring(point, ring):
    x, y = point
    inside = False
    for index, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(index + 1) % len(ring)]
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if (
            abs(cross) < 1e-10
            and min(x1, x2) - 1e-10 <= x <= max(x1, x2) + 1e-10
            and min(y1, y2) - 1e-10 <= y <= max(y1, y2) + 1e-10
        ):
            return True
        if (y1 > y) != (y2 > y):
            crossing_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing_x:
                inside = not inside
    return inside


def point_in_polygons(point, polygons):
    return any(
        point_in_ring(point, outer)
        and not any(point_in_ring(point, hole) for hole in holes)
        for outer, holes in polygons
    )


def make_tiles(polygons):
    all_points = [
        point
        for outer, holes in polygons
        for ring in [outer, *holes]
        for point in ring
    ]
    min_lon = min(point[0] for point in all_points)
    max_lon = max(point[0] for point in all_points)
    min_lat = min(point[1] for point in all_points)
    max_lat = max(point[1] for point in all_points)
    columns = max(1, math.ceil((max_lon - min_lon) / TILE_WIDTH))
    rows = max(1, math.ceil((max_lat - min_lat) / TILE_HEIGHT))
    tiles = []
    for row in range(rows):
        south = min_lat + (max_lat - min_lat) * row / rows
        north = min_lat + (max_lat - min_lat) * (row + 1) / rows
        for column in range(columns):
            west = min_lon + (max_lon - min_lon) * column / columns
            east = min_lon + (max_lon - min_lon) * (column + 1) / columns
            tiles.append((west, south, east, north))
    return tiles


def fetch_geojson(bbox):
    params = urllib.parse.urlencode({
        "idCapa": "null",
        "bbox": ",".join(format(value, ".12f").rstrip("0").rstrip(".") for value in bbox),
        "zoom": ZOOM,
    })
    request = urllib.request.Request(
        f"{BASE_URL}?{params}",
        headers={"User-Agent": "cellular-site-export/1.0", "Accept": "application/json"},
    )
    for attempt in range(SITE_MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                payload = response.read().decode(charset)
            data = json.loads(payload)
            if data == {}:
                return []
            if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                raise ValueError(
                    f"Respuesta no válida para bbox={bbox}: {payload[:200]!r}"
                )
            return data["features"]
        except (
            urllib.error.URLError,
            TimeoutError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            if attempt == SITE_MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Falló la consulta bbox={bbox}: {error}") from error
            time.sleep(2 ** attempt)


def feature_key(feature, properties):
    return (
        feature.get("id")
        or properties.get("Gis_ID")
        or properties.get("Gis_Codigo")
        or json.dumps(feature, sort_keys=True, ensure_ascii=False)
    )


def download_cell_sites():
    polygons = load_polygons(KML_FILE)
    tiles = make_tiles(polygons)
    unique_features = {}

    for index, bbox in enumerate(tiles, start=1):
        print(f"Consulta de estaciones {index}/{len(tiles)} (zoom {ZOOM})...")
        for feature in fetch_geojson(bbox):
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates")
            if (
                geometry.get("type") != "Point"
                or not isinstance(coordinates, list)
                or len(coordinates) < 2
            ):
                continue
            longitude, latitude = coordinates[:2]
            if not point_in_polygons((longitude, latitude), polygons):
                continue
            properties = feature.get("properties") or {}
            unique_features.setdefault(
                feature_key(feature, properties),
                (longitude, latitude, properties),
            )
        if index < len(tiles):
            time.sleep(REQUEST_DELAY_SECONDS)

    property_names = sorted({
        name
        for _, _, properties in unique_features.values()
        for name in properties
    })
    rows = []
    for identifier, (longitude, latitude, properties) in unique_features.items():
        row = {
            "feature_id": identifier,
            "latitude": latitude,
            "longitude": longitude,
        }
        row.update({
            name: value if isinstance(value, (str, int, float, bool)) or value is None
            else json.dumps(value, ensure_ascii=False)
            for name, value in properties.items()
        })
        for name in ("Gis_ID", "Gis_Codigo"):
            if row.get(name) is not None:
                row[name] = str(row[name])
        rows.append(row)

    return rows, property_names


def normalize_operator(operator):
    value = operator.strip()
    return OPERATOR_ALIASES.get(value.casefold(), value)


def band_metadata(bands):
    numbers = []
    technologies = set()
    band_numbers = set()
    for band in bands:
        matched_bands = {"frecuencia_MHz": band, "LTE": [], "NR": []}
        for segment in band.split(";"):
            lower, upper = (
                float(value.strip())
                for value in segment.split("-", maxsplit=1)
            )
            for technology, frequency_bands in BAND_FREQUENCY_RANGES.items():
                for number, ranges in frequency_bands.items():
                    if any(start <= lower and upper <= end for start, end in ranges):
                        if number not in matched_bands[technology]:
                            matched_bands[technology].append(number)
                        band_numbers.add(int(number.lstrip("Bn")))
                        technologies.add(technology)
        numbers.append(matched_bands)
    return (
        json.dumps(numbers, ensure_ascii=False),
        json.dumps(sorted(band_numbers), ensure_ascii=False),
        json.dumps(sorted(technologies), ensure_ascii=False),
    )


def classify_bands(band_numbers):
    if any({"n77", "n78"}.intersection(item["NR"]) for item in band_numbers):
        return "5G"
    lte_4g_bands = {"B4", "B7", "B20", "B28", "B38", "B40", "B41"}
    if any(lte_4g_bands.intersection(item["LTE"]) for item in band_numbers):
        return "4G"
    return "2G/3G"


class StationTablesParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._table_depth = 0
        self._current_table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            if self._current_table is None and attributes.get("class") == "tablaDetalleEstacionNivelesMedidos":
                self._current_table = []
                self._table_depth = 1
            elif self._current_table is not None:
                self._table_depth += 1
        elif self._current_table is not None and self._table_depth == 1:
            if tag == "tr":
                self._row = []
            elif tag in {"td", "th"} and self._row is not None:
                self._cell = []
            elif tag == "br" and self._cell is not None:
                self._cell.append(" ")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if self._current_table is None:
            return
        if tag in {"td", "th"} and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self._current_table.append(self._row)
            self._row = None
        elif tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self.tables.append(self._current_table)
                self._current_table = None


def identify_tables(html):
    parser = StationTablesParser()
    parser.feed(html)
    location = []
    technical = []

    for rows in parser.tables:
        if not rows:
            continue
        headings = [value.casefold() for value in rows[0]]
        if "operador" in headings and "referencia" in headings and any("banda asignada" in value for value in headings):
            technical = rows[1:]
        elif any("codigo" in value or "código" in value for value in headings) and any("direccion" in value or "dirección" in value for value in headings):
            location = rows[1:]

    address = location[0][1] if location and len(location[0]) > 1 else ""
    return address, technical


def make_records(station, address, technical):
    base = {
        "Gis_ID": station["Gis_ID"],
        "Gis_Codigo": station.get("Gis_Codigo", ""),
        "direccion": address,
        "operador": "",
        "bandas_MHz": "[]",
        "numero_banda": "[]",
        "bandas": "[]",
        "tecnologia": "[]",
        "clasificacion_asumida": "EXCLUIR",
        "estado": "SIN_DATOS",
        "error": "",
    }
    operators = []
    bands = []
    for row in technical:
        values = (row + [""] * 3)[:3]
        if any(values):
            operator = normalize_operator(values[0])
            if operator and operator not in operators:
                operators.append(operator)
            if values[2] and values[2] not in bands:
                bands.append(values[2])
    base["operador"] = "; ".join(operators)
    base["bandas_MHz"] = json.dumps(bands, ensure_ascii=False)
    (
        base["numero_banda"],
        base["bandas"],
        base["tecnologia"],
    ) = band_metadata(bands)
    base["clasificacion_asumida"] = classify_bands(
        json.loads(base["numero_banda"])
    )
    if bands:
        base["estado"] = "OK"
    return [base]


def fetch_station(station):
    identifier = station.get("Gis_ID", "").strip()
    code = station.get("Gis_Codigo", "").strip()
    if not identifier or not code:
        return [{
            "Gis_ID": identifier,
            "Gis_Codigo": code,
            "tipo_registro": "error",
            "estado": "ERROR",
            "error": "Falta Gis_ID o Gis_Codigo en el CSV de entrada.",
        }]

    query = urllib.parse.urlencode({
        "emplazamiento": identifier,
        "codEmplazamiento": code,
    })
    request = urllib.request.Request(
        f"{DETAIL_URL}?{query}",
        headers={"User-Agent": "cellular-site-details-export/1.0", "Accept": "text/html"},
    )
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset)
            address, technical = identify_tables(html)
            return make_records(station, address, technical)
        except (OSError, UnicodeError) as error:
            last_error = error
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS * (2 ** attempt))

    return [{
        "Gis_ID": identifier,
        "Gis_Codigo": code,
        "tipo_registro": "error",
        "estado": "ERROR",
        "error": str(last_error),
    }]


def read_successful_rows(path):
    if not path.exists():
        return [], set()
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    if "bandas_MHz" in fieldnames:
        successful = [row for row in rows if row.get("estado") in SUCCESS_STATUSES]
        for row in successful:
            row["operador"] = "; ".join(dict.fromkeys(
                normalize_operator(operator)
                for operator in row.get("operador", "").split(";")
                if operator.strip()
            ))
            bands = json.loads(row.get("bandas_MHz") or "[]")
            (
                row["numero_banda"],
                row["bandas"],
                row["tecnologia"],
            ) = band_metadata(bands)
            row["clasificacion_asumida"] = classify_bands(
                json.loads(row["numero_banda"])
            )
    else:
        stations = {}
        for row in rows:
            if row.get("estado") not in SUCCESS_STATUSES:
                continue
            code = row.get("Gis_Codigo", "")
            if not code:
                continue
            station = stations.setdefault(code, {
                "Gis_ID": row.get("Gis_ID", ""),
                "Gis_Codigo": code,
                "direccion": row.get("direccion", ""),
                "operador": "",
                "bandas_MHz": "[]",
                "numero_banda": "[]",
                "bandas": "[]",
                "tecnologia": "[]",
                "clasificacion_asumida": "2G/3G",
                "estado": "SIN_DATOS",
                "error": "",
            })
            operators = [value for value in station["operador"].split("; ") if value]
            operator = row.get("operador", "").strip()
            operator = normalize_operator(operator)
            if operator and operator not in operators:
                operators.append(operator)
                station["operador"] = "; ".join(operators)
            band = row.get("banda_asignada_MHz", "").strip()
            bands = json.loads(station["bandas_MHz"])
            if band and band not in bands:
                bands.append(band)
                station["bandas_MHz"] = json.dumps(bands, ensure_ascii=False)
                station["estado"] = "OK"
            (
                station["numero_banda"],
                station["bandas"],
                station["tecnologia"],
            ) = band_metadata(json.loads(station["bandas_MHz"]))
            station["clasificacion_asumida"] = classify_bands(
                json.loads(station["numero_banda"]),
            )
        successful = list(stations.values())
    completed_codes = {
        row["Gis_Codigo"] for row in successful if row.get("Gis_Codigo")
    }
    return successful, completed_codes


def write_combined_csv(site_rows, detail_rows):
    details_by_code = {
        row.get("Gis_Codigo", "").strip(): row
        for row in detail_rows
        if row.get("Gis_Codigo", "").strip()
    }
    site_columns = list(dict.fromkeys(
        name for row in site_rows for name in row
    ))
    columns = list(dict.fromkeys([
        *site_columns,
        *OUTPUT_COLUMNS,
    ]))
    if not site_rows:
        columns = ["feature_id", "latitude", "longitude", *OUTPUT_COLUMNS]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with TEMP_OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for site in site_rows:
            row = {name: "" for name in OUTPUT_COLUMNS}
            row["estado"] = "NO_CONSULTADO"
            row.update(site)
            code = str(site.get("Gis_Codigo") or "").strip()
            row.update(details_by_code.get(code, {}))
            writer.writerow(row)
    os.replace(TEMP_OUTPUT_CSV, OUTPUT_CSV)


def main():
    argument_parser = argparse.ArgumentParser(
        description="Descarga las estaciones móviles y sus detalles técnicos."
    )
    argument_parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Consultas simultáneas (por defecto: {DEFAULT_WORKERS}).",
    )
    argument_parser.add_argument(
        "--limit",
        type=int,
        help="Limita las consultas de detalles (la descarga de estaciones es completa).",
    )
    args = argument_parser.parse_args()
    if args.workers < 1:
        argument_parser.error("--workers debe ser al menos 1.")
    if args.limit is not None and args.limit < 1:
        argument_parser.error("--limit debe ser al menos 1.")

    site_rows, _ = download_cell_sites()
    if any(not str(station.get("Gis_Codigo") or "").strip() for station in site_rows):
        raise ValueError(f"Hay filas sin Gis_Codigo en los datos descargados.")
    stations_by_code = {
        str(station.get("Gis_Codigo") or "").strip(): station
        for station in site_rows
        if str(station.get("Gis_Codigo") or "").strip()
    }
    stations = list(stations_by_code.values())
    if args.limit is not None:
        stations = stations[:args.limit]

    prior_path = CHECKPOINT_CSV if CHECKPOINT_CSV.exists() else OUTPUT_CSV
    retained_rows, completed_codes = read_successful_rows(prior_path)
    active_codes = {station["Gis_Codigo"] for station in stations}
    retained_rows = [
        row for row in retained_rows
        if row.get("Gis_Codigo", "").strip() in active_codes
    ]
    completed_codes.intersection_update(active_codes)
    pending = [
        station
        for station in stations
        if station["Gis_Codigo"] not in completed_codes
    ]
    CHECKPOINT_CSV.parent.mkdir(parents=True, exist_ok=True)

    with CHECKPOINT_CSV.open("w", newline="", encoding="utf-8-sig") as checkpoint:
        writer = csv.DictWriter(checkpoint, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(retained_rows)
        checkpoint.flush()

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(fetch_station, station): station for station in pending}
            total = len(pending)
            failed = 0
            for completed, future in enumerate(as_completed(futures), start=1):
                records = future.result()
                if records and records[0].get("estado") == "ERROR":
                    failed += 1
                writer.writerows(records)
                checkpoint.flush()
                if completed % 25 == 0 or completed == total:
                    print(
                        f"Consultas completadas: {completed}/{total}; errores: {failed}",
                        flush=True,
                    )

    with CHECKPOINT_CSV.open("r", newline="", encoding="utf-8-sig") as checkpoint:
        detail_rows = list(csv.DictReader(checkpoint))
    write_combined_csv(site_rows, detail_rows)
    CHECKPOINT_CSV.unlink()
    successful_codes = {
        row.get("Gis_Codigo", "").strip()
        for row in detail_rows
        if row.get("estado") in SUCCESS_STATUSES and row.get("Gis_Codigo", "").strip()
    }
    print(
        f"CSV combinado creado: {OUTPUT_CSV} "
        f"({len(site_rows)} celdas; {len(successful_codes)} Gis_Codigo con detalles; "
        f"{failed} errores nuevos)."
    )


if __name__ == "__main__":
    main()
