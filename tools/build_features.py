#!/usr/bin/env python3
"""Build road, rail, building, water, park, and tree meshes as chunked GLBs."""
from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

try:
    import rasterio
except ImportError:
    rasterio = None

import trimesh

CHUNK = 256.0  # meters, city feature grid
ROAD_WIDTH = {
    "motorway": 16.0, "motorway_link": 8.0,
    "trunk": 14.0, "trunk_link": 8.0,
    "primary": 12.0, "primary_link": 7.0,
    "secondary": 10.0, "secondary_link": 6.5,
    "tertiary": 8.5, "tertiary_link": 6.0,
    "residential": 7.0, "living_street": 6.5,
    "unclassified": 6.5, "service": 4.5,
    "alley": 3.5, "track": 3.2, "path": 1.6,
    "footway": 2.0, "cycleway": 2.0, "bridleway": 2.0,
    "pedestrian": 5.0, "steps": 2.0, "corridor": 2.5,
}
SIDEWALK_TYPES = {
    "residential", "living_street", "tertiary", "secondary", "primary",
    "unclassified", "trunk",
}
MAJOR_TYPES = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "residential", "unclassified", "living_street",
}
BUILDING_H = {
    "house": 6.0, "detached": 6.2, "terrace": 6.0, "semidetached_house": 6.0,
    "apartments": 12.0, "residential": 8.0, "hut": 3.5, "cabin": 5.0,
    "commercial": 9.0, "retail": 7.5, "office": 12.0, "warehouse": 8.5,
    "industrial": 8.5, "hangar": 10.0, "school": 10.0, "university": 12.0,
    "church": 12.0, "religious": 10.0, "garage": 3.0, "garages": 3.0,
    "shed": 3.0, "roof": 3.0, "parking": 3.5, "construction": 6.0,
    "hospital": 14.0, "hotel": 14.0, "train_station": 14.0, "yes": None,
}
PITCHED_TYPES = {
    "house", "detached", "terrace", "semidetached_house", "residential",
    "apartments", "hut", "cabin", "church", "religious",
}


class DemSampler:
    def __init__(self, path):
        self.ds = rasterio.open(path)
        self.band = 1
        # cache a downsampled array for fast bilinear samples
        self.arr = self.ds.read(1).astype(np.float32)
        nd = self.ds.nodata
        if nd is not None:
            self.arr = np.where(self.arr == nd, np.nan, self.arr)
        self.gt = self.ds.transform
        self.h, self.w = self.arr.shape
        # fill small holes
        if np.isnan(self.arr).any():
            nan = np.isnan(self.arr)
            self.arr[nan] = np.nanmean(self.arr)

    def sample(self, x, y):
        # x,y can be arrays of UTM coords
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        # affine: x = gt.a * col + gt.c ; y = gt.e * row + gt.f  (e is negative)
        col = (x - self.gt.c) / self.gt.a
        row = (y - self.gt.f) / self.gt.e
        r0 = np.floor(row).astype(int)
        c0 = np.floor(col).astype(int)
        dr = row - r0
        dc = col - c0
        r0 = np.clip(r0, 0, self.h - 2)
        c0 = np.clip(c0, 0, self.w - 2)
        z00 = self.arr[r0, c0]
        z10 = self.arr[r0, c0 + 1]
        z01 = self.arr[r0 + 1, c0]
        z11 = self.arr[r0 + 1, c0 + 1]
        return (z00 * (1 - dc) + z10 * dc) * (1 - dr) + (z01 * (1 - dc) + z11 * dc) * dr

    def min_in_poly(self, geom):
        minx, miny, maxx, maxy = geom.bounds
        xs = np.arange(minx, maxx + 2, 2.0)
        ys = np.arange(miny, maxy + 2, 2.0)
        if len(xs) == 0 or len(ys) == 0:
            c = geom.representative_point()
            return float(self.sample(c.x, c.y))
        xx, yy = np.meshgrid(xs, ys)
        # cheap bbox min (skirt covers residual)
        return float(np.min(self.sample(xx.ravel(), yy.ravel())))


def load_fc(name):
    path = C.PROCESSED / "geojson" / f"{name}_utm.geojson"
    if not path.exists():
        return []
    return json.loads(path.read_text())["features"]


def parse_m(val):
    if val is None:
        return None
    s = str(val).strip().lower().replace(",", " ").split()[0]
    s = s.replace("m", "")
    try:
        return float(s)
    except ValueError:
        return None


def road_width(tags):
    w = parse_m(tags.get("width"))
    typ = tags.get("highway") or ""
    base = ROAD_WIDTH.get(typ, 6.0)
    lanes = parse_m(tags.get("lanes"))
    if lanes and typ not in ("footway", "path", "cycleway", "steps"):
        base = max(base, lanes * 3.2)
    if w:
        return max(w, 1.5)
    return base


