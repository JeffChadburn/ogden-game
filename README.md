# Ogden Environment (prototype)

A walkable 3D replica of a section of **Ogden, Utah**, built as the setting for a new game (separate from *Flesh of the Ancients*). Plain HTML / JavaScript and Three.js in the browser; a Python pipeline prepares engine-neutral data (GLB chunks, 16-bit heightmaps, JSON manifests) for later Unity or Unreal use.

**Play (stable link once Pages is on):** [https://jeffchadburn.github.io/ogden-game/](https://jeffchadburn.github.io/ogden-game/)

This is a first-pass environment only. The game’s own story and plot are not implemented here; see `docs/NARRATIVE.md` for how the real layout can support them.

## The area

- **Feature / street box (unchanged from the brief):** S 41.205, N 41.245, W −112.000, E −111.900 — from south of 28th Street to north of 17th Street, and from the Union Pacific tracks west of Wall Avenue to the Wasatch foothill bench.
- **Verified against OSM:** 17th Street ~41.237 N, 28th Street ~41.214 N, Wall Avenue ~111.979 W, Washington Boulevard ~111.970 W. The user box already covers them; it was not moved.
- **Terrain-only margin:** S 41.193, N 41.247, W −112.003, E −111.855. Mount Ogden (41.1999 N, 111.8822 W, 2,917 m) sits just southeast of the street box; the margin keeps the Wasatch silhouette complete from street level (~1,300 m valley floor to ~2,918 m ridge, true 1× scale).

## Legal / data

Only OpenStreetMap (ODbL, via the Geofabrik Utah extract) and USGS 3DEP 1-meter elevation (public domain) were used. **No Google Maps, Earth, Street View, or other proprietary imagery.** Facades and ground materials are procedural. Attribution:

> © OpenStreetMap contributors, ODbL  
> Elevation: USGS 3D Elevation Program

Full URLs, product names, and download timestamps: `data/SOURCES.md` and `CREDITS.md`.

## Controls

- **Desktop:** click to look, WASD to move (Shift = slower), V first/third person, E vertical exaggeration (1× / 1.5× / 2×; default is accurate scale), C credits, Esc releases the mouse.
- **Phone:** left stick to walk (push farther to go faster), drag on the right to look, on-screen buttons for view / credits / exaggeration.

Default start: 25th Street and Washington Boulevard, facing east toward the mountains.

## Pipeline

All tools live in `tools/` and write under `data/` and `export/`.

1. `tools/fetch_data.py` — cache USGS 3DEP 1 m tiles (windowed `/vsicurl/` mosaic to UTM 12N). OSM was intended via one Overpass query; Overpass failed on this run, so the pipeline used the allowed Geofabrik Utah extract (`osmium extract` + GDAL).
2. `tools/split_osm.py` — per-layer GeoJSON (WGS84 + UTM 12N) for buildings, highways, railways, landuse, water, trees, peaks.
3. `tools/generate_textures.py` — procedural tileable materials (grass, rock, dirt, pavement, brick, etc.).
4. `tools/build_terrain.py` — 16-bit heightmaps (4097×2049 Unity-oriented + a 2049×1025 web/collision map, PNG + RAW + sidecar JSON), hillshade, RGBA splat and landuse preview, terrain GLB chunks with LODs (8 m in the city, coarser on the mountains).
5. `tools/build_features.py` — roads as draped ribbons with width-by-highway-type and sidewalks on urban streets; intersection patches at major junctions; UP rails as ballast, sleepers, and steel; extruded buildings (height tag → levels × 3.2 m → type/area defaults; flat or pitched roofs; procedural facade classes); parks and water from OSM; tree points.
6. Export pack: `export/` and `ogden-environment.zip`. Import notes: `docs/IMPORT.md`. Starter Unity streamer: `unity/ChunkStreamer.cs` (not editor-tested). Unreal Engine 5 pack: `export_unreal/` and `ogden-unreal.zip`; beginner setup and exact Landscape numbers: [`docs/UNREAL_SETUP.md`](docs/UNREAL_SETUP.md) (import script untested in the editor).

Coordinates: EPSG:32612, origin at the DEM southwest corner, X east, Y up, Z north, meters.

## Changelog

### Round 1 — 2026-11-11

- First walkable prototype of the Ogden street box and Wasatch backdrop.
- 1 m 3DEP terrain, OSM roads / rail / buildings / parks / water, procedural materials, chunk streaming, phone and desktop controls, credits screen, engine export folder and zip.
- GitHub Pages at `jeffchadburn.github.io/ogden-game`.

### Unreal Engine 5 pack — 2026-11-11

- First-pass UE5 pack in `export_unreal/` (zipped as `ogden-unreal.zip`): 4033×4033 16-bit landscape and weight layers, glTF meshes in centimeters / Z-up, tree instances, and `tools/unreal_import.py`. Beginner steps and exact import numbers: `docs/UNREAL_SETUP.md`. The import script has not been run inside the Unreal Editor.


### Visual pass — 2026-11-11

- Roads near the camera no longer render black. The feature GLBs had no UVs or normals (dropped on export), so lit materials and textures were sampling as black; both are now generated at load time. Asphalt was lifted so sRGB + ACES still reads as pavement, sidewalks use a concrete slab texture, and polygon offset reduces z-fighting.
- Landuse/debug ground colors were replaced with muted, tiled procedural materials (dry grass, dirt, pavement, scrub, rock) blended from the existing RGBA splat.
- Foothill streaks from stretching a top-down map on steep slopes are reduced with triplanar rock/scrub and a slope-based blend to rock on steep faces.
- Cheap procedural window bays on building facades (runtime wall UVs in 4 m bays / floors). No copied facades or business names.
- Minimap uses a muted splat coloring instead of the GIS legend.

### Known issues and deferred work (first pass)

- OSM building **heights are sparse** (~0.06% with a height tag, ~1.2% with `building:levels` in the user box). Most heights are type/area defaults, so the skyline is plausible, not survey-accurate.
- Buildings are **generic massing** (procedural facades, simple gables). No photoreal copies, no interiors, no unique landmark architecture.
- Intersection patches cover **degree ≥ 3** junctions only, not every alley T-junction.
- Sidewalks follow classified urban streets, not a complete pedestrian network.
- On the **east bench**, many road/building/tree meshes sit on a flat ~1,769.6 m shelf instead of the 3DEP surface (a height-sample gap from the first build). They can appear to float. Fixing it needs a features rebuild, which was out of scope for this visual pass.
- Terrain **visual** mesh in the export is 8 m (city) / 16 m (mountains); collision and the web heightfield are finer (~3 m and ~6 m). A 1–2 m city mesh everywhere is deferred.
- 3D sleepers are thinned in yards; long yard leads may look simpler than the mainline.
- Unity / Unreal import scripts are documented starters, **not** play-tested in those editors.
- No traffic, interiors, weather, or day/night cycle yet (simple daylight + haze only).
- Some passage-crossing / thin lots may still have footprints slightly off the curb; buildings are seated with a 3 m skirt on min ground under the footprint.
