# Importing the Ogden environment into Unity, Unreal, or Three.js

All exported assets live in `export/` (or the `ogden-environment.zip` archive). Coordinates are **EPSG:32612** (WGS 84 / UTM zone 12N, meters). Game space: **X = east**, **Y = up** (elevation in meters), **Z = north**. The southwest corner of the 1 m DEM is the origin; see `manifest.json` → `origin_utm12n_sw`.

Vertical scale is **1×** (no exaggeration). Heightmaps encode elevation as unsigned 16-bit values:

`elevation_m = min_elevation_m + (value / 65535) * height_scale_m`

Read `min_elevation_m` and `height_scale_m` from the sidecar JSON next to each heightmap.

## Unity (Terrain from RAW, meshes via glTFast)

These steps were written against Unity 6 / 2022 LTS and [glTFast](https://docs.unity3d.com/Packages/com.unity.cloud.gltfast@6.0/manual/index.html). They have **not** been run in the Unity editor in this pass.

1. Create a new scene. Install **glTFast** (and Addressables if you want the included `ChunkStreamer.cs` to load from Addressable groups).
2. Import `export/terrain/heightmap_4097x2049.raw`. In the sidecar JSON you will find:
   - `width_px` / `height_px`: 4097 × 2049 (non-square)
   - `meters_per_pixel_x` / `meters_per_pixel_y` (uniform by design)
   - `unity.terrain_width_m`, `terrain_length_m`, `terrain_height_m`
   - RAW is **16-bit little-endian, row 0 = south** (Unity’s usual “bottom to top” convention)
3. GameObject → 3D Object → Terrain. In the Terrain import / heightmap tool, set resolution as close as Unity allows. Unity Terrains prefer **square** power-of-two-plus-one grids (513, 1025, 2049, 4097). Because this map is 4097×2049, either:
   - Use a custom mesh (the GLB terrain chunks) instead of Terrain, or
   - Pad/crop to 4097×4097 and adjust `terrain_length_m` accordingly, or
   - Use a third-party non-square terrain importer.
4. Set Terrain width / length / height from the JSON (`terrain_width_m`, `terrain_length_m`, `terrain_height_m`). Place the terrain so its southwest corner is at the world origin (0, 0, 0) *or* offset by the same origin you use for the GLB chunks. Chunk GLBs are already in game space (origin at the DEM southwest).
5. Splat / landuse: `export/terrain/splat_rgba.png` (R = grass/forest, G = rock, B = urban/pavement, A = dirt/scrub). `landuse_mask.png` is a color preview only. Assign procedural or your own layers; do not project Google or Street View photos.
6. Install glTFast. Import `export/terrain/chunks/*.glb`, `export/roads/`, `export/rail/`, `export/buildings/`, `export/landcover/`.
7. Add `unity/ChunkStreamer.cs` to a manager object, point it at `manifest.json` (TextAsset or Addressable), and set LOD distances from the manifest. **This script is a starter; it has not been play-tested inside Unity.**

## Unreal Engine (Landscape from 16-bit PNG/RAW, World Partition)

Written against UE 5.x. **Not tested in the Unreal editor in this pass.**

1. Unreal Landscape heightmaps are typically **16-bit** and, by default, a Landscape world-unit is **1 cm**. The sidecar JSON includes a starting point under `unreal`:
   - `landscape_scale_x` = `meters_per_pixel_x / 100` (cm per pixel along east)
   - `landscape_scale_y` = `meters_per_pixel_y / 100` (along north — check axis swap)
   - `landscape_scale_z` = `height_scale_m / 65535 / 100` (cm per 16-bit step)
2. Import `heightmap_4097x2049.png` (row 0 = **north**, image convention) **or** the RAW (row 0 = **south**). If the terrain looks flipped, invert the Y/X import option. If it is a flat plateau, you likely imported 8-bit or missed the Z scale.
3. Because 4097×2049 is not square, use “non-square landscape” or split into components; or pad to 4097×4097.
4. Import GLB chunks with **Interchange** (glTF). Place them without extra offset if the Landscape southwest corner is at the same origin as `origin_utm12n_sw`.
5. For streaming, enable **World Partition** and load the same 256 m / 1024 m grid described in `manifest.json`. A Blueprint or C++ loader can mirror `ChunkStreamer.cs`: keep actors whose bounds are within `lod_distances_m` of the player, unload beyond `unload`. This project does not ship an Unreal plugin.

## Three.js prototype (already wired)

The browser prototype in this repo is a complete first pass:

- Serves from the project root (`index.html`, `js/`, `data/processed/`, `assets/textures/`).
- Builds terrain tiles at runtime from `heightmap_web_2049x1025.raw` + `splat_rgba.png`.
- Streams feature GLBs listed in `manifest.json` by distance (`js/world.js`).
- Collision: bilinear heightfield + building AABB.
- Optional vertical exaggeration (E key), first/third person (V), credits screen (C).

No build step. Use any static file server.

## Coordinate reminder

To go from UTM to game meters:

```
x = easting  - origin_utm12n_sw.easting
y = elevation_m          # (or elevation * exaggeration)
z = northing - origin_utm12n_sw.northing
```
