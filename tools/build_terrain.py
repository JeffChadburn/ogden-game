#!/usr/bin/env python3
"""Build heightmaps, hillshade, splat/landuse masks, and terrain GLB chunks."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal, gdalconst, ogr, osr
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

gdal.UseExceptions()


def open_dem():
    path = C.RAW / "dem" / "ogden_1m_utm12n.tif"
    ds = gdal.Open(str(path))
    if ds is None:
        raise SystemExit(f"missing DEM {path}")
    return ds, path


def read_array(ds, band=1):
    b = ds.GetRasterBand(band)
    arr = b.ReadAsArray().astype(np.float32)
    nd = b.GetNoDataValue()
    if nd is not None:
        arr = np.where(arr == nd, np.nan, arr)
    return arr


def fill_nodata(arr):
    if not np.isnan(arr).any():
        return arr
    # fill with nearest valid via simple forward/back fill along rows then cols
    out = arr.copy()
    for _ in range(2):
        mask = np.isnan(out)
        # row-wise
        for r in range(out.shape[0]):
            row = out[r]
            m = np.isnan(row)
            if m.all() or not m.any():
                continue
            good = np.flatnonzero(~m)
            row[m] = np.interp(np.flatnonzero(m), good, row[good])
        # col-wise remaining
        mask = np.isnan(out)
        if mask.any():
            for c in range(out.shape[1]):
                col = out[:, c]
                m = np.isnan(col)
                if m.all() or not m.any():
                    continue
                good = np.flatnonzero(~m)
                col[m] = np.interp(np.flatnonzero(m), good, col[good])
        # leftover: global mean
        out = np.where(np.isnan(out), np.nanmean(out), out)
    return out


def warp_to_grid(src_path, dest, width, height, bounds, res_x, res_y):
    dest.parent.mkdir(parents=True, exist_ok=True)
    opts = gdal.WarpOptions(
        format="GTiff",
        outputBounds=bounds,
        width=width,
        height=height,
        resampleAlg="bilinear",
        dstNodata=-9999,
        creationOptions=["COMPRESS=LZW", "TILED=YES", "PREDICTOR=2"],
    )
    ds = gdal.Warp(str(dest), str(src_path), options=opts)
    arr = read_array(ds)
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    ds = None
    arr = fill_nodata(arr)
    return arr, gt, proj


def encode_u16(arr, vmin, vmax):
    scale = max(vmax - vmin, 1e-3)
    enc = np.clip((arr - vmin) / scale, 0, 1)
    return (enc * 65535.0 + 0.5).astype(np.uint16)


def save_png_u16(path, u16):
    h, w = u16.shape
    img = Image.new("I;16", (w, h))
    img.frombytes(np.ascontiguousarray(u16).tobytes())
    img.save(path)


def save_raw_u16_le(path, u16, south_row_first=True):
    # Unity default: bottom-to-top (south first if Y increases north in world).
    data = np.flipud(u16) if south_row_first else u16
    data.astype("<u2").tofile(path)


def hillshade(arr, res, azimuth=315.0, altitude=45.0):
    zy, zx = np.gradient(arr, res, res)
    slope = np.pi / 2.0 - np.arctan(np.hypot(zx, zy))
    aspect = np.arctan2(-zx, zy)
    az = math.radians(azimuth)
    alt = math.radians(altitude)
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return np.clip(shaded, 0, 1)


def slope_deg(arr, res):
    zy, zx = np.gradient(arr, res, res)
    return np.degrees(np.arctan(np.hypot(zx, zy)))


def rasterize_landuse(shape, gt, landuse_fc, highways_fc, water_fc, railways_fc):
    """Return a dict of float32 masks in 0..1 at the given grid."""
    h, w = shape
    masks = {k: np.zeros((h, w), np.float32) for k in
             ("grass", "forest", "scrub", "rock", "dirt", "urban", "pavement", "water", "rail")}

    mem = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdalconst.GDT_Byte)
    mem.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32612)
    mem.SetProjection(srs.ExportToWkt())

    def burn(features, predicate, out, value=1):
        # Build a temporary in-memory OGR layer of matching polygons/lines
        drv = ogr.GetDriverByName("Memory")
        ods = drv.CreateDataSource("mem")
        layer = ods.CreateLayer("l", srs, ogr.wkbUnknown)
        n = 0
        for f in features:
            if not predicate(f.get("properties") or {}):
                continue
            geom = ogr.CreateGeometryFromJson(json.dumps(f["geometry"]))
            if geom is None:
                continue
            feat = ogr.Feature(layer.GetLayerDefn())
            feat.SetGeometry(geom)
            layer.CreateFeature(feat)
            n += 1
        if n == 0:
            return
        band = mem.GetRasterBand(1)
        band.Fill(0)
        gdal.RasterizeLayer(mem, [1], layer, burn_values=[255])
        arr = band.ReadAsArray().astype(np.float32) / 255.0
        out[:] = np.maximum(out, arr)

    lu = landuse_fc
    burn(lu, lambda p: p.get("landuse") in ("grass", "meadow", "village_green", "recreation_ground")
         or p.get("leisure") in ("park", "pitch", "garden", "playground", "golf_course", "recreation_ground")
         or p.get("natural") in ("grassland", "grass", "meadow"), masks["grass"])
    burn(lu, lambda p: p.get("landuse") in ("forest", "orchard") or p.get("natural") in ("wood", "forest"),
         masks["forest"])
    burn(lu, lambda p: p.get("landuse") in ("scrub",) or p.get("natural") in ("scrub", "heath", "shrub"),
         masks["scrub"])
    burn(lu, lambda p: p.get("natural") in ("bare_rock", "rock", "scree", "cliff", "stone")
         or p.get("landuse") in ("quarry",), masks["rock"])
    burn(lu, lambda p: p.get("landuse") in ("residential", "allotments", "cemetery"), masks["urban"])
    burn(lu, lambda p: p.get("landuse") in ("commercial", "retail", "industrial", "railway",
                                            "construction", "garages", "institutional", "religious",
                                            "education", "military"), masks["urban"])
    burn(lu, lambda p: p.get("landuse") in ("farmland", "farmyard", "brownfield", "greenfield",
                                            "basin") or p.get("natural") in ("sand", "bare_earth", "mud"),
         masks["dirt"])
    burn(lu, lambda p: p.get("landuse") in ("residential",), masks["dirt"])  # yards mixed later
    burn(water_fc, lambda p: True, masks["water"])
    # parking / paved polygons
    burn(lu, lambda p: p.get("landuse") in ("parking",) or p.get("amenity") == "parking"
         or p.get("leisure") == "parking", masks["pavement"])
    # rail beds slightly wider via rasterized lines? skip, features script draws rails
    burn(railways_fc, lambda p: p.get("railway") in ("rail", "yard", "disused"), masks["rail"])
    return masks


def load_fc(name):
    path = C.PROCESSED / "geojson" / f"{name}_utm.geojson"
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("features") or []


def build_splat(elev, res, masks):
    slope = slope_deg(elev, res)
    h, w = elev.shape
    grass = masks.get("grass", np.zeros((h, w), np.float32)).copy()
    forest = masks.get("forest", np.zeros((h, w), np.float32))
    scrub = masks.get("scrub", np.zeros((h, w), np.float32)).copy()
    rock = masks.get("rock", np.zeros((h, w), np.float32)).copy()
    dirt = masks.get("dirt", np.zeros((h, w), np.float32)).copy()
    urban = masks.get("urban", np.zeros((h, w), np.float32))
    pav = masks.get("pavement", np.zeros((h, w), np.float32))
    water = masks.get("water", np.zeros((h, w), np.float32))
    rail = masks.get("rail", np.zeros((h, w), np.float32))

    # Slope / altitude rules (fill where landuse is empty)
    steep = np.clip((slope - 25.0) / 20.0, 0, 1)
    mid_steep = np.clip((slope - 12.0) / 18.0, 0, 1)
    high = np.clip((elev - 1700.0) / 400.0, 0, 1)
    alpine = np.clip((elev - 2100.0) / 300.0, 0, 1)
    covered = np.clip(grass + forest + scrub + rock + dirt + urban + pav + water + rail, 0, 1)

    # default valley = dry grass / dirt; foothills = scrub; high/steep = rock
    default_grass = (1 - covered) * (1 - steep) * (1 - high * 0.7) * 0.65
    default_dirt = (1 - covered) * (1 - steep) * (1 - alpine) * 0.35
    default_scrub = (1 - covered) * np.clip(high * 0.8 + mid_steep * 0.4, 0, 1) * (1 - alpine)
    default_rock = np.maximum(steep, alpine) * (1 - water)

    grass = np.clip(grass + forest * 0.35 + default_grass, 0, 1)
    scrub = np.clip(scrub + default_scrub, 0, 1)
    dirt = np.clip(dirt * 0.5 + default_dirt, 0, 1)
    rock = np.clip(rock + default_rock, 0, 1)
    urban_b = np.clip(urban + pav + rail * 0.5, 0, 1)

    # pack RGBA: R grass (and forest), G rock, B urban/pavement, A dirt/scrub
    R = np.clip(grass + forest * 0.5, 0, 1)
    G = np.clip(rock, 0, 1)
    B = np.clip(urban_b, 0, 1)
    A = np.clip(dirt + scrub, 0, 1)
    # zero out water (rendered as meshes) but keep a slight dirt/grass at banks
    R *= (1 - water * 0.85)
    G *= (1 - water * 0.85)
    B *= (1 - water * 0.9)
    A *= (1 - water * 0.5)

    splat = np.stack([
        (R * 255).astype(np.uint8),
        (G * 255).astype(np.uint8),
        (B * 255).astype(np.uint8),
        (A * 255).astype(np.uint8),
    ], axis=-1)

    # preview mask (opaque RGB legend)
    preview = np.zeros((h, w, 3), dtype=np.uint8)
    preview[..., 0] = np.clip(B * 200 + A * 140 + G * 80, 0, 255).astype(np.uint8)
    preview[..., 1] = np.clip(R * 180 + A * 90 + forest * 80, 0, 255).astype(np.uint8)
    preview[..., 2] = np.clip(G * 200 + water * 200 + B * 40, 0, 255).astype(np.uint8)
    # paint water distinctly
    wmask = water > 0.3
    preview[wmask] = (40, 90, 160)
    return splat, preview


def write_heightmap_bundle(arr, gt, name, out_dir, pad_to=None):
    """Write 16-bit PNG + RAW + sidecar JSON. arr is north-row-first (GDAL)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    vmin = float(np.nanmin(arr))
    vmax = float(np.nanmax(arr))
    h, w = arr.shape
    content_h, content_w = h, w
    pad = {"top": 0, "bottom": 0, "left": 0, "right": 0}
    work = arr
    if pad_to is not None:
        tw, th = pad_to
        if tw < w or th < h:
            raise ValueError(f"pad_to {pad_to} smaller than array {w}x{h}")
        pad_x = tw - w
        pad_y = th - h
        pad["left"] = pad_x // 2
        pad["right"] = pad_x - pad["left"]
        pad["top"] = pad_y // 2
        pad["bottom"] = pad_y - pad["top"]
        work = np.pad(arr, ((pad["top"], pad["bottom"]), (pad["left"], pad["right"])),
                      mode="edge")
        h, w = work.shape
    u16 = encode_u16(work, vmin, vmax)
    png_path = out_dir / f"{name}.png"
    raw_path = out_dir / f"{name}.raw"
    json_path = out_dir / f"{name}.json"
    save_png_u16(png_path, u16)
    save_raw_u16_le(raw_path, u16, south_row_first=True)
    res_x = float(gt[1])
    res_y = float(abs(gt[5]))
    west = float(gt[0])
    north = float(gt[3])
    # content (unpadded) extent
    east = west + content_w * res_x
    south = north - content_h * res_y
    # padded geographic extent (edge-extended)
    west_p = west - pad["left"] * res_x
    north_p = north + pad["top"] * res_y
    east_p = west_p + w * res_x
    south_p = north_p - h * res_y
    C.load_origin_from_dem()
    meta = {
        "name": name,
        "format": "16-bit unsigned, PNG row 0 = north; RAW little-endian, row 0 = south (Unity)",
        "width_px": w,
        "height_px": h,
        "content_width_px": content_w,
        "content_height_px": content_h,
        "pad_pixels": pad,
        "crs": "EPSG:32612",
        "origin_utm12n_sw": {"easting": C.ORIGIN_X, "northing": C.ORIGIN_Y},
        "bounds_utm12n_content": {"west": west, "south": south, "east": east, "north": north},
        "bounds_utm12n_padded": {"west": west_p, "south": south_p, "east": east_p, "north": north_p},
        "meters_per_pixel_x": res_x,
        "meters_per_pixel_y": res_y,
        "min_elevation_m": vmin,
        "max_elevation_m": vmax,
        "height_scale_m": vmax - vmin,
        "vertical_exaggeration": 1.0,
        "unity": {
            "terrain_width_m": w * res_x,
            "terrain_length_m": h * res_y,
            "terrain_height_m": vmax - vmin,
            "heightmap_resolution": w if w == h else f"{w}x{h} (non-square; see IMPORT.md)",
            "raw_bit_depth": 16,
            "raw_byte_order": "little-endian",
            "raw_row_0": "south",
            "set_terrain_size": "Terrain.width = width_m, Terrain.length = length_m, Terrain.height = height_scale_m",
        },
        "unreal": {
            "landscape_scale_x": res_x / 100.0,  # default UE unit is cm; documented in IMPORT.md
            "landscape_scale_y": res_y / 100.0,
            "landscape_scale_z": (vmax - vmin) / 65535.0 / 100.0,
            "note": "See docs/IMPORT.md for exact scale math; Z-up vs Y-up.",
        },
        "files": {"png": png_path.name, "raw": raw_path.name, "json": json_path.name},
    }
    C.write_json(json_path, meta)
    print(f"  heightmap {name}: {w}x{h} elev {vmin:.1f}–{vmax:.1f} m  png={png_path.stat().st_size} raw={raw_path.stat().st_size}")
    return meta, work, u16


