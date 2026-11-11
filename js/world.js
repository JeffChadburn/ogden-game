import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

export class World {
  constructor(scene, manifest) {
    this.scene = scene;
    this.manifest = manifest;
    this.origin = manifest.origin_utm12n_sw;
    this.loader = new GLTFLoader();
    this.loaded = new Map(); // chunk id -> { group, kinds }
    this.terrainTiles = new Map();
    this.height = null;
    this.exag = 1;
    this.buildingBoxes = [];
    this.maxLoad = 7; // chunks radius in 256 m units ~ 900 m
    this.maps = {};
  }

  async init() {
    const [hm, splat, boxes, trees, grass, rock, dirt, scrub, pave, asphalt, sidewalk, ballast, wood, water] =
      await Promise.all([
        this._loadHeightfield(),
        this._loadTexture("data/processed/terrain/splat_rgba.png"),
        fetch("data/processed/collision_buildings.json").then((r) => r.json()).catch(() => ({ boxes: [] })),
        fetch("data/processed/trees.json").then((r) => r.json()).catch(() => ({ points: [] })),
        this._loadTiled("assets/textures/grass.png"),
        this._loadTiled("assets/textures/rock.png"),
        this._loadTiled("assets/textures/dirt.png"),
        this._loadTiled("assets/textures/scrub.png"),
        this._loadTiled("assets/textures/pavement.png"),
        this._loadTiled("assets/textures/asphalt.png"),
        this._loadTiled("assets/textures/sidewalk.png"),
        this._loadTiled("assets/textures/ballast.png"),
        this._loadTiled("assets/textures/wood.png"),
        this._loadTiled("assets/textures/water.png"),
      ]);
    this.height = hm;
    this.splatTex = splat;
    // Splat channels are blend weights, not color — keep them linear.
    this.splatTex.colorSpace = THREE.NoColorSpace;
    this.splatTex.wrapS = this.splatTex.wrapT = THREE.ClampToEdgeWrapping;
    this.splatTex.needsUpdate = true;
    this.maps = { grass, rock, dirt, scrub, pave, asphalt, sidewalk, ballast, wood, water };
    this.buildingBoxes = boxes.boxes || [];
    this.treePts = trees.points || [];
    this._buildTerrainTiles();
    this._scatterTrees();
  }

  async preloadAround(px, pz, radius = 500) {
    const tasks = [];
    for (const ch of this.manifest.feature_chunks || []) {
      const b = ch.bounds_game;
      const d = Math.hypot((b.minx + b.maxx) / 2 - px, (b.minz + b.maxz) / 2 - pz);
      if (d < radius && !this.loaded.has(ch.id) && !ch._loading) {
        tasks.push(this._loadChunk(ch));
      }
    }
    await Promise.all(tasks);
  }

  async _loadHeightfield() {
    const meta = await (await fetch("data/processed/terrain/heightmap_web_2049x1025.json")).json();
    const buf = await (await fetch("data/processed/terrain/heightmap_web_2049x1025.raw")).arrayBuffer();
    const u16 = new Uint16Array(buf);
    // RAW row 0 = south
    const w = meta.width_px, h = meta.height_px;
    if (u16.length !== w * h) console.warn("height raw size mismatch", u16.length, w * h);
    const bounds = meta.bounds_utm12n_padded || meta.bounds_utm12n_content;
    return {
      meta, u16, w, h,
      west: bounds.west, south: bounds.south, east: bounds.east, north: bounds.north,
      min: meta.min_elevation_m, scale: meta.height_scale_m,
      resX: meta.meters_per_pixel_x, resY: meta.meters_per_pixel_y,
    };
  }

  _loadTexture(url) {
    return new Promise((resolve, reject) => {
      new THREE.TextureLoader().load(url, resolve, undefined, reject);
    });
  }

  _loadTiled(url) {
    return new Promise((resolve, reject) => {
      new THREE.TextureLoader().load(url, (t) => {
        t.wrapS = t.wrapT = THREE.RepeatWrapping;
        t.colorSpace = THREE.SRGBColorSpace;
        t.anisotropy = 4;
        t.needsUpdate = true;
        resolve(t);
      }, undefined, reject);
    });
  }

