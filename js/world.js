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
  }

  async init() {
    const [hm, splat, mask, boxes, trees] = await Promise.all([
      this._loadHeightfield(),
      this._loadTexture("data/processed/terrain/splat_rgba.png"),
      this._loadTexture("data/processed/terrain/terrain_albedo.png"),
      fetch("data/processed/collision_buildings.json").then((r) => r.json()).catch(() => ({ boxes: [] })),
      fetch("data/processed/trees.json").then((r) => r.json()).catch(() => ({ points: [] })),
    ]);
    this.height = hm;
    this.splatTex = splat;
    this.maskTex = mask;
    for (const t of [this.splatTex, this.maskTex]) {
      t.colorSpace = THREE.SRGBColorSpace;
      t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
      t.needsUpdate = true;
    }
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
    // landuse_mask is an RGB preview (grass/rock/urban/dirt). Reliable in all
    // browsers; splat_rgba is also available for engine import.
    const mat = new THREE.MeshStandardMaterial({
      map: this.maskTex || this.splatTex,
      color: 0xffffff,
      roughness: 0.92,
      metalness: 0.02,
    });
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
        const mat = materialFor(kind);
        gltf.scene.traverse((o) => {
          if (o.isMesh) {
            if (kind.startsWith("buildings_")) {
              // Keep exported vertex colors (procedural facade tints).
              const m = o.material;
              if (m) {
                m.vertexColors = true;
                m.side = THREE.DoubleSide;
                m.roughness = 0.82;
                m.metalness = kind.includes("industrial") ? 0.2 : 0.04;
              }
            } else if (mat) {
              o.material = mat;
              o.material.side = THREE.DoubleSide;
            }
            o.castShadow = kind.startsWith("buildings");
            o.receiveShadow = true;
          }
        });
        group.add(gltf.scene);
        resolve();
      }, undefined, (err) => { console.warn(url, err); resolve(); });
    });
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
function materialFor(kind) {
  if (MATS[kind]) return MATS[kind];
  const L = new THREE.TextureLoader();
  const wrap = (t) => { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.colorSpace = THREE.SRGBColorSpace; return t; };
  let mat;
  if (kind === "roads") {
    mat = new THREE.MeshStandardMaterial({ map: wrap(L.load("assets/textures/asphalt.png")), color: 0x5a5a60, roughness: 0.85, metalness: 0 });
  } else if (kind === "sidewalks") {
    mat = new THREE.MeshStandardMaterial({ map: wrap(L.load("assets/textures/sidewalk.png")), roughness: 0.9, metalness: 0 });
  } else if (kind === "rail_ballast") {
    mat = new THREE.MeshStandardMaterial({ map: wrap(L.load("assets/textures/ballast.png")), roughness: 0.95, metalness: 0 });
  } else if (kind === "rail_steel") {
    mat = new THREE.MeshStandardMaterial({ color: 0xb0b6bc, roughness: 0.35, metalness: 0.8 });
  } else if (kind === "rail_sleeper") {
    mat = new THREE.MeshStandardMaterial({ map: wrap(L.load("assets/textures/wood.png")), roughness: 0.9, metalness: 0 });
  } else if (kind === "water") {
    mat = new THREE.MeshStandardMaterial({
      map: wrap(L.load("assets/textures/water.png")), color: 0x4a7aa8,
      roughness: 0.25, metalness: 0.1, transparent: true, opacity: 0.88,
    });
  } else if (kind === "parks" || kind === "forest") {
    mat = new THREE.MeshStandardMaterial({ map: wrap(L.load("assets/textures/grass.png")), roughness: 0.95, metalness: 0 });
  } else if (kind.startsWith("buildings_")) {
    const mapName = {
      buildings_residential: "brick.png",
      buildings_commercial: "concrete.png",
      buildings_industrial: "metal.png",
      buildings_civic: "concrete.png",
    }[kind] || "brick.png";
    const map = wrap(L.load("assets/textures/" + mapName));
    mat = new THREE.MeshStandardMaterial({ map, roughness: 0.8, metalness: kind.includes("industrial") ? 0.25 : 0.05 });
    // window tint via simple emissive? skip — UV grid already on mesh
  }
  MATS[kind] = mat || null;
  return MATS[kind];
}
