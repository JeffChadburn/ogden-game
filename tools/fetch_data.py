#!/usr/bin/env python3
"""Fetch and cache OpenStreetMap + USGS 3DEP elevation for the Ogden prototype.

Legal sources only:
  - OpenStreetMap via Overpass API (ODbL)
  - USGS 3DEP 1-meter DEM tiles via The National Map / public S3 (public domain)

Rerun is safe: existing outputs are reused unless --force is set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from osgeo import gdal

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OSM_DIR = RAW / "osm"
DEM_DIR = RAW / "dem"
META_DIR = RAW / "meta"

# User-specified game/feature bbox (WGS84). Do not expand for OSM features.
USER_BBOX = {
    "south": 41.205,
    "west": -112.000,
    "north": 41.245,
    "east": -111.900,
    "note": "User-specified box: 17th–28th St corridor, Wall Ave/UP tracks to foothills.",
}

# Terrain-only margin. Mount Ogden summit sits just SE of the user box
# (~41.196 N, 111.88 W). Extending south and east keeps the Wasatch
# silhouette complete from street level, without adding OSM features there.
TERRAIN_BBOX = {
    "south": 41.193,
    "west": -112.003,
    "north": 41.247,
    "east": -111.855,
    "note": "Terrain-only margin east/south to include Mount Ogden ridge and a thin pad on other sides.",
}

# Best-available 1 m tiles that together cover TERRAIN_BBOX.
# West: UT_Northern_QL1_2018 (full coverage). East overlap: prefer the
# newer UT_StatewideNCentral_2020 tile (listed last so gdalwarp stacks it on top).
DEM_TILES = [
    {
        "title": "USGS one meter x41y457 UT Northern QL1 2018",
        "url": "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/UT_Northern_QL1_2018/TIFF/USGS_one_meter_x41y457_UT_Northern_QL1_2018.tif",
        "publicationDate": "2020-03-30",
        "product": "USGS 3DEP 1-meter DEM",
        "project": "UT_Northern_QL1_2018",
        "sizeInBytes": 342517350,
        "role": "west half, including downtown / valley floor",
    },
    {
        "title": "USGS 1 Meter 12 x42y457 UT_StatewideNCentral_2020_A20",
        "url": "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/UT_StatewideNCentral_2020_A20/TIFF/USGS_1M_12_x42y457_UT_StatewideNCentral_2020_A20.tif",
        "publicationDate": "2021-11-21",
        "product": "USGS 3DEP 1-meter DEM",
        "project": "UT_StatewideNCentral_2020_A20",
        "sizeInBytes": 234333877,
        "role": "east half, foothills and Mount Ogden massif (preferred in overlap)",
    },
]

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

USER_AGENT = "ogden-game-prototype/0.1 (open-data 3D environment; https://github.com/JeffChadburn/ogden-game)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def overpass_query(s, w, n, e, s2, w2, n2, e2) -> str:
    # One polite Overpass query: feature layers in the user box, plus peaks
    # and named streets in a slightly larger box so we can verify extents.
    return f"""