  sampleHeightUTM(easting, northing) {
    const hm = this.height;
    if (!hm) return 1300;
    let u = (easting - hm.west) / (hm.east - hm.west);
    let v = (northing - hm.south) / (hm.north - hm.south); // 0 at south
    u = Math.min(1, Math.max(0, u));
    v = Math.min(1, Math.max(0, v));
    const x = u * (hm.w - 1);
    const y = v * (hm.h - 1);
    const x0 = Math.floor(x), y0 = Math.floor(y);
    const x1 = Math.min(x0 + 1, hm.w - 1), y1 = Math.min(y0 + 1, hm.h - 1);
    const fx = x - x0, fy = y - y0;
    const s = (c, r) => hm.u16[r * hm.w + c] / 65535 * hm.scale + hm.min;
    const z0 = s(x0, y0) * (1 - fx) + s(x1, y0) * fx;
    const z1 = s(x0, y1) * (1 - fx) + s(x1, y1) * fx;
    const elev = z0 * (1 - fy) + z1 * fy;
    return this.height.meta.min_elevation_m + (elev - this.height.meta.min_elevation_m) * this.exag;
  }

  sampleHeightGame(x, z) {
    return this.sampleHeightUTM(x + this.origin.easting, z + this.origin.northing);
  }

  setExaggeration(k) {
    this.exag = k;
    this._rebuildTerrainHeights();
  }

  _tileSpan() { return 1024; }

  _buildTerrainTiles() {
    const hm = this.height;
    const span = this._tileSpan();
    const ox = this.origin.easting, oy = this.origin.northing;
    const x0 = Math.floor(hm.west / span) * span;
    const y0 = Math.floor(hm.south / span) * span;
    const mat = this._terrainMaterial();
    for (let y = y0; y < hm.north; y += span) {
      for (let x = x0; x < hm.east; x += span) {
        const x1 = Math.min(x + span, hm.east);
        const y1 = Math.min(y + span, hm.north);
        const isCity = x1 > 416200 && x < 424600 && y1 > 4562000 && y < 4566500;
        const segs = isCity ? 48 : 20;
        const geo = new THREE.PlaneGeometry(x1 - x, y1 - y, segs, segs);
        // Plane is XY; rotate to XZ
        geo.rotateX(-Math.PI / 2);
        this._applyHeights(geo, x, y, x1, y1);
        // world UVs for splat (full terrain)
        const uv = geo.attributes.uv;
        const pos = geo.attributes.position;
        for (let i = 0; i < pos.count; i++) {
          const gx = pos.getX(i) + (x + x1) / 2 - ox;
          const gz = pos.getZ(i) + (y + y1) / 2 - oy;
          const ee = gx + ox, nn = gz + oy;
          uv.setXY(i,
            (ee - hm.west) / (hm.east - hm.west),
            (nn - hm.south) / (hm.north - hm.south));
        }
        uv.needsUpdate = true;
        const mesh = new THREE.Mesh(geo, mat);
        mesh.position.set((x + x1) / 2 - ox, 0, (y + y1) / 2 - oy);
        mesh.receiveShadow = true;
        mesh.userData.tile = { x, y, x1, y1 };
        this.scene.add(mesh);
        this.terrainTiles.set(`${x}_${y}`, mesh);
      }
    }
  }

  _applyHeights(geo, x, y, x1, y1) {
    const pos = geo.attributes.position;
    const cx = (x + x1) / 2, cy = (y + y1) / 2;
    for (let i = 0; i < pos.count; i++) {
      const ee = pos.getX(i) + cx;
      const nn = pos.getZ(i) + cy;
      pos.setY(i, this.sampleHeightUTM(ee, nn));
    }
    pos.needsUpdate = true;
    geo.computeVertexNormals();
  }

  _rebuildTerrainHeights() {
    for (const mesh of this.terrainTiles.values()) {
      const t = mesh.userData.tile;
      this._applyHeights(mesh.geometry, t.x, t.y, t.x1, t.y1);
    }
  }

