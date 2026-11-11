"""Shared paths, bounding boxes, and CRS helpers for the Ogden pipeline."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyproj

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
EXPORT = ROOT / "export"
SCREENS = ROOT / "screens"
ASSETS = ROOT / "assets"
WEB = ROOT / "web"

USER_BBOX = {
    "south": 41.205,
    "west": -112.000,
    "north": 41.245,
    "east": -111.900,
}
TERRAIN_BBOX = {
    "south": 41.193,
    "west": -112.003,
    "north": 41.247,
    "east": -111.855,
}

CRS_GEOG = "EPSG:4326"
CRS_PROJ = "EPSG:32612"  # WGS84 UTM zone 12N, meters

TO_UTM = pyproj.Transformer.from_crs(CRS_GEOG, CRS_PROJ, always_xy=True)
TO_LL = pyproj.Transformer.from_crs(CRS_PROJ, CRS_GEOG, always_xy=True)


def ll_to_utm(lon, lat):
    return TO_UTM.transform(lon, lat)


def utm_to_ll(x, y):
    return TO_LL.transform(x, y)


def ensure_dirs():
    for p in (
        RAW / "osm",
        RAW / "dem",
        RAW / "meta",
        PROCESSED / "terrain",
        PROCESSED / "geojson",
        PROCESSED / "chunks",
        ASSETS / "textures",
        EXPORT,
        SCREENS,
        WEB,
        ROOT / "docs",
        ROOT / "unity",
        ROOT / "js",
    ):
        p.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=_json_default) + "\n")


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


# Game-space origin: southwest corner of the 1 m DEM, filled in after the
# raster is opened (see load_origin). Until then we use a stable fallback
# computed from TERRAIN_BBOX.
_fallback_x, _fallback_y = ll_to_utm(TERRAIN_BBOX["west"], TERRAIN_BBOX["south"])
ORIGIN_X = float(_fallback_x)
ORIGIN_Y = float(_fallback_y)


def load_origin_from_dem(dem_path: Path | None = None):
    """Set ORIGIN_X/Y to the DEM's southwest corner (UTM 12N meters)."""
    global ORIGIN_X, ORIGIN_Y
    from osgeo import gdal

    dem_path = dem_path or (RAW / "dem" / "ogden_1m_utm12n.tif")
    ds = gdal.Open(str(dem_path))
    if ds is None:
        return ORIGIN_X, ORIGIN_Y
    gt = ds.GetGeoTransform()
    ORIGIN_X = float(gt[0])
    ORIGIN_Y = float(gt[3] + ds.RasterYSize * gt[5])  # south (gt[5] is negative)
    ds = None
    return ORIGIN_X, ORIGIN_Y


def to_game(easting, northing):
    """UTM meters -> game meters (X east, Z north, origin at DEM SW)."""
    return float(easting - ORIGIN_X), float(northing - ORIGIN_Y)