def export_terrain_glbs(elev, gt, out_dir, chunk_m=1024.0):
    """Write LOD terrain chunks. City (user bbox) uses finer spacing."""
    import trimesh

    h, w = elev.shape
    res_x = float(gt[1])
    res_y = float(abs(gt[5]))
    west = float(gt[0])
    north = float(gt[3])
    south = north - h * res_y
    east = west + w * res_x
    C.load_origin_from_dem()

    user_sw = C.ll_to_utm(C.USER_BBOX["west"], C.USER_BBOX["south"])
    user_ne = C.ll_to_utm(C.USER_BBOX["east"], C.USER_BBOX["north"])

    def in_city(ex, ny):
        return (user_sw[0] - 80 <= ex <= user_ne[0] + 80) and (user_sw[1] - 80 <= ny <= user_ne[1] + 80)

    chunks = []
    xs = np.arange(west, east, chunk_m)
    ys = np.arange(south, north, chunk_m)
    out_dir.mkdir(parents=True, exist_ok=True)

    for iy, y0 in enumerate(ys):
        for ix, x0 in enumerate(xs):
            x1 = min(x0 + chunk_m, east)
            y1 = min(y0 + chunk_m, north)
            mid_x = 0.5 * (x0 + x1)
            mid_y = 0.5 * (y0 + y1)
            city = in_city(mid_x, mid_y)
            spacings = (8.0, 16.0, 32.0) if city else (16.0, 32.0, 64.0)
            entry = {
                "id": f"t_{ix}_{iy}",
                "ix": ix, "iy": iy,
                "zone": "city" if city else "mountain",
                "bounds_utm": {"west": x0, "south": y0, "east": x1, "north": y1},
                "bounds_game": {
                    "minx": x0 - C.ORIGIN_X, "minz": y0 - C.ORIGIN_Y,
                    "maxx": x1 - C.ORIGIN_X, "maxz": y1 - C.ORIGIN_Y,
                },
                "lods": [],
            }
            for lod, step in enumerate(spacings):
                mesh = _grid_mesh(elev, gt, x0, y0, x1, y1, step)
                if mesh is None:
                    continue
                fname = f"t_{ix}_{iy}_lod{lod}.glb"
                path = out_dir / fname
                mesh.export(path)
                entry["lods"].append({
                    "lod": lod,
                    "spacing_m": step,
                    "file": fname,
                    "bytes": path.stat().st_size,
                    "vertices": int(len(mesh.vertices)),
                    "faces": int(len(mesh.faces)),
                })
            if entry["lods"]:
                chunks.append(entry)
    return chunks