  _terrainMaterial() {
    // Splat RGBA = grass, rock, urban/pavement, dirt/scrub. Detail is tiled
    // procedural textures; rock/scrub use triplanar + slope so steep foothills
    // don't stretch a top-down map into streaks.
    const mat = new THREE.MeshStandardMaterial({
      map: this.splatTex,
      color: 0xffffff,
      roughness: 0.94,
      metalness: 0.0,
    });
    const maps = this.maps;
    const splat = this.splatTex;
    mat.onBeforeCompile = (shader) => {
      shader.uniforms.uSplat = { value: splat };
      shader.uniforms.uGrass = { value: maps.grass };
      shader.uniforms.uRock = { value: maps.rock };
      shader.uniforms.uDirt = { value: maps.dirt };
      shader.uniforms.uScrub = { value: maps.scrub };
      shader.uniforms.uPave = { value: maps.pave };
      shader.vertexShader = `
        varying vec3 vWp;
        varying vec3 vWn;
      ` + shader.vertexShader.replace(
        "#include <project_vertex>",
        `#include <project_vertex>
        vWp = (modelMatrix * vec4(transformed, 1.0)).xyz;
        vWn = normalize(mat3(modelMatrix) * objectNormal);`
      );
      shader.fragmentShader = `
        uniform sampler2D uSplat;
        uniform sampler2D uGrass;
        uniform sampler2D uRock;
        uniform sampler2D uDirt;
        uniform sampler2D uScrub;
        uniform sampler2D uPave;
        varying vec3 vWp;
        varying vec3 vWn;
      ` + shader.fragmentShader.replace(
        "#include <map_fragment>",
        `
        #ifdef USE_MAP
        {
          vec3 wp = vWp;
          vec3 wn = normalize(vWn);
          vec3 bn = abs(wn);
          bn = pow(max(bn, vec3(0.0)), vec3(4.0));
          bn /= max(bn.x + bn.y + bn.z, 1e-4);

          float sc = 16.0;
          vec2 uvX = wp.zy / sc;
          vec2 uvY = wp.xz / sc;
          vec2 uvZ = wp.xy / sc;

          vec3 grassXZ = texture2D(uGrass, uvY).rgb;
          vec3 dirtXZ  = texture2D(uDirt,  uvY).rgb;
          vec3 scrubXZ = texture2D(uScrub, uvY).rgb;
          vec3 paveXZ  = texture2D(uPave,  uvY).rgb;
          // Triplanar rock (the layer used on steep Wasatch slopes).
          vec3 rockC =
            texture2D(uRock, uvX).rgb * bn.x +
            texture2D(uRock, uvY).rgb * bn.y +
            texture2D(uRock, uvZ).rgb * bn.z;
          // Cheap biplanar scrub: XZ plus the dominant vertical wall.
          vec3 scrubC = scrubXZ * bn.y +
            texture2D(uScrub, mix(uvX, uvZ, step(bn.x, bn.z))).rgb * (1.0 - bn.y);

          // Dry, muted northern-Utah tints.
          grassXZ *= vec3(1.22, 0.88, 0.58);
          dirtXZ  *= vec3(1.08, 0.96, 0.78);
          paveXZ  *= vec3(0.90, 0.90, 0.88);
          rockC   *= vec3(1.04, 1.00, 0.92);
          scrubC  *= vec3(1.12, 0.95, 0.68);
          scrubXZ *= vec3(1.12, 0.95, 0.68);

          vec4 s = textureLod(uSplat, vMapUv, 2.0);
          float wGrass = s.r;
          float wRock  = s.g;
          float wUrban = s.b;
          float wDirtA = s.a;
          float steep = 1.0 - clamp(wn.y, 0.0, 1.0);
          float elevN = clamp((wp.y - 1400.0) / 900.0, 0.0, 1.0);
          float scrubMix = clamp(elevN * 0.7 + steep * 0.55, 0.0, 1.0);
          vec3 dirtScrub = mix(dirtXZ, mix(scrubXZ, scrubC, smoothstep(0.25, 0.7, steep)), scrubMix);

          // City lots should read as yards, not a solid urban debug color.
          vec3 urbanC = dirtScrub * 0.30 + grassXZ * 0.48 + paveXZ * 0.22;

          float sum = wGrass + wRock + wUrban + wDirtA;
          vec3 albedo = dirtScrub;
          if (sum > 0.04) {
            albedo = (wGrass * grassXZ + wRock * rockC + wUrban * urbanC + wDirtA * dirtScrub) / max(sum, 1e-4);
            albedo = mix(dirtScrub, albedo, clamp(sum, 0.0, 1.0));
          }
          // Slope-based override so steep faces never show stretched XZ grass/dirt.
          float slopeBlend = smoothstep(0.26, 0.70, steep);
          vec3 slopeCol = mix(scrubC, rockC, clamp(elevN * 1.15 + 0.15, 0.0, 1.0));
          albedo = mix(albedo, slopeCol, slopeBlend * 0.9);

          diffuseColor *= vec4(albedo, 1.0);
        }
        #endif
        `
      );
    };
    mat.customProgramCacheKey = () => "ogden-terrain-splat-triplanar-v1";
    return mat;
  }