def building_height(tags, area):
    h = parse_m(tags.get("height") or tags.get("building:height"))
    if h:
        return max(2.5, min(h, 80.0)), "height"
    lv = parse_m(tags.get("building:levels") or tags.get("levels"))
    if lv:
        return max(3.0, lv * 3.2), "levels"
    typ = (tags.get("building") or "yes").lower()
    d = BUILDING_H.get(typ)
    if d:
        return d, "type"
    if area < 80:
        return 5.0, "area"
    if area < 250:
        return 6.5, "area"
    if area < 800:
        return 8.0, "area"
    if area < 2000:
        return 10.0, "area"
    return 12.0, "area"


def facade_class(tags):
    typ = (tags.get("building") or "yes").lower()
    if typ in ("commercial", "retail", "office", "hotel", "train_station"):
        return "commercial"
    if typ in ("industrial", "warehouse", "hangar", "garage", "garages"):
        return "industrial"
    if typ in ("school", "university", "church", "religious", "hospital"):
        return "civic"
    if typ in ("house", "detached", "terrace", "semidetached_house", "apartments",
               "residential", "hut", "cabin"):
        return "residential"
    # generic "yes": pick by area later
    return "residential" if typ in ("yes",) else "commercial"


def want_pitched(tags, fclass):
    shape = (tags.get("roof:shape") or "").lower()
    if shape in ("flat", "flat_roof"):
        return False
    if shape in ("gabled", "hipped", "pitched", "gambrel", "pyramidal"):
        return True
    typ = (tags.get("building") or "").lower()
    if typ in PITCHED_TYPES:
        return True
    return False


def resample_line(coords, step=4.0):
    pts = np.asarray(coords, dtype=np.float64)
    if len(pts) < 2:
        return pts
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    dist = np.concatenate([[0], np.cumsum(seg)])
    total = dist[-1]
    if total < 0.5:
        return pts
    n = max(2, int(math.ceil(total / step)) + 1)
    t = np.linspace(0, total, n)
    x = np.interp(t, dist, pts[:, 0])
    y = np.interp(t, dist, pts[:, 1])
    return np.column_stack([x, y])


def ribbon(center_xy, z, width, y_off=0.04, uv_v=1.0):
    """Build a draped ribbon mesh. center_xy (N,2), z (N,)."""
    n = len(center_xy)
    if n < 2:
        return None
    tang = np.zeros_like(center_xy)
    tang[1:-1] = center_xy[2:] - center_xy[:-2]
    tang[0] = center_xy[1] - center_xy[0]
    tang[-1] = center_xy[-1] - center_xy[-2]
    norms = np.linalg.norm(tang, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-6)
    tang = tang / norms
    nrm = np.column_stack([-tang[:, 1], tang[:, 0]])
    hw = width * 0.5
    left = center_xy + nrm * hw
    right = center_xy - nrm * hw
    z = z + y_off
    verts = []
    uvs = []
    dist = 0.0
    for i in range(n):
        if i:
            dist += float(np.linalg.norm(center_xy[i] - center_xy[i - 1]))
        gx, gz = C.to_game(left[i, 0], left[i, 1])
        verts.append((gx, float(z[i]), gz))
        uvs.append((0.0, dist / 8.0))
        gx, gz = C.to_game(right[i, 0], right[i, 1])
        verts.append((gx, float(z[i]), gz))
        uvs.append((uv_v, dist / 8.0))
    faces = []
    for i in range(n - 1):
        a = i * 2
        faces.append((a, a + 2, a + 1))
        faces.append((a + 1, a + 2, a + 3))
    return np.asarray(verts, np.float32), np.asarray(faces, np.int32), np.asarray(uvs, np.float32)


def merge_meshes(parts, color=None, name="mesh"):
    parts = [p for p in parts if p is not None and len(p[1]) > 0]
    if not parts:
        return None
    vs, fs, uvs = [], [], []
    off = 0
    for v, f, uv in parts:
        vs.append(v)
        fs.append(f + off)
        uvs.append(uv if uv is not None else np.zeros((len(v), 2), np.float32))
        off += len(v)
    v = np.vstack(vs)
    f = np.vstack(fs)
    uv = np.vstack(uvs)
    mesh = trimesh.Trimesh(vertices=v, faces=f, process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv)
    if color is not None:
        rgba = np.array(list(color) + [255], dtype=np.uint8)
        mesh.visual.vertex_colors = np.tile(rgba, (len(v), 1))
    mesh.metadata["name"] = name
    return mesh