def _grid_mesh(elev, gt, x0, y0, x1, y1, step):
    import trimesh
    res_x = float(gt[1])
    res_y = float(abs(gt[5]))
    west = float(gt[0])
    north = float(gt[3])
    h, w = elev.shape

    xs = np.arange(x0, x1 + step * 0.5, step)
    ys = np.arange(y0, y1 + step * 0.5, step)
    if len(xs) < 2 or len(ys) < 2:
        return None
    # sample
    cols = np.clip(np.round((xs - west) / res_x).astype(int), 0, w - 1)
    rows = np.clip(np.round((north - ys) / res_y).astype(int), 0, h - 1)
    # rows are from south to north in ys; elev index is north-first
    Z = elev[np.ix_(rows[::-1], cols)]  # wait, ys is south->north, rows computed from north-ys
    # Actually rows[i] already corresponds to ys[i]. So height[j,i] = elev[rows[j], cols[i]]
    # Rebuild properly:
    C.load_origin_from_dem()
    colg, rowg = np.meshgrid(cols, rows)
    heights = elev[rowg, colg]
    xx, yy = np.meshgrid(xs, ys)
    gx = (xx - C.ORIGIN_X).astype(np.float32)
    gz = (yy - C.ORIGIN_Y).astype(np.float32)
    gy = heights.astype(np.float32)
    ny, nx = heights.shape
    verts = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
    # faces
    faces = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            a = j * nx + i
            b = a + 1
            c = a + nx
            d = c + 1
            faces.append((a, c, b))
            faces.append((b, c, d))
    faces = np.asarray(faces, dtype=np.int32)
    # world-space UVs over full terrain for splat sampling
    south = north - h * res_y
    east = west + w * res_x
    u = ((xx - west) / (east - west)).astype(np.float32)
    v = ((yy - south) / (north - south)).astype(np.float32)
    uv = np.column_stack([u.ravel(), v.ravel()])
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv)
    return mesh


