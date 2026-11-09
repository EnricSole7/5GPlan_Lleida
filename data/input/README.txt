
Place your Infoantenas export in this folder.

The pipeline accepts:
- GeoJSON FeatureCollection with Point geometries
- JSON list of records
- CSV with latitude and longitude columns

Recommended fields:
- site_id
- latitude
- longitude
- operator
- technology
- band
- site_type

Default expected filename:
infoantenas.geojson

If your file has another name, update "input_file" in config.json.

Official Infoantenas portal:
https://geoportal.minetur.gob.es/VCTEL/vcne.do

The project brief does not specify a verified API endpoint or
request schema, so the pipeline does not invent one.
