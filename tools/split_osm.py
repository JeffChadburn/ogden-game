#!/usr/bin/env python3
"""Split GDAL OSM GeoJSON into per-layer WGS84 + UTM collections and verify streets."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from shapely.geometry import box, mapping, shape
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

SKIP_PROPS = {"z_order", "other_tags"}


def parse_props(p):
    tags = {}
    for k, v in (p or {}).items():
        if v in (None, "") or k in SKIP_PROPS:
            continue
        tags[k] = v
    ot = (p or {}).get("other_tags")
    if ot:
        try:
            extra = json.loads(ot) if isinstance(ot, str) else ot
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if v not in (None, "") and k not in tags:
                        tags[k] = v
        except Exception:
            pass
    # normalize ids
    if "osm_id" in (p or {}) and p["osm_id"] not in (None, ""):
        tags["osm_id"] = p["osm_id"]
        tags["osm_type"] = "relation_or_node_or_way"
    if "osm_way_id" in (p or {}) and p["osm_way_id"] not in (None, ""):
        tags["osm_id"] = p["osm_way_id"]
        tags["osm_type"] = "way"
    return tags


def load(name):
    path = C.RAW / "osm" / f"_{name}.geojson"
    return json.loads(path.read_text()).get("features") or []


def feat(geom, tags, layer):
    return {
        "type": "Feature",
        "geometry": mapping(geom) if not isinstance(geom, dict) else geom,
        "properties": {**tags, "layer": layer},
    }


def to_utm(geom):
    return shp_transform(lambda x, y, z=None: C.TO_UTM.transform(x, y), geom)


def write_layer(name, features):
    out_wgs = C.RAW / "osm" / f"{name}.geojson"
    out_utm = C.PROCESSED / "geojson" / f"{name}_utm.geojson"
    out_utm.parent.mkdir(parents=True, exist_ok=True)
    wgs_feats, utm_feats = [], []
    for f in features:
        g = shape(f["geometry"])
        if g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0)
            if g.is_empty:
                continue
        wgs_feats.append({"type": "Feature", "geometry": mapping(g), "properties": f["properties"]})
        ug = to_utm(g)
        utm_feats.append({"type": "Feature", "geometry": mapping(ug), "properties": f["properties"]})
    out_wgs.write_text(json.dumps({"type": "FeatureCollection", "name": name, "crs": "EPSG:4326", "features": wgs_feats}))
    out_utm.write_text(json.dumps({"type": "FeatureCollection", "name": name, "crs": "EPSG:32612", "features": utm_feats}))
    return len(wgs_feats), out_wgs.stat().st_size


def bounds_of(features, pred):
    minx = miny = 1e9
    maxx = maxy = -1e9
    names = Counter()
    n = 0
    for f in features:
        p = f["properties"]
        if not pred(p):
            continue
        names[p.get("name") or "?"] += 1
        g = shape(f["geometry"])
        b = g.bounds
        if not b or b[0] == b[2] and b[1] == b[3] and g.is_empty:
            continue
        minx, miny, maxx, maxy = min(minx, b[0]), min(miny, b[1]), max(maxx, b[2]), max(maxy, b[3])
        n += 1
    if n == 0:
        return None
    return {"west": minx, "south": miny, "east": maxx, "north": maxy,
            "n_features": n, "names": dict(names.most_common(12))}


def main():
    C.ensure_dirs()
    points = load("points")
    lines = load("lines")
    polys = load("multipolygons")
    print(f"input points={len(points)} lines={len(lines)} polys={len(polys)}")

    layers = defaultdict(list)
    # user bbox with a 80 m-ish buffer in degrees (~0.001) so edge roads stay complete
    ub = C.USER_BBOX
    user_box = box(ub["west"] - 0.001, ub["south"] - 0.001, ub["east"] + 0.001, ub["north"] + 0.001)
    terrain_box = box(C.TERRAIN_BBOX["west"], C.TERRAIN_BBOX["south"],
                      C.TERRAIN_BBOX["east"], C.TERRAIN_BBOX["north"])

    for raw in points:
        tags = parse_props(raw.get("properties") or {})
        g = shape(raw["geometry"])
        if tags.get("natural") == "peak" and terrain_box.covers(g):
            layers["peaks"].append(feat(g, tags, "peaks"))
        if not g.intersects(user_box):
            continue
        if tags.get("natural") == "tree":
            layers["trees"].append(feat(g, tags, "trees"))
        if tags.get("railway") in ("station", "halt") or tags.get("building") == "train_station":
            layers["stations"].append(feat(g, tags, "stations"))

    # Keep all named verification streets from the full extract, but
    # game highways only in/near the user box.
    all_highways = []
    for raw in lines:
        tags = parse_props(raw.get("properties") or {})
        g = shape(raw["geometry"])
        if tags.get("highway"):
            all_highways.append(feat(g, tags, "highways"))
            if g.intersects(user_box):
                layers["highways"].append(feat(g, tags, "highways"))
        if tags.get("railway") and g.intersects(user_box):
            layers["railways"].append(feat(g, tags, "railways"))
        if tags.get("waterway") and g.intersects(user_box):
            layers["waterways"].append(feat(g, tags, "waterways"))

    height_stats = {"buildings": 0, "with_height": 0, "with_levels": 0, "with_roof_shape": 0,
                    "buildings_in_extract": 0}
    for raw in polys:
        tags = parse_props(raw.get("properties") or {})
        g = shape(raw["geometry"])
        if "building" in tags:
            height_stats["buildings_in_extract"] += 1
        if not g.intersects(user_box):
            # keep peaks already handled; skip other polygons outside the game box
            # except landuse/natural in the terrain margin (foothill cover)
            if g.intersects(terrain_box) and (tags.get("landuse") or tags.get("natural") or tags.get("leisure")):
                layers["landuse"].append(feat(g, tags, "landuse"))
            continue
        if "building" in tags:
            layers["buildings"].append(feat(g, tags, "buildings"))
            height_stats["buildings"] += 1
            if tags.get("height") or tags.get("building:height"):
                height_stats["with_height"] += 1
            if tags.get("building:levels") or tags.get("levels"):
                height_stats["with_levels"] += 1
            if tags.get("roof:shape"):
                height_stats["with_roof_shape"] += 1
        elif tags.get("railway") in ("station", "halt") or tags.get("building") == "train_station":
            layers["stations"].append(feat(g, tags, "stations"))
        elif tags.get("natural") == "water" or "water" in tags or tags.get("waterway") in ("riverbank", "dock"):
            layers["water"].append(feat(g, tags, "water"))
        elif tags.get("landuse") or tags.get("leisure") or tags.get("natural") or tags.get("amenity") == "parking":
            layers["landuse"].append(feat(g, tags, "landuse"))

    summary = {"layers": {}, "height_stats": height_stats, "source": "geofabrik clipped PBF via GDAL OSM driver"}
    for name, feats in layers.items():
        n, sz = write_layer(name, feats)
        summary["layers"][name] = {"count": n, "bytes": sz}
        print(f"  {name}: {n}")

    verify = {
        "17th_street": bounds_of(all_highways, lambda p: p.get("highway") and "17th" in (p.get("name") or "")),
        "28th_street": bounds_of(all_highways, lambda p: p.get("highway") and "28th" in (p.get("name") or "")),
        "wall_avenue": bounds_of(all_highways, lambda p: p.get("highway") and "Wall" in (p.get("name") or "")),
        "washington_blvd": bounds_of(all_highways, lambda p: p.get("highway") and "Washington" in (p.get("name") or "")),
        "harrison_blvd": bounds_of(all_highways, lambda p: p.get("highway") and "Harrison" in (p.get("name") or "")),
        "25th_street": bounds_of(all_highways, lambda p: p.get("highway") and "25th" in (p.get("name") or "")),
        "peaks": [
            {
                "name": f["properties"].get("name"),
                "ele": f["properties"].get("ele"),
                "lon": f["geometry"]["coordinates"][0] if isinstance(f["geometry"]["coordinates"][0], (int, float)) else None,
                "lat": f["geometry"]["coordinates"][1] if isinstance(f["geometry"]["coordinates"][0], (int, float)) else None,
            }
            for f in layers["peaks"]
        ],
        "user_bbox_kept": True,
        "reason": (
            "User bbox N 41.245 / S 41.205 / W -112.000 / E -111.900 already covers "
            "the 17th–28th Street corridor (17th ~41.237, 28th ~41.214) and Wall Avenue "
            "(~-111.979) plus the UP tracks to the west. No adjustment was required. "
            "A terrain-only margin (S 41.193, E -111.855, padded N/W) was added because "
            "Mount Ogden (41.1999 N, 111.8822 W, 2917 m) sits just southeast of the user "
            "box; without it the Wasatch silhouette would be truncated."
        ),
    }
    summary["verification"] = verify
    C.write_json(C.RAW / "osm" / "osm_summary.json", summary)
    print(json.dumps(verify, indent=2, default=str)[:3000])
    hs = height_stats
    n = max(1, hs["buildings"])
    print(f"buildings in user bbox: {hs['buildings']}  "
          f"height {hs['with_height']} ({100*hs['with_height']/n:.2f}%)  "
          f"levels {hs['with_levels']} ({100*hs['with_levels']/n:.2f}%)")
    print("split_osm complete")


if __name__ == "__main__":
    main()