def triangulate_poly(poly, sampler, y_off=0.03):
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        parts = [triangulate_poly(p, sampler, y_off) for p in poly.geoms]
        parts = [p for p in parts if p]
        if not parts:
            return None
        return merge_meshes(parts)
    if poly.geom_type != "Polygon" or poly.area < 2:
        return None
    try:
        from shapely import affinity  # noqa
        # trimesh triangulation from vertices of exterior + interiors via earcut
        import trimesh.creation
        from trimesh.geometry import triangulate_polygon as tri_poly
    except Exception:
        tri_poly = None
    ext = np.asarray(poly.exterior.coords)[:, :2]
    if len(ext) < 4:
        return None
    holes = [np.asarray(r.coords)[:, :2] for r in poly.interiors if len(r.coords) >= 4]
    try:
        verts2, faces = trimesh.creation.triangulate_polygon(
            type("P", (), {"vertices": ext[:-1] if np.allclose(ext[0], ext[-1]) else ext,
                           }) 
        )
    except Exception:
        verts2, faces = None, None
    # Use shapely + mapbox earcut via trimesh if possible
    try:
        tv, tf = trimesh.triangles.triangulate_polygon(poly)  # may not exist
    except Exception:
        tv = None
    if tv is None:
        # fallback: earcut through trimesh path
        try:
            from trimesh.visual.texture import TextureVisuals  # noqa
            from shapely.geometry import Polygon as SPoly
            # trimesh.creation.extrude_polygon then take top? too heavy
            # simple fan triangulation of exterior only (skip holes)
            pts = ext[:-1] if np.allclose(ext[0], ext[-1]) else ext
            if len(pts) < 3:
                return None
            # drop colinear-ish
            c = pts.mean(axis=0)
            verts2 = pts
            faces = []
            for i in range(1, len(pts) - 1):
                faces.append((0, i, i + 1))
            faces = np.asarray(faces, np.int32)
        except Exception:
            return None
    else:
        verts2, faces = tv, tf
    z = sampler.sample(verts2[:, 0], verts2[:, 1]) + y_off
    gx, gz = C.to_game(verts2[:, 0], verts2[:, 1])
    # to_game on arrays:
    gx = verts2[:, 0] - C.ORIGIN_X
    gz = verts2[:, 1] - C.ORIGIN_Y
    verts = np.column_stack([gx, z, gz]).astype(np.float32)
    uvs = np.column_stack([gx / 16.0, gz / 16.0]).astype(np.float32)
    return verts, np.asarray(faces, np.int32), uvs


def triangulate_poly2(poly, sampler, y_off=0.03):
    """Robust triangulation using triangle-free ear clipping via shapely.buffer(0) + matplotlib? 
    Use trimesh's triangulate if available, else fan from centroid for convex-ish parks.
    """
    if poly is None or poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        parts = [triangulate_poly2(p, sampler, y_off) for p in poly.geoms]
        parts = [p for p in parts if p]
        return merge_meshes(parts) if parts else None
    if poly.geom_type != "Polygon" or poly.area < 1.5:
        return None
    # Try earcut (mapbox) via trimesh
    try:
        from trimesh.geometry import triangulate_polygon
        verts2, faces = triangulate_polygon(poly, engine=None)
        verts2 = np.asarray(verts2)[:, :2]
        faces = np.asarray(faces)
    except Exception:
        # centroid fan, only if no holes and reasonably convex
        pts = np.asarray(poly.exterior.coords)[:, :2]
        if np.allclose(pts[0], pts[-1]):
            pts = pts[:-1]
        if len(pts) < 3:
            return None
        c = np.array([poly.centroid.x, poly.centroid.y])
        verts2 = np.vstack([c, pts])
        faces = np.array([[0, i, i + 1] for i in range(1, len(pts))] + [[0, len(pts), 1]], np.int32)
    z = sampler.sample(verts2[:, 0], verts2[:, 1]) + y_off
    gx = verts2[:, 0] - C.ORIGIN_X
    gz = verts2[:, 1] - C.ORIGIN_Y
    verts = np.column_stack([gx, z, gz]).astype(np.float32)
    uvs = np.column_stack([gx / 16.0, gz / 16.0]).astype(np.float32)
    return verts, np.asarray(faces, np.int32), uvs


