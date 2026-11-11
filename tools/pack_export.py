#!/usr/bin/env python3
"""Assemble export/, combined manifest, zip, and a slim web tree."""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C


def mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def main():
    C.ensure_dirs()
    C.load_origin_from_dem()
    export = mkdir(C.EXPORT)
    # clear previous export contents but not the folder
    for child in export.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    terr_src = C.PROCESSED / "terrain"
    feat_chunks = C.PROCESSED / "chunks"
    tdir = mkdir(export / "terrain")
    for name in (
        "heightmap_4097x2049.png", "heightmap_4097x2049.raw", "heightmap_4097x2049.json",
        "heightmap_web_2049x1025.png", "heightmap_web_2049x1025.raw", "heightmap_web_2049x1025.json",
        "hillshade.png", "splat_rgba.png", "landuse_mask.png",
    ):
        s = terr_src / name
        if s.exists():
            copy(s, tdir / name)
    if (terr_src / "chunks").exists():
        copy(terr_src / "chunks", tdir / "chunks")

    # split feature GLBs by kind prefix
    mapping = {
        "roads": "roads", "sidewalks": "roads",
        "rail_ballast": "rail", "rail_steel": "rail", "rail_sleeper": "rail",
        "buildings_residential": "buildings", "buildings_commercial": "buildings",
        "buildings_industrial": "buildings", "buildings_civic": "buildings",
        "parks": "landcover", "forest": "landcover", "water": "landcover",
    }
    for d in ("roads", "rail", "buildings", "landcover"):
        mkdir(export / d)
    if feat_chunks.exists():
        for glb in feat_chunks.glob("*.glb"):
            kind = glb.name.split("_", 3)
            # c_{ix}_{iy}_{kind}.glb  — ix may be negative? usually not
            # name is c_<ix>_<iy>_<kind>.glb, kind can contain underscores
            parts = glb.stem.split("_")
            # parts[0]=c, parts[1]=ix, parts[2]=iy, parts[3:]=kind
            kind = "_".join(parts[3:])
            dest_dir = mapping.get(kind, "landcover")
            copy(glb, export / dest_dir / glb.name)

    for extra in ("trees.json", "collision_buildings.json"):
        s = C.PROCESSED / extra
        if s.exists():
            copy(s, export / "landcover" / extra if extra == "trees.json" else export / "buildings" / extra)

    copy(C.ROOT / "CREDITS.md", export / "CREDITS.md")
    copy(C.ROOT / "data" / "SOURCES.md", export / "SOURCES.md")
    (export / "LICENSE.txt").write_text(
        (C.ROOT / "CREDITS.md").read_text()
        + "\n\n---\nODbL share-alike applies to the derived OSM database (GeoJSON layers and clipped OSM files).\n"
          "The 3D meshes and rendered views are Produced Works and must keep OSM and USGS attribution.\n"
          "USGS 3DEP elevation is public domain.\n"
          "Procedural textures in this pack were generated for this project and may be reused with the same attribution block.\n"
    )
    copy(C.ROOT / "docs" / "IMPORT.md", export / "IMPORT.md")
    copy(C.ROOT / "docs" / "NARRATIVE.md", export / "NARRATIVE.md")
    copy(C.ROOT / "unity" / "ChunkStreamer.cs", export / "unity" / "ChunkStreamer.cs")

    # Combined manifest
    feat = {}
    fp = C.PROCESSED / "manifest_features.json"
    if fp.exists():
        feat = json.loads(fp.read_text())
    terr = {}
    tp = terr_src / "terrain_chunks.json"
    if tp.exists():
        terr = json.loads(tp.read_text())
    manifest = {
        "name": "ogden-environment",
        "crs": "EPSG:32612",
        "origin_utm12n_sw": feat.get("origin_utm12n_sw") or terr.get("origin_utm12n_sw"),
        "game_axes": feat.get("game_axes") or "X=east, Y=up, Z=north, meters",
        "chunk_size_m": feat.get("chunk_size_m", 256),
        "terrain_chunk_size_m": terr.get("chunk_m", 1024),
        "lod_distances_m": feat.get("lod_distances_m") or {"lod0": 350, "lod1": 700, "lod2": 1400, "unload": 1800},
        "user_bbox_wgs84": C.USER_BBOX,
        "terrain_bbox_wgs84": C.TERRAIN_BBOX,
        "start": feat.get("start"),
        "stats": feat.get("stats"),
        "feature_chunks": feat.get("feature_chunks") or [],
        "terrain_chunks": terr.get("chunks") or [],
        "attribution": "© OpenStreetMap contributors, ODbL. Elevation: USGS 3D Elevation Program.",
    }
    C.write_json(export / "manifest.json", manifest)
    C.write_json(C.PROCESSED / "manifest.json", manifest)

    # Zip (may be large; not necessarily pushed to GitHub)
    zpath = C.ROOT / "ogden-environment.zip"
    if zpath.exists():
        zpath.unlink()
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in export.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(export.parent))
    print(f"export packed: {zpath} ({zpath.stat().st_size/1e6:.1f} MB)")
    print(f"manifest chunks: features={len(manifest['feature_chunks'])} terrain={len(manifest['terrain_chunks'])}")


if __name__ == "__main__":
    main()