  _scatterTrees() {
    let pts = this.treePts;
    if (!pts.length) return;
    if (pts.length > 2500) {
      // Keep a spread sample so foothills still read as wooded without 12k instances.
      const step = Math.ceil(pts.length / 2500);
      pts = pts.filter((_, i) => i % step === 0);
    }
    const trunk = new THREE.CylinderGeometry(0.12, 0.18, 2.2, 5);
    const crown = new THREE.ConeGeometry(1.6, 3.4, 6);
    const tMat = new THREE.MeshStandardMaterial({ color: 0x4a3420, roughness: 0.9 });
    const cMat = new THREE.MeshStandardMaterial({ color: 0x3d6b32, roughness: 0.85 });
    const tMesh = new THREE.InstancedMesh(trunk, tMat, pts.length);
    const cMesh = new THREE.InstancedMesh(crown, cMat, pts.length);
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), s = new THREE.Vector3();
    const p = new THREE.Vector3();
    for (let i = 0; i < pts.length; i++) {
      const h = 0.8 + ((i * 17) % 10) / 12;
      p.set(pts[i].x, pts[i].y + 1.1 * h, pts[i].z);
      s.set(h, h, h);
      m.compose(p, q, s);
      tMesh.setMatrixAt(i, m);
      p.y = pts[i].y + 2.6 * h;
      s.set(h, h, h);
      m.compose(p, q, s);
      cMesh.setMatrixAt(i, m);
    }
    this.scene.add(tMesh, cMesh);
  }

  updateStreaming(px, pz) {
    const chunks = this.manifest.feature_chunks || [];
    const loadR = 420, unloadR = 700;
    const want = new Set();
    for (const ch of chunks) {
      const b = ch.bounds_game;
      const cx = (b.minx + b.maxx) / 2, cz = (b.minz + b.maxz) / 2;
      const d = Math.hypot(cx - px, cz - pz);
      if (d < loadR) want.add(ch.id);
    }
    for (const [id, rec] of this.loaded) {
      const ch = rec.def;
      const b = ch.bounds_game;
      const d = Math.hypot((b.minx + b.maxx) / 2 - px, (b.minz + b.maxz) / 2 - pz);
      if (d > unloadR && !want.has(id)) {
        this.scene.remove(rec.group);
        rec.group.traverse((o) => {
          if (o.geometry) o.geometry.dispose();
        });
        this.loaded.delete(id);
      }
    }
    for (const ch of chunks) {
      if (!want.has(ch.id) || this.loaded.has(ch.id) || ch._loading) continue;
      this._loadChunk(ch);
    }
  }

  async _loadChunk(ch) {
    ch._loading = true;
    const group = new THREE.Group();
    group.name = ch.id;
    const files = ch.files || {};
    const tasks = [];
    for (const [kind, info] of Object.entries(files)) {
      tasks.push(this._loadGlb(`data/processed/chunks/${info.file}`, kind, group));
    }
    try {
      await Promise.all(tasks);
      this.scene.add(group);
      this.loaded.set(ch.id, { group, def: ch });
    } catch (e) {
      console.warn("chunk failed", ch.id, e);
    } finally {
      ch._loading = false;
    }
  }

  _loadGlb(url, kind, group) {
    return new Promise((resolve) => {
      this.loader.load(url, (gltf) => {
        const mat = materialFor(kind, this.maps);
        gltf.scene.traverse((o) => {
          if (o.isMesh) {
            // Feature GLBs were exported with vertex colors but no UVs or
            // normals (trimesh TextureVisuals dropped them). Rebuild both so
            // tiled maps and Lambert lighting work without a pipeline rerun.
            if (!o.geometry.attributes.normal) o.geometry.computeVertexNormals();
            if (!o.geometry.attributes.uv) this._ensureUv(o.geometry, kind);
            if (mat) o.material = mat;
            if (kind.startsWith("buildings_")) o.castShadow = true;
            o.receiveShadow = true;
          }
        });
        group.add(gltf.scene);
        resolve();
      }, undefined, (err) => { console.warn(url, err); resolve(); });
    });
  }

  _ensureUv(geo, kind) {
    const pos = geo.attributes.position;
    const nrm = geo.attributes.normal;
    const uv = new Float32Array(pos.count * 2);
    const isBld = kind.startsWith("buildings_");
    const scale = (kind === "roads" || kind === "sidewalks") ? 8 : 16;
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i);
      if (isBld && nrm) {
        const ny = nrm.getY(i);
        if (Math.abs(ny) < 0.55) {
          const nx = nrm.getX(i), nz = nrm.getZ(i);
          // Horizontal tangent perpendicular to the wall normal.
          uv[i * 2] = (x * nz - z * nx) / 4.0;
          uv[i * 2 + 1] = y / 3.2;
        } else {
          uv[i * 2] = x / 8.0;
          uv[i * 2 + 1] = z / 8.0;
        }
      } else {
        uv[i * 2] = x / scale;
        uv[i * 2 + 1] = z / scale;
      }
    }
    geo.setAttribute("uv", new THREE.BufferAttribute(uv, 2));
  }

  collideXZ(x, z, radius, y) {
    // AABB vs circle in XZ. Return pushed x,z
    let px = x, pz = z;
    const r = radius;
    // coarse scan — buildings are many; hash would be better, first-pass loop of nearby
    const boxes = this.buildingBoxes;
    for (let i = 0; i < boxes.length; i++) {
      const b = boxes[i];
      if (y + 0.5 < b.ymin - 1 || y > b.ymax + 0.5) continue;
      const qX = Math.max(b.minx, Math.min(px, b.maxx));
      const qZ = Math.max(b.minz, Math.min(pz, b.maxz));
      const dx = px - qX, dz = pz - qZ;
      const d2 = dx * dx + dz * dz;
      if (d2 < r * r) {
        const d = Math.sqrt(d2) || 0.0001;
        const push = (r - d) / d;
        px += dx * push;
        pz += dz * push;
      }
    }
    return { x: px, z: pz };
  }
}