def extrude_building(poly, zmin, height, pitched, fclass, oid):
    """Walls + roof + skirt. Returns (verts, faces, uvs, colors)."""
    if poly.geom_type != "Polygon":
        if poly.geom_type == "MultiPolygon":
            parts = []
            for p in poly.geoms:
                r = extrude_building(p, zmin, height, pitched, fclass, oid)
                if r:
                    parts.append(r)
            if not parts:
                return None
            vs = np.vstack([p[0] for p in parts])
            fs, off = [], 0
            for v, f, u, c in parts:
                fs.append(f + off)
                off += len(v)
            return vs, np.vstack(fs), np.vstack([p[2] for p in parts]), np.vstack([p[3] for p in parts])
        return None
    ext = np.asarray(poly.exterior.coords)[:, :2]
    if np.allclose(ext[0], ext[-1]):
        ext = ext[:-1]
    if len(ext) < 3:
        return None
    skirt = 3.0
    z_bot = zmin - skirt
    z_top = zmin + height
    # walls
    verts = []
    uvs = []
    colors = []
    faces = []
    pal = {
        "residential": (160, 90, 70),
        "commercial": (180, 185, 190),
        "industrial": (130, 135, 140),
        "civic": (170, 165, 155),
    }
    base_col = pal.get(fclass, (150, 140, 130))
    # slight variation from id
    hsh = (int(oid) * 17) % 25 - 12 if str(oid).isdigit() else 0
    col = tuple(int(np.clip(c + hsh, 0, 255)) for c in base_col)
    dist = 0.0
    n = len(ext)
    for i in range(n):
        a = ext[i]
        b = ext[(i + 1) % n]
        seg = float(np.linalg.norm(b - a))
        if seg < 0.15:
            continue
        ax, az = a[0] - C.ORIGIN_X, a[1] - C.ORIGIN_Y
        bx, bz = b[0] - C.ORIGIN_X, b[1] - C.ORIGIN_Y
        v0 = len(verts)
        verts += [
            (ax, z_bot, az), (bx, z_bot, bz),
            (bx, z_top, bz), (ax, z_top, az),
        ]
        # UV: u along wall in "window bays" of 4 m, v from 0 at floor (not skirt) to height/3.2
        # skirt occupies v < 0 region; shift so floor is v=0
        u0, u1 = dist / 4.0, (dist + seg) / 4.0
        v_bot = -skirt / 3.2
        v_top = height / 3.2
        uvs += [(u0, v_bot), (u1, v_bot), (u1, v_top), (u0, v_top)]
        colors += [col] * 4
        faces += [(v0, v0 + 1, v0 + 2), (v0, v0 + 2, v0 + 3)]
        dist += seg
    # roof
    roof_col = (70, 70, 75) if not pitched else (90, 55, 45)
    if pitched and n >= 4:
        # gable along the longer PCA axis
        c = ext.mean(axis=0)
        _, _, vh = np.linalg.svd(ext - c, full_matrices=False)
        axis = vh[0]
        proj = (ext - c) @ axis
        ridge_h = z_top + max(1.2, min(3.5, height * 0.28))
        # two roof planes via triangulation from ridge line ends
        # build roof as triangle fan from a ridge of two points
        pmin, pmax = c + axis * proj.min(), c + axis * proj.max()
        r1 = (pmin[0] - C.ORIGIN_X, ridge_h, pmin[1] - C.ORIGIN_Y)
        r2 = (pmax[0] - C.ORIGIN_X, ridge_h, pmax[1] - C.ORIGIN_Y)
        # project each edge midpoint to decide sides — simpler: triangulate eaves to nearest ridge point
        eave = []
        for p in ext:
            eave.append((p[0] - C.ORIGIN_X, z_top, p[1] - C.ORIGIN_Y))
        base = len(verts)
        verts += eave + [r1, r2]
        uvs += [(p[0] / 8.0, p[1] / 8.0) for p in ext] + [(0, 0), (1, 1)]
        colors += [roof_col] * (n + 2)
        ir1, ir2 = base + n, base + n + 1
        for i in range(n):
            a, b = base + i, base + ((i + 1) % n)
            # pick closer ridge end for a mid-edge test
            mid = (np.array(verts[a]) + np.array(verts[b])) * 0.5
            d1 = np.linalg.norm(mid[[0, 2]] - np.array([r1[0], r1[2]]))
            d2 = np.linalg.norm(mid[[0, 2]] - np.array([r2[0], r2[2]]))
            # two tris to both ridge points would over-span; use a single tri to the farther? 
            # Better: each eave edge forms a tri with the ridge segment (quad split)
            faces += [(a, b, ir1 if d1 < d2 else ir2)]
        # close gable ends
        faces += [(ir1, ir2, base + int(np.argmin(proj))), (ir1, ir2, base + int(np.argmax(proj)))]
    else:
        # flat roof: fan
        c = ext.mean(axis=0)
        cx, cz = c[0] - C.ORIGIN_X, c[1] - C.ORIGIN_Y
        base = len(verts)
        verts.append((cx, z_top + 0.08, cz))
        uvs.append((0, 0))
        colors.append(roof_col)
        for p in ext:
            verts.append((p[0] - C.ORIGIN_X, z_top + 0.08, p[1] - C.ORIGIN_Y))
            uvs.append((p[0] / 8.0, p[1] / 8.0))
            colors.append(roof_col)
        for i in range(n):
            faces.append((base, base + 1 + i, base + 1 + ((i + 1) % n)))
    return (
        np.asarray(verts, np.float32),
        np.asarray(faces, np.int32),
        np.asarray(uvs, np.float32),
        np.asarray(colors, np.uint8),
    )


def mesh_from_vc(v, f, uv, col, name):
    mesh = trimesh.Trimesh(vertices=v, faces=f, process=False)
    # pack colors
    if col.ndim == 2 and col.shape[1] == 3:
        rgba = np.concatenate([col, np.full((len(col), 1), 255, np.uint8)], axis=1)
    else:
        rgba = col
    mesh.visual.vertex_colors = rgba
    if uv is not None:
        try:
            mesh.visual = trimesh.visual.TextureVisuals(uv=uv)
            mesh.visual.vertex_colors = rgba
        except Exception:
            pass
    mesh.metadata["name"] = name
    return mesh


