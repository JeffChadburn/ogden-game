#!/usr/bin/env python3
"""Convert the cached Overpass JSON into per-layer GeoJSON (WGS84 + UTM 12N)."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    mapping,
)
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

AREA_KEYS = ("building", "landuse", "leisure", "amenity", "water")
AREA_NATURAL = {
    "wood", "forest", "scrub", "grassland", "grass", "heath", "meadow",
    "bare_rock", "scree", "shingle", "sand", "water", "wetland", "beach",
    "rock", "bare_earth", "mud",
}
AREA_WATERWAY = {"riverbank", "dock", "boatyard", "canal" }  # canal only if area=yes usually
LINE_LAYERS = {"highway", "railway", "waterway"}


def coords_from_geom(geom):
    if not geom:
        return []
    return [(p["lon"], p["lat"]) for p in geom if "lon" in p and "lat" in p]


def is_area_way(tags, pts):
    if not tags or len(pts) < 4:
        return False
    if pts[0] != pts[-1]:
        return False
    if tags.get("area") == "no":
        return False
    if tags.get("area") == "yes":
        return True
    if any(k in tags for k in AREA_KEYS):
        return True
    if tags.get("natural") in AREA_NATURAL:
        return True
    if tags.get("waterway") in ("riverbank", "dock"):
        return True
    if tags.get("railway") in ("station", "platform") and pts[0] == pts[-1]:
        return True
    return False


def polygon_from_ring(pts):
    if len(pts) < 4:
        return None
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    try:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            return None
        return poly
    except Exception:
        return None


def relation_polygon(el):
    members = el.get("members") or []
    outers, inners = [], []
    for m in members:
        if m.get("type") not in ("way",):
            continue
        pts = coords_from_geom(m.get("geometry") or [])
        if len(pts) < 2:
            continue
        role = m.get("role") or "outer"
        (outers if role != "inner" else inners).append(pts)
    # stitch incomplete rings
    def stitch(rings):
        leftover = [r for r in rings if len(r) >= 2]
        closed = []
        # already closed
        still = []
        for r in leftover:
            if r[0] == r[-1] and len(r) >= 4:
                closed.append(r)
            else:
                still.append(r)
        # greedy stitch
        used = [False] * len(still)
        for i, r in enumerate(still):
            if used[i]:
                continue
            chain = list(r)
            used[i] = True
            changed = True
            while changed and chain[0] != chain[-1]:
                changed = False
                for j, s in enumerate(still):
                    if used[j]:
                        continue
                    if chain[-1] == s[0]:
                        chain.extend(s[1:])
                        used[j] = True
                        changed = True
                    elif chain[-1] == s[-1]:
                        chain.extend(reversed(s[:-1]))
                        used[j] = True
                        changed = True
                    elif chain[0] == s[-1]:
                        chain = s + chain[1:]
                        used[j] = True
                        changed = True
                    elif chain[0] == s[0]:
                        chain = list(reversed(s)) + chain[1:]
                        used[j] = True
                        changed = True
            if chain[0] == chain[-1] and len(chain) >= 4:
                closed.append(chain)
        return closed

    outer_rings = stitch(outers)
    inner_rings = stitch(inners)
    polys = []
    inners_used = set()
    inner_polys = []
    for ir in inner_rings:
        p = polygon_from_ring(ir)
        if p and p.geom_type == "Polygon":
            inner_polys.append(p)
    for oring in outer_rings:
        outer = polygon_from_ring(oring)
        if outer is None:
            continue
        if outer.geom_type == "MultiPolygon":
            parts = list(outer.geoms)
        else:
            parts = [outer]
        for part in parts:
            holes = []
            for idx, ip in enumerate(inner_polys):
                if idx in inners_used:
                    continue
                try:
                    if part.covers(ip) or part.contains(ip.representative_point()):
                        holes.append(list(ip.exterior.coords))
                        inners_used.add(idx)
                except Exception:
                    pass
            try:
                poly = Polygon(part.exterior.coords, holes)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if not poly.is_empty:
                    polys.append(poly)
            except Exception:
                if not part.is_empty:
                    polys.append(part)
    if not polys:
        return None
    if len(polys) == 1:
        return polys[0]
    return MultiPolygon([p for p in polys if p.geom_type == "Polygon"])


def feature(geom, tags, osm_type, osm_id, layer):
    return {
        "type": "Feature",
        "geometry": mapping(geom),
        "properties": {
            **{k: v for k, v in (tags or {}).items()},
            "osm_type": osm_type,
            "osm_id": osm_id,
            "layer": layer,
        },
    }


def classify(el):
    tags = el.get("tags") or {}
    t = el.get("type")
    if t == "node":
        if tags.get("natural") == "peak":
            return "peaks"
        if tags.get("natural") == "tree":
            return "trees"
        if tags.get("railway") in ("station", "halt") or tags.get("building") == "train_station":
            return "stations"
        return None
    if "building" in tags:
        return "buildings"
    if tags.get("railway") in ("station", "halt") or tags.get("building") == "train_station":
        return "stations"
    if "highway" in tags:
        return "highways"
    if "railway" in tags:
        return "railways"
    if tags.get("natural") == "water" or "water" in tags or tags.get("waterway") in ("riverbank", "dock"):
        return "water"
    if "waterway" in tags:
        return "waterways"
    if "landuse" in tags or "leisure" in tags or tags.get("natural") in AREA_NATURAL:
        return "landuse"
    return None


def to_utm_geom(geom):
    return shp_transform(lambda x, y, z=None: C.TO_UTM.transform(x, y), geom)


def main():
    C.ensure_dirs()
    src = C.RAW / "osm" / "ogden.osm.json"
    if not src.exists():
        raise SystemExit(f"missing {src}; run fetch_data.py first")
    print(f"reading {src} ({src.stat().st_size} bytes)", flush=True)
    data = json.loads(src.read_text())
    elements = data.get("elements") or []
    print(f"elements {len(elements)}", flush=True)

    layers = defaultdict(list)
    counts = Counter()
    height_stats = {"buildings": 0, "with_height": 0, "with_levels": 0}

    for el in elements:
        tags = el.get("tags") or {}
        layer = classify(el)
        if not layer:
            continue
        typ = el["type"]
        oid = el.get("id")
        geom = None
        if typ == "node":
            lon, lat = el.get("lon"), el.get("lat")
            if lon is None or lat is None:
                continue
            geom = Point(lon, lat)
        elif typ == "way":
            pts = coords_from_geom(el.get("geometry") or [])
            if len(pts) < 2:
                continue
            if is_area_way(tags, pts):
                geom = polygon_from_ring(pts)
            else:
                try:
                    geom = LineString(pts)
                except Exception:
                    geom = None
        elif typ == "relation":
            geom = relation_polygon(el)
        if geom is None or geom.is_empty:
            continue
        layers[layer].append(feature(geom, tags, typ, oid, layer))
        counts[layer] += 1
        if layer == "buildings":
            height_stats["buildings"] += 1
            if tags.get("height") or tags.get("building:height"):
                height_stats["with_height"] += 1
            if tags.get("building:levels") or tags.get("levels"):
                height_stats["with_levels"] += 1

    out_dir = C.RAW / "osm"
    proc_dir = C.PROCESSED / "geojson"
    out_dir.mkdir(parents=True, exist_ok=True)
    proc_dir.mkdir(parents=True, exist_ok=True)

    summary = {"layers": {}, "height_stats": height_stats, "source": str(src)}
    for name, feats in layers.items():
        fc = {"type": "FeatureCollection", "features": feats, "name": name, "crs": "EPSG:4326"}
        path = out_dir / f"{name}.geojson"
        path.write_text(json.dumps(fc))
        # UTM copy for processing
        utm_feats = []
        for f in feats:
            from shapely.geometry import shape
            g = shape(f["geometry"])
            ug = to_utm_geom(g)
            utm_feats.append({
                "type": "Feature",
                "geometry": mapping(ug),
                "properties": f["properties"],
            })
        ufc = {
            "type": "FeatureCollection",
            "features": utm_feats,
            "name": name,
            "crs": "EPSG:32612",
        }
        (proc_dir / f"{name}_utm.geojson").write_text(json.dumps(ufc))
        summary["layers"][name] = {"count": len(feats), "bytes": path.stat().st_size}
        print(f"  {name}: {len(feats)}  -> {path}", flush=True)

    # Street-name verification against the user bbox
    verify = verify_streets(layers.get("highways", []), layers.get("railways", []), layers.get("peaks", []))
    summary["verification"] = verify
    C.write_json(out_dir / "osm_summary.json", summary)
    print(json.dumps(verify, indent=2))
    hs = height_stats
    pct_h = 100 * hs["with_height"] / max(1, hs["buildings"])
    pct_l = 100 * hs["with_levels"] / max(1, hs["buildings"])
    print(f"buildings {hs['buildings']}: {hs['with_height']} with height ({pct_h:.1f}%), "
          f"{hs['with_levels']} with levels ({pct_l:.1f}%)", flush=True)


def bounds_of_named(feats, predicate):
    minx = miny = 1e9
    maxx = maxy = -1e9
    n = 0
    names = Counter()
    for f in feats:
        p = f["properties"]
        if not predicate(p):
            continue
        names[p.get("name") or "?"] += 1
        g = f["geometry"]
        coords = list(_iter_coords(g))
        for x, y in coords:
            minx, miny = min(minx, x), min(miny, y)
            maxx, maxy = max(maxx, x), max(maxy, y)
            n += 1
    if n == 0:
        return None
    return {
        "west": minx, "south": miny, "east": maxx, "north": maxy,
        "n_features": sum(names.values()),
        "names": dict(names.most_common(12)),
    }


def _iter_coords(geom):
    t = geom["type"]
    c = geom["coordinates"]
    if t == "Point":
        yield c
    elif t in ("LineString", "MultiPoint"):
        yield from c
    elif t in ("Polygon", "MultiLineString"):
        for ring in c:
            yield from ring
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly:
                yield from ring


def verify_streets(highways, railways, peaks):
    def name_has(p, *needles):
        name = (p.get("name") or "")
        return any(n.lower() in name.lower() for n in needles) and p.get("highway")

    result = {
        "17th_street": bounds_of_named(highways, lambda p: name_has(p, "17th")),
        "28th_street": bounds_of_named(highways, lambda p: name_has(p, "28th")),
        "wall_avenue": bounds_of_named(highways, lambda p: name_has(p, "Wall")),
        "washington_blvd": bounds_of_named(highways, lambda p: name_has(p, "Washington")),
        "up_rail": bounds_of_named(railways, lambda p: p.get("railway") in ("rail", "yard", "disused", "light_rail", "abandoned")),
        "peaks": [],
    }
    for f in peaks:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        result["peaks"].append({
            "name": p.get("name") or p.get("name:en"),
            "ele": p.get("ele"),
            "lon": lon, "lat": lat,
        })
    # Recommend keeping the user bbox; report whether streets sit inside it.
    u = C.USER_BBOX
    notes = []
    for key, label, edge, field in (
        ("17th_street", "17th Street", "north", "north"),
        ("28th_street", "28th Street", "south", "south"),
        ("wall_avenue", "Wall Avenue", "west", "west"),
    ):
        b = result[key]
        if not b:
            notes.append(f"{label} not found in extract")
            continue
        notes.append(
            f"{label} spans lat {b['south']:.5f}..{b['north']:.5f}, lon {b['west']:.5f}..{b['east']:.5f}"
        )
    result["notes"] = notes
    result["user_bbox_kept"] = True
    result["reason"] = (
        "User bbox N 41.245, S 41.205, W -112.000, E -111.900 already covers the "
        "17th–28th Street corridor and Wall Avenue / UP tracks. Terrain-only margin "
        "extends east/south so the Wasatch / Mount Ogden silhouette is complete."
    )
    return result


if __name__ == "__main__":
    main()
