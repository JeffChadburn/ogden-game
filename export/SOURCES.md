# Data sources

All data used to build this environment is open or public-domain. **Nothing was taken from Google Maps, Google Earth, Street View, or any other proprietary imagery, tiles, or 3D models.** No satellite photographs were used as textures. Building facades, roofs, and ground materials are procedural (generated in `tools/generate_textures.py` and in-engine shaders). No business logos or copyrighted architectural ornament were copied.

## 1. OpenStreetMap (ODbL 1.0)

- **License:** Open Database License (ODbL) 1.0. © OpenStreetMap contributors.
- **Attribution (required):** `© OpenStreetMap contributors, ODbL`
- **Share-alike:** The derived OSM database (clipped extracts, GeoJSON layers, and any other derived databases) remains under ODbL. Produced Works (the 3D meshes, screenshots, and the game scene) must include the attribution above. See [ODbL Produced Work guidance](https://wiki.osmfoundation.org/wiki/Licence/Licence_and_Legal_FAQ/ODbL_Guideline_for_Using_OpenStreetMap_Data).
- **Product / method:** Geofabrik Utah extract, then clipped to the project area with `osmium extract` (complete-ways) and converted with GDAL’s OSM driver (`ogr2ogr`) using `tools/osmconf.ini`.
- **Download URL:** https://download.geofabrik.de/north-america/us/utah-latest.osm.pbf
- **Downloaded at (UTC):** 2026-11-11T20:36:15Z
- **Clip bbox (W,S,E,N):** -112.010, 41.188, -111.850, 41.252
- **Clip finished (UTC):** 2026-11-11T20:36:44Z
- **Raw files:** `data/raw/osm/utah-latest.osm.pbf` (statewide, not shipped in the web build), `data/raw/osm/ogden.osm.pbf`, `data/raw/osm/ogden.osm` (OSM XML), per-layer GeoJSON under `data/raw/osm/`.
- **Overpass note:** A single Overpass query (`data/raw/osm/overpass_query.overpassql`) was attempted first against `https://overpass-api.de/api/interpreter` (SSL unexpected EOF) and `https://overpass.kumi.systems/api/interpreter` (no response). The pipeline fell back to the Geofabrik extract, which is an allowed ODbL source in the project brief.

## 2. USGS 3D Elevation Program (3DEP) — public domain

- **License:** Public domain (USGS). Credit requested: `Elevation: USGS 3D Elevation Program`
- **Product:** 3DEP **1-meter** DEM GeoTIFF tiles from The National Map (not 1/3 arc-second; 1 m lidar coverage exists for the entire project box).
- **Discovery API:** `https://tnmaccess.nationalmap.gov/api/v1/products` with `datasets=Digital Elevation Model (DEM) 1 meter`, `bbox=-112.000,41.205,-111.900,41.245`, `prodFormats=GeoTIFF`. Query cached in `data/raw/meta/`.
- **Tiles used (windowed read via GDAL `/vsicurl/`, mosaicked and cropped to the terrain bbox, reprojected to EPSG:32612):**
  1. **USGS one meter x41y457 UT Northern QL1 2018** — west / valley floor. Publication date 2020-03-30. URL: https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/UT_Northern_QL1_2018/TIFF/USGS_one_meter_x41y457_UT_Northern_QL1_2018.tif
  2. **USGS 1 Meter 12 x42y457 UT_StatewideNCentral_2020_A20** — east / foothills and Mount Ogden massif (preferred in the overlap). Publication date 2021-11-21. URL: https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/UT_StatewideNCentral_2020_A20/TIFF/USGS_1M_12_x42y457_UT_StatewideNCentral_2020_A20.tif
- **Warp finished (UTC):** 2026-11-11T20:32:39Z (see `data/raw/dem/dem_fetch.json`)
- **Output:** `data/raw/dem/ogden_1m_utm12n.tif` — 12,470 × 5,862 pixels at 1 m, CRS EPSG:32612 (WGS 84 / UTM zone 12N).
- **Fallback not used:** 1/3 arc-second NED and SRTM/Copernicus were not needed; 1 m coverage is complete for the terrain box.

## 3. Materials

- All ground and facade textures are **procedural** (code-generated, tileable). No CC0 or third-party texture packs were downloaded. See `tools/generate_textures.py`.

## Bounding boxes (WGS84)

- **User / feature box (unchanged):** S 41.205, N 41.245, W -112.000, E -111.900
- **Terrain-only margin:** S 41.193, N 41.247, W -112.003, E -111.855 — extends east and slightly south so Mount Ogden (41.1999 N, 111.8822 W) and the Wasatch ridgeline remain visible from street level.