def chunk_key(x, y):
    return int(math.floor(x / CHUNK)), int(math.floor(y / CHUNK))


def main():
    C.ensure_dirs()
    C.load_origin_from_dem()
    dem = DemSampler(C.RAW / "dem" / "ogden_1m_utm12n.tif")
    print(f"origin SW ({C.ORIGIN_X:.1f}, {C.ORIGIN_Y:.1f})")

    highways = load_fc("highways")
    railways = load_fc("railways")
    buildings = load_fc("buildings")
    landuse = load_fc("landuse")
    water = load_fc("water")
    waterways = load_fc("waterways")
    trees = load_fc("trees")
    stations = load_fc("stations")
    print(f"features roads={len(highways)} rail={len(railways)} bld={len(buildings)} "
          f"lu={len(landuse)} water={len(water)} trees={len(trees)}")

    # --- intersection points (major roads) ---
    node_ways = defaultdict(set)
    way_widths = []
    for i, f in enumerate(highways):
        tags = f["properties"]
        typ = tags.get("highway")
        if typ not in MAJOR_TYPES:
            continue
        g = shape(f["geometry"])
        if g.geom_type == "MultiLineString":
            lines = list(g.geoms)
        elif g.geom_type == "LineString":
            lines = [g]
        else:
            continue
        w = road_width(tags)
        for line in lines:
            coords = list(line.coords)
            for (x, y, *_) in coords:
                key = (round(x * 1.25) / 1.25, round(y * 1.25) / 1.25)
                node_ways[key].add(i)
        way_widths.append((i, w, typ))
    intersections = []
    for (x, y), ids in node_ways.items():
        if len(ids) >= 2:
            # T or cross: at least one shared vertex of 2+ ways
            # require 3+ for mid-block false positives from split ways? 2 is T-junction if endpoint
            maxw = 7.0
            for i, w, typ in way_widths:
                if i in ids:
                    maxw = max(maxw, w)
            # filter split-way duplicates: if only 2 ways and they appear colinear, skip later visually OK
            intersections.append((x, y, maxw + 1.0))
    print(f"intersection patches (raw 2+ ways): {len(intersections)}")
    # keep a cap of major ones: degree>=3 plus degree 2 of primary/secondary
    deg3 = [p for p in intersections if len(node_ways[(round(p[0]*1.25)/1.25, round(p[1]*1.25)/1.25)]) >= 3]
    print(f"  degree>=3: {len(deg3)}")
    use_ix = deg3 if deg3 else intersections

    # --- group everything by 256 m chunk in UTM ---
    buckets = defaultdict(lambda: defaultdict(list))  # (ix,iy) -> kind -> list of meshes or parts

    def add_part(kind, x, y, part):
        if part is None:
            return
        ix, iy = chunk_key(x, y)
        buckets[(ix, iy)][kind].append(part)

    # roads + sidewalks
    n_roads = 0
    for f in highways:
        tags = f["properties"]
        typ = tags.get("highway")
        if not typ:
            continue
        g = shape(f["geometry"])
        geoms = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
        w = road_width(tags)
        for line in geoms:
            if line.geom_type != "LineString" or line.length < 1.0:
                continue
            xy = resample_line(np.asarray(line.coords)[:, :2], step=5.0)
            z = dem.sample(xy[:, 0], xy[:, 1])
            part = ribbon(xy, z, w, y_off=0.06)
            mid = xy[len(xy)//2]
            add_part("roads", mid[0], mid[1], part)
            n_roads += 1
            if typ in SIDEWALK_TYPES and w >= 5.5:
                # sidewalks just outside the carriageway
                sw = 1.6
                gap = w / 2 + 0.4 + sw / 2
                # offset left/right
                tang = np.gradient(xy, axis=0)
                tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-6)
                nrm = np.column_stack([-tang[:, 1], tang[:, 0]])
                for sign in (-1, 1):
                    side = xy + nrm * (sign * gap)
                    zs = dem.sample(side[:, 0], side[:, 1])
                    add_part("sidewalks", mid[0], mid[1], ribbon(side, zs, sw, y_off=0.10))
    print(f"road segments meshed: {n_roads}")

    # intersection patches
    n_ix = 0
    for x, y, rad in use_ix:
        # regular octagon
        ang = np.linspace(0, 2 * math.pi, 9)
        ring = np.column_stack([x + rad * np.cos(ang), y + rad * np.sin(ang)])
        z = float(dem.sample(x, y)) + 0.09
        gx = ring[:, 0] - C.ORIGIN_X
        gz = ring[:, 1] - C.ORIGIN_Y
        verts = np.column_stack([gx, np.full(len(ring), z), gz]).astype(np.float32)
        # center
        verts = np.vstack([[x - C.ORIGIN_X, z, y - C.ORIGIN_Y], verts[:-1]])
        faces = np.array([[0, i, i + 1] for i in range(1, 8)] + [[0, 8, 1]], np.int32)
        uvs = np.zeros((len(verts), 2), np.float32)
        add_part("roads", x, y, (verts.astype(np.float32), faces, uvs))
        n_ix += 1
    print(f"intersection patches meshed: {n_ix}")

    # rail
    n_rail = 0
    for f in railways:
        tags = f["properties"]
        typ = tags.get("railway")
        if typ in ("abandoned", "proposed", "dismantled", "platform"):
            continue
        g = shape(f["geometry"])
        geoms = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
        for line in geoms:
            if line.geom_type != "LineString" or line.length < 2.0:
                continue
            xy = resample_line(np.asarray(line.coords)[:, :2], step=4.0)
            z = dem.sample(xy[:, 0], xy[:, 1])
            mid = xy[len(xy)//2]
            add_part("rail_ballast", mid[0], mid[1], ribbon(xy, z, 3.6, y_off=0.05))
            # two rails, standard gauge 1.435 m
            tang = np.gradient(xy, axis=0)
            tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-6)
            nrm = np.column_stack([-tang[:, 1], tang[:, 0]])
            for sign in (-1, 1):
                rail_xy = xy + nrm * (sign * 0.7175)
                add_part("rail_steel", mid[0], mid[1], ribbon(rail_xy, z, 0.08, y_off=0.20))
            # 3D sleepers along mainline and light yards, every 0.8 m
            if typ in ("rail", "light_rail", "tram", "yard") and line.length < 2500:
                step = 0.8 if typ == "rail" else 1.2
                sxy = resample_line(xy, step=step)
                sz = dem.sample(sxy[:, 0], sxy[:, 1])
                stang = np.gradient(sxy, axis=0)
                stang /= np.maximum(np.linalg.norm(stang, axis=1, keepdims=True), 1e-6)
                sn = np.column_stack([-stang[:, 1], stang[:, 0]])
                for i in range(len(sxy)):
                    a = sxy[i] + sn[i] * 1.2
                    b = sxy[i] - sn[i] * 1.2
                    seg = np.vstack([a, b])
                    zz = np.array([sz[i], sz[i]])
                    add_part("rail_sleeper", sxy[i, 0], sxy[i, 1], ribbon(seg, zz, 0.22, y_off=0.12))
            n_rail += 1
    print(f"rail segments meshed: {n_rail}")

    # buildings
    n_b = 0
    src_counts = Counter()
    collision = []  # 2d footprints + height for the prototype
    for f in buildings:
        tags = f["properties"]
        g = shape(f["geometry"])
        if g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            continue
        area = g.area
        if area < 8:
            continue
        typ = (tags.get("building") or "yes").lower()
        if typ == "roof":  # canopy, skip or very low
            pass
        h, src = building_height(tags, area)
        src_counts[src] += 1
        fclass = facade_class(tags)
        if typ == "yes" and area > 600:
            fclass = "commercial"
        pitched = want_pitched(tags, fclass)
        zmin = dem.min_in_poly(g)
        oid = tags.get("osm_id") or n_b
        meshdata = extrude_building(g, zmin, h, pitched, fclass, oid)
        if meshdata is None:
            continue
        c = g.representative_point()
        add_part("buildings_" + fclass, c.x, c.y, meshdata)
        n_b += 1
        minx, miny, maxx, maxy = g.bounds
        collision.append({
            "minx": minx - C.ORIGIN_X, "minz": miny - C.ORIGIN_Y,
            "maxx": maxx - C.ORIGIN_X, "maxz": maxy - C.ORIGIN_Y,
            "ymin": zmin, "ymax": zmin + h,
            "class": fclass,
        })
    print(f"buildings meshed: {n_b}  height sources: {dict(src_counts)}")

    # generic station volume near tracks if OSM has a station feature
    for f in stations:
        g = shape(f["geometry"])
        c = g.representative_point() if g.geom_type != "Point" else g
        # if already a building, skip extra; points get a generic hall
        if g.geom_type == "Point":
            hall = box(c.x - 18, c.y - 10, c.x + 18, c.y + 10)
            zmin = float(dem.sample(c.x, c.y))
            md = extrude_building(hall, zmin, 12.0, False, "civic", 0)
            add_part("buildings_civic", c.x, c.y, md)

    # parks / grass / forest / etc.
    n_lu = 0
    PARK_TAGS = {
        "grass", "meadow", "village_green", "recreation_ground", "forest", "orchard",
    }
    LEISURE = {"park", "pitch", "garden", "playground", "golf_course", "recreation_ground"}
    NATURAL_GREEN = {"wood", "forest", "grassland", "grass", "meadow", "scrub", "heath"}
    for f in landuse:
        tags = f["properties"]
        keep = (
            tags.get("landuse") in PARK_TAGS
            or tags.get("leisure") in LEISURE
            or tags.get("natural") in NATURAL_GREEN
        )
        if not keep:
            continue
        g = shape(f["geometry"])
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty or g.area < 20:
            continue
        part = triangulate_poly2(g, dem, y_off=0.04)
        if part is None:
            continue
        c = g.representative_point()
        kind = "parks"
        if tags.get("natural") in ("wood", "forest") or tags.get("landuse") == "forest":
            kind = "forest"
        add_part(kind, c.x, c.y, part if not isinstance(part, trimesh.Trimesh) else None)
        # triangulate_poly2 may return a merged trimesh if MultiPolygon — handle
        if isinstance(part, trimesh.Trimesh):
            add_part(kind, c.x, c.y, (part.vertices, part.faces, np.zeros((len(part.vertices), 2), np.float32)))
        n_lu += 1
    print(f"park/forest polys meshed: {n_lu}")

    # water polygons + wide river banks from waterways
    n_w = 0
    for f in water:
        g = shape(f["geometry"])
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            continue
        part = triangulate_poly2(g, dem, y_off=0.02)
        if part is None or isinstance(part, trimesh.Trimesh):
            if isinstance(part, trimesh.Trimesh):
                c = g.representative_point()
                add_part("water", c.x, c.y, (part.vertices, part.faces, np.zeros((len(part.vertices), 2), np.float32)))
                n_w += 1
            continue
        c = g.representative_point()
        add_part("water", c.x, c.y, part)
        n_w += 1
    for f in waterways:
        tags = f["properties"]
        typ = tags.get("waterway")
        if typ not in ("river", "canal", "stream", "ditch"):
            continue
        g = shape(f["geometry"])
        geoms = list(g.geoms) if g.geom_type == "MultiLineString" else [g]
        width = 12.0 if typ == "river" else (6.0 if typ == "canal" else 2.5)
        for line in geoms:
            if line.geom_type != "LineString" or line.length < 3:
                continue
            xy = resample_line(np.asarray(line.coords)[:, :2], step=6.0)
            z = dem.sample(xy[:, 0], xy[:, 1])
            mid = xy[len(xy)//2]
            add_part("water", mid[0], mid[1], ribbon(xy, z, width, y_off=0.01))
            n_w += 1
    print(f"water features meshed: {n_w}")

    # trees as simple points file (meshes instanced in engine / prototype)
    tree_pts = []
    for f in trees:
        g = shape(f["geometry"])
        x, y = g.x, g.y
        z = float(dem.sample(x, y))
        gx, gz = x - C.ORIGIN_X, y - C.ORIGIN_Y
        tree_pts.append({"x": gx, "y": z, "z": gz})
    # scatter extra trees in forest/park polygons (capped)
    rng = np.random.default_rng(42)
    extra = 0
    for f in landuse:
        tags = f["properties"]
        if not (tags.get("natural") in ("wood", "forest") or tags.get("landuse") in ("forest", "orchard")
                or tags.get("leisure") == "park"):
            continue
        g = shape(f["geometry"])
        if g.area < 80:
            continue
        n = int(min(80, max(2, g.area / 400.0)))
        minx, miny, maxx, maxy = g.bounds
        tries = 0
        placed = 0
        while placed < n and tries < n * 8:
            tries += 1
            px = float(rng.uniform(minx, maxx))
            py = float(rng.uniform(miny, maxy))
            if not g.contains(Point(px, py)):
                continue
            z = float(dem.sample(px, py))
            tree_pts.append({"x": px - C.ORIGIN_X, "y": z, "z": py - C.ORIGIN_Y})
            placed += 1
            extra += 1
    print(f"trees: osm={len(trees)} + scattered={extra}")

    # --- write GLBs per chunk / kind ---
    out_root = C.PROCESSED / "chunks"
    out_root.mkdir(parents=True, exist_ok=True)
    COLOR = {
        "roads": (45, 45, 48),
        "sidewalks": (150, 148, 142),
        "rail_ballast": (90, 82, 70),
        "rail_steel": (180, 185, 190),
        "rail_sleeper": (110, 80, 50),
        "parks": (70, 110, 55),
        "forest": (50, 90, 45),
        "water": (40, 90, 140),
        "buildings_residential": (160, 90, 70),
        "buildings_commercial": (180, 185, 190),
        "buildings_industrial": (130, 135, 140),
        "buildings_civic": (170, 165, 155),
    }
    manifest_chunks = []
    kind_counts = Counter()
    bytes_total = 0
    for (ix, iy), kinds in sorted(buckets.items()):
        entry = {
            "id": f"c_{ix}_{iy}",
            "ix": ix, "iy": iy,
            "bounds_utm": {
                "west": ix * CHUNK, "south": iy * CHUNK,
                "east": (ix + 1) * CHUNK, "north": (iy + 1) * CHUNK,
            },
            "bounds_game": {
                "minx": ix * CHUNK - C.ORIGIN_X, "minz": iy * CHUNK - C.ORIGIN_Y,
                "maxx": (ix + 1) * CHUNK - C.ORIGIN_X, "maxz": (iy + 1) * CHUNK - C.ORIGIN_Y,
            },
            "files": {},
        }
        # wait: chunk_key uses UTM coords, but bounds_game should subtract origin.
        # UTM chunk origin is ix*CHUNK, but UTM values are ~415000, so ix is large.
        # That's correct as long as we use the same key consistently.
        cx0, cy0 = ix * CHUNK, iy * CHUNK
        entry["bounds_utm"] = {"west": cx0, "south": cy0, "east": cx0 + CHUNK, "north": cy0 + CHUNK}
        entry["bounds_game"] = {
            "minx": cx0 - C.ORIGIN_X, "minz": cy0 - C.ORIGIN_Y,
            "maxx": cx0 + CHUNK - C.ORIGIN_X, "maxz": cy0 + CHUNK - C.ORIGIN_Y,
        }
        for kind, parts in kinds.items():
            # parts may be 3-tuples or 4-tuples (buildings)
            vslist, fslist, uvslist, colslist = [], [], [], []
            off = 0
            for p in parts:
                if p is None:
                    continue
                if len(p) == 4:
                    v, f, uv, col = p
                else:
                    v, f, uv = p
                    col = None
                if f is None or len(f) == 0:
                    continue
                vslist.append(v)
                fslist.append(f + off)
                uvslist.append(uv if uv is not None else np.zeros((len(v), 2), np.float32))
                if col is not None:
                    if col.ndim == 2 and col.shape[1] == 3:
                        col = np.concatenate([col, np.full((len(col), 1), 255, np.uint8)], axis=1)
                    colslist.append(col)
                else:
                    rgba = np.array(list(COLOR.get(kind, (128, 128, 128))) + [255], np.uint8)
                    colslist.append(np.tile(rgba, (len(v), 1)))
                off += len(v)
            if not vslist:
                continue
            v = np.vstack(vslist)
            fcs = np.vstack(fslist)
            uv = np.vstack(uvslist)
            col = np.vstack(colslist)
            # skip degenerate
            if len(v) < 3 or len(fcs) < 1:
                continue
            mesh = trimesh.Trimesh(vertices=v, faces=fcs, process=False, vertex_colors=col)
            fname = f"c_{ix}_{iy}_{kind}.glb"
            path = out_root / fname
            try:
                mesh.export(path)
            except Exception as e:
                print(f"  export fail {fname}: {e}")
                continue
            entry["files"][kind] = {"file": fname, "bytes": path.stat().st_size,
                                    "vertices": int(len(v)), "faces": int(len(fcs))}
            kind_counts[kind] += 1
            bytes_total += path.stat().st_size
        if entry["files"]:
            manifest_chunks.append(entry)

    print(f"chunks with files: {len(manifest_chunks)}  bytes {bytes_total/1e6:.1f} MB  kinds {dict(kind_counts)}")

    # start pose: 25th & Washington
    start_ll = (-111.9705, 41.2206)
    sx, sy = C.ll_to_utm(*start_ll)
    sz = float(dem.sample(sx, sy))
    start = {
        "name": "25th Street & Washington Boulevard",
        "lon": start_ll[0], "lat": start_ll[1],
        "utm": {"easting": sx, "northing": sy},
        "game": {"x": sx - C.ORIGIN_X, "y": sz, "z": sy - C.ORIGIN_Y},
        "look_east": True,
    }

    manifest = {
        "crs": "EPSG:32612",
        "origin_utm12n_sw": {"easting": C.ORIGIN_X, "northing": C.ORIGIN_Y},
        "game_axes": "X=east, Y=up (elevation meters NAVD88/ellipsoid as in 3DEP), Z=north",
        "chunk_size_m": CHUNK,
        "lod_distances_m": {"lod0": 350, "lod1": 700, "lod2": 1400, "unload": 1800},
        "user_bbox_wgs84": C.USER_BBOX,
        "terrain_bbox_wgs84": C.TERRAIN_BBOX,
        "start": start,
        "feature_chunks": manifest_chunks,
        "stats": {
            "buildings_meshed": n_b,
            "building_height_sources": dict(src_counts),
            "road_segments": n_roads,
            "intersection_patches": n_ix,
            "rail_segments": n_rail,
            "trees": len(tree_pts),
            "glb_bytes": bytes_total,
        },
    }
    C.write_json(C.PROCESSED / "manifest_features.json", manifest)
    C.write_json(C.PROCESSED / "trees.json", {"points": tree_pts})
    # collision packed as a compact json (bbox only)
    C.write_json(C.PROCESSED / "collision_buildings.json", {"boxes": collision})
    print("build_features complete")


if __name__ == "__main__":
    main()