def main():
    C.ensure_dirs()
    ds, dem_path = open_dem()
    C.load_origin_from_dem(dem_path)
    src_arr = read_array(ds)
    src_gt = ds.GetGeoTransform()
    src_h, src_w = src_arr.shape
    src_arr = fill_nodata(src_arr)
    vmin, vmax = float(np.nanmin(src_arr)), float(np.nanmax(src_arr))
    print(f"source DEM {src_w}x{src_h} at 1 m, elev {vmin:.2f}–{vmax:.2f} m, "
          f"origin SW ({C.ORIGIN_X:.2f}, {C.ORIGIN_Y:.2f})")

    # Full-extent Unity-friendly 4097 x 2049, uniform m/px from width
    west = src_gt[0]
    north = src_gt[3]
    res_src = src_gt[1]
    east = west + src_w * res_src
    south = north - src_h * abs(src_gt[5])
    width_m = east - west
    height_m = north - south
    target_w, target_h = 4097, 2049
    res = width_m / (target_w - 1)
    # expand N/S so height is 2048 * res (uniform)
    need_h = (target_h - 1) * res
    extra = need_h - height_m
    south_p = south - extra / 2
    north_p = north + extra / 2
    bounds = [west, south_p, east, north_p]
    out_t = C.PROCESSED / "terrain"
    tmp = out_t / "_hmap_4097.tif"
    arr4097, gt4097, _ = warp_to_grid(dem_path, tmp, target_w, target_h, bounds, res, res)
    # warp_to_grid already set width/height so res should match
    meta_full, _, _ = write_heightmap_bundle(arr4097, gt4097, "heightmap_4097x2049", out_t)

    # Web / collision heightmap 2049 x 1025 (roughly half)
    tw, th = 2049, 1025
    res_w = width_m / (tw - 1)
    need_h = (th - 1) * res_w
    extra = need_h - height_m
    bounds_w = [west, south - extra / 2, east, north + extra / 2]
    tmpw = out_t / "_hmap_2049.tif"
    arr2049, gt2049, _ = warp_to_grid(dem_path, tmpw, tw, th, bounds_w, res_w, res_w)
    write_heightmap_bundle(arr2049, gt2049, "heightmap_web_2049x1025", out_t)

    # Hillshade preview (from 4097 grid, saved as 8-bit PNG)
    hs = hillshade(arr4097, float(gt4097[1]))
    hs_img = (hs * 255).astype(np.uint8)
    # tint low = green-gray, high = rock-gray for a readable relief
    alt = (arr4097 - vmin) / max(vmax - vmin, 1)
    rgb = np.stack([
        np.clip(hs_img * (0.7 + 0.3 * alt), 0, 255),
        np.clip(hs_img * (0.75 + 0.15 * (1 - alt)), 0, 255),
        np.clip(hs_img * (0.65 + 0.2 * alt), 0, 255),
    ], axis=-1).astype(np.uint8)
    hs_path = out_t / "hillshade.png"
    Image.fromarray(rgb, mode="RGB").save(hs_path, optimize=True)
    print(f"  hillshade {hs_path} ({hs_path.stat().st_size} bytes)")

    # Splat / landuse at 2049 x ~1025 (same grid as web heightmap for easy alignment)
    print("  rasterizing landuse…", flush=True)
    masks = rasterize_landuse(
        arr2049.shape, gt2049,
        load_fc("landuse"), load_fc("highways"), load_fc("water") + load_fc("waterways"),
        load_fc("railways"),
    )
    splat, preview = build_splat(arr2049, float(gt2049[1]), masks)
    splat_path = out_t / "splat_rgba.png"
    mask_path = out_t / "landuse_mask.png"
    Image.fromarray(splat, mode="RGBA").save(splat_path, optimize=True)
    Image.fromarray(preview, mode="RGB").save(mask_path, optimize=True)
    print(f"  splat {splat_path.stat().st_size}  mask {mask_path.stat().st_size}")

    # Terrain GLB chunks (export quality, first pass spacings)
    chunk_dir = out_t / "chunks"
    print("  building terrain GLB chunks…", flush=True)
    # Use a 4 m downsample of the 1 m DEM for sampling
    step = 4
    small = src_arr[::step, ::step]
    gt_small = (src_gt[0], src_gt[1] * step, 0, src_gt[3], 0, src_gt[5] * step)
    chunks = export_terrain_glbs(small, gt_small, chunk_dir, chunk_m=1024.0)
    print(f"  terrain chunks: {len(chunks)}")
    C.write_json(out_t / "terrain_chunks.json", {
        "chunk_m": 1024,
        "crs": "EPSG:32612",
        "origin_utm12n_sw": {"easting": C.ORIGIN_X, "northing": C.ORIGIN_Y},
        "chunks": chunks,
    })

    # copy key previews to screens later in pipeline
    summary = {
        "source_dem": str(dem_path.relative_to(C.ROOT)),
        "source_resolution_m": 1.0,
        "source_size_px": [src_w, src_h],
        "elevation_min_m": vmin,
        "elevation_max_m": vmax,
        "relief_m": vmax - vmin,
        "origin_utm12n_sw": {"easting": C.ORIGIN_X, "northing": C.ORIGIN_Y},
        "heightmap_full": meta_full,
        "n_terrain_chunks": len(chunks),
        "vertical_exaggeration": 1.0,
    }
    C.write_json(out_t / "terrain_summary.json", summary)
    # cleanup temp tifs to save space? keep them, they're useful. Maybe delete later.
    print("build_terrain complete")


if __name__ == "__main__":
    main()