const MATS = {};
function materialFor(kind, maps) {
  if (MATS[kind]) return MATS[kind];
  maps = maps || {};
  let mat;
  if (kind === "roads") {
    mat = makeRoadMaterial(maps.asphalt);
  } else if (kind === "sidewalks") {
    mat = new THREE.MeshLambertMaterial({
      map: maps.sidewalk,
      color: 0xe8e6e0,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -3,
      polygonOffsetUnits: -3,
    });
  } else if (kind === "rail_ballast") {
    mat = new THREE.MeshStandardMaterial({
      map: maps.ballast, roughness: 0.95, metalness: 0,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
    });
  } else if (kind === "rail_steel") {
    mat = new THREE.MeshStandardMaterial({ color: 0xb0b6bc, roughness: 0.35, metalness: 0.8 });
  } else if (kind === "rail_sleeper") {
    mat = new THREE.MeshStandardMaterial({ map: maps.wood, roughness: 0.9, metalness: 0 });
  } else if (kind === "water") {
    mat = new THREE.MeshStandardMaterial({
      map: maps.water, color: 0x4a7aa8,
      roughness: 0.25, metalness: 0.1, transparent: true, opacity: 0.88,
    });
  } else if (kind === "parks" || kind === "forest") {
    mat = new THREE.MeshLambertMaterial({ map: maps.grass, color: 0xc8c4a0, side: THREE.DoubleSide });
  } else if (kind.startsWith("buildings_")) {
    mat = makeBuildingMaterial(kind);
  }
  MATS[kind] = mat || null;
  return MATS[kind];
}



function makeRoadMaterial(asphaltMap) {
  // Lambert + a lifted asphalt map: MeshStandard + ACES crushed shaded
  // carriageways to black, especially at street level looking east.
  const mat = new THREE.MeshLambertMaterial({
    map: asphaltMap,
    color: 0xe8e8ea,
    side: THREE.DoubleSide,
    polygonOffset: true,
    polygonOffsetFactor: -2,
    polygonOffsetUnits: -2,
  });
  return mat;
}

function makeBuildingMaterial(kind) {
  const industrial = kind.includes("industrial");
  // Lambert keeps shaded facades readable (the GLBs were effectively unlit).
  // Window bays come from a tiling canvas texture; wall UVs are already in
  // 4 m bays / floors. Roofs use world XZ UVs and pick up a faint grid only.
  const mat = new THREE.MeshLambertMaterial({
    map: windowMap(),
    color: 0xffffff,
    vertexColors: true,
    side: THREE.DoubleSide,
  });
  mat.customProgramCacheKey = () => "ogden-facade-windowtex-v1-" + kind;
  return mat;
}

function windowMap() {
  if (windowMap._tex) return windowMap._tex;
  const c = document.createElement("canvas");
  c.width = 64; c.height = 64;
  const g = c.getContext("2d");
  g.fillStyle = "#ffffff";
  g.fillRect(0, 0, 64, 64);
  g.fillStyle = "#3a424a";
  g.fillRect(11, 18, 42, 32);
  g.fillStyle = "#4a545c";
  g.fillRect(13, 20, 18, 13);
  g.fillRect(33, 20, 18, 13);
  g.fillRect(13, 35, 18, 13);
  g.fillRect(33, 35, 18, 13);
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 2;
  tex.needsUpdate = true;
  windowMap._tex = tex;
  return tex;
}