[out:json][timeout:240];
(
  way["building"]({s},{w},{n},{e});
  relation["building"]({s},{w},{n},{e});
  way["highway"]({s},{w},{n},{e});
  relation["highway"]({s},{w},{n},{e});
  way["railway"]({s},{w},{n},{e});
  relation["railway"]({s},{w},{n},{e});
  way["landuse"]({s},{w},{n},{e});
  relation["landuse"]({s},{w},{n},{e});
  way["natural"]({s},{w},{n},{e});
  relation["natural"]({s},{w},{n},{e});
  way["waterway"]({s},{w},{n},{e});
  relation["waterway"]({s},{w},{n},{e});
  way["water"]({s},{w},{n},{e});
  relation["water"]({s},{w},{n},{e});
  way["leisure"]({s},{w},{n},{e});
  relation["leisure"]({s},{w},{n},{e});
  node["natural"="tree"]({s},{w},{n},{e});
  node["natural"="peak"]({s2},{w2},{n2},{e2});
  way["highway"]["name"~"17th|28th|Wall|Washington|Harrison"]({s2},{w2},{n2},{e2});
  node["railway"~"station|halt"]({s},{w},{n},{e});
  way["railway"~"station|halt"]({s},{w},{n},{e});
  relation["railway"~"station|halt"]({s},{w},{n},{e});
  node["building"="train_station"]({s},{w},{n},{e});
  way["building"="train_station"]({s},{w},{n},{e});
);
out geom;
""".strip()


def fetch_osm(force: bool = False) -> Path:
    dest = OSM_DIR / "ogden.osm.json"
    log_path = OSM_DIR / "overpass_fetch.json"
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 10000 and not force:
        print(f"OSM cache hit: {dest} ({dest.stat().st_size} bytes)")
        return dest

    u = USER_BBOX
    t = TERRAIN_BBOX
    # Slightly larger than terrain so named streets / peaks on the rim are included.
    q = overpass_query(
        u["south"], u["west"], u["north"], u["east"],
        t["south"] - 0.005, t["west"] - 0.005, t["north"] + 0.005, t["east"] + 0.005,
    )
    (OSM_DIR / "overpass_query.overpassql").write_text(q + "\n")
    last_err = None
    started = now_iso()
    for endpoint in OVERPASS_ENDPOINTS:
        print(f"Overpass POST {endpoint} ...", flush=True)
        try:
            r = SESSION.post(endpoint, data={"data": q}, timeout=300)
            print(f"  status {r.status_code} bytes {len(r.content)}", flush=True)
            if r.status_code == 200 and r.content.startswith(b"{") or r.content[:1] == b"{":
                # validate json
                data = r.json()
                n = len(data.get("elements") or [])
                print(f"  elements {n}", flush=True)
                dest.write_bytes(r.content)
                log = {
                    "downloaded_at_utc": now_iso(),
                    "started_at_utc": started,
                    "endpoint": endpoint,
                    "bytes": dest.stat().st_size,
                    "elements": n,
                    "query_file": "overpass_query.overpassql",
                    "user_bbox": USER_BBOX,
                    "license": "ODbL 1.0, © OpenStreetMap contributors",
                }
                log_path.write_text(json.dumps(log, indent=2) + "\n")
                return dest
            last_err = f"{r.status_code} {r.text[:300]}"
        except Exception as e:
            last_err = repr(e)
            print(f"  failed: {last_err}", flush=True)
            time.sleep(5)
    raise RuntimeError(f"Overpass failed on all endpoints: {last_err}")


def fetch_dem(force: bool = False, resolution_m: float = 1.0) -> Path:
    DEM_DIR.mkdir(parents=True, exist_ok=True)
    dest = DEM_DIR / f"ogden_{resolution_m:g}m_utm12n.tif"
    log_path = DEM_DIR / "dem_fetch.json"
    if dest.exists() and dest.stat().st_size > 10000 and not force:
        print(f"DEM cache hit: {dest} ({dest.stat().st_size} bytes)")
        return dest

    gdal.UseExceptions()
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")
    os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
    os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
    os.environ.setdefault("GDAL_HTTP_VERSION", "2")
    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
    os.environ.setdefault("GDAL_HTTP_USERAGENT", USER_AGENT)

    sources = [f"/vsicurl/{tile['url']}" for tile in DEM_TILES]
    t = TERRAIN_BBOX
    started = now_iso()
    print(f"gdal.Warp -> {dest}  res={resolution_m}m  bbox={t}", flush=True)
    for src in sources:
        print(f"  source {src}", flush=True)

    opts = gdal.WarpOptions(
        format="GTiff",
        dstSRS="EPSG:32612",
        outputBounds=[t["west"], t["south"], t["east"], t["north"]],
        outputBoundsSRS="EPSG:4326",
        xRes=resolution_m,
        yRes=resolution_m,
        resampleAlg="bilinear",
        dstNodata=-9999,
        multithread=True,
        creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER", "PREDICTOR=2"],
        callback=_gdal_progress,
    )
    ds = gdal.Warp(str(dest), sources, options=opts)
    if ds is None:
        raise RuntimeError("gdal.Warp returned None")
    band = ds.GetRasterBand(1)
    stats = band.ComputeStatistics(False)
    info = {
        "width": ds.RasterXSize,
        "height": ds.RasterYSize,
        "geotransform": ds.GetGeoTransform(),
        "projection": ds.GetProjection(),
        "nodata": band.GetNoDataValue(),
        "stats_min_max_mean_std": stats,
    }
    ds = None
    print(f"  wrote {dest} ({dest.stat().st_size} bytes) {info['width']}x{info['height']}", flush=True)
    log = {
        "downloaded_at_utc": now_iso(),
        "started_at_utc": started,
        "output": str(dest.relative_to(ROOT)),
        "bytes": dest.stat().st_size,
        "resolution_m": resolution_m,
        "crs": "EPSG:32612",
        "terrain_bbox_wgs84": TERRAIN_BBOX,
        "user_bbox_wgs84": USER_BBOX,
        "method": "gdal.Warp windowed read via /vsicurl/ of official USGS 3DEP 1 m GeoTIFF tiles (no Google/proprietary sources)",
        "tiles": DEM_TILES,
        "raster": info,
        "license": "Public domain, USGS 3D Elevation Program (3DEP)",
    }
    log_path.write_text(json.dumps(log, indent=2) + "\n")
    return dest


def _gdal_progress(complete, message, unknown):
    pct = int(complete * 100)
    if pct % 5 == 0:
        print(f"  warp {pct}% {message or ''}", flush=True)
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--osm-only", action="store_true")
    ap.add_argument("--dem-only", action="store_true")
    ap.add_argument("--dem-res", type=float, default=1.0)
    args = ap.parse_args()
    if not args.dem_only:
        fetch_osm(force=args.force)
    if not args.osm_only:
        fetch_dem(force=args.force, resolution_m=args.dem_res)
    print("fetch_data complete", flush=True)


if __name__ == "__main__":
    main()
