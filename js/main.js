import * as THREE from "three";
import { createInput } from "./input.js";
import { World } from "./world.js";

const CREDIT_BLOCK = `Map data © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright).
Elevation: USGS 3D Elevation Program (3DEP) 1-meter DEM (public domain).
Tiles: UT_Northern_QL1_2018 (published 2020-03-30) and UT_StatewideNCentral_2020_A20 (published 2021-11-21).
Data retrieved 2026-11-11. Ground and facade materials are procedural (no photogrammetry, no Google or proprietary imagery).`;

const canvas = document.getElementById("c");
const hud = document.getElementById("hud");
const loadEl = document.getElementById("load");
document.getElementById("creditBlock").textContent = CREDIT_BLOCK;

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight, false);
renderer.shadowMap.enabled = false;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x9eb6cc);
// Atmospheric haze so the ridge stays readable but recedes with distance
scene.fog = new THREE.FogExp2(0xb7c4d4, 0.00022);

const camera = new THREE.PerspectiveCamera(65, innerWidth / innerHeight, 0.4, 28000);
const sun = new THREE.DirectionalLight(0xfff1d6, 1.6);
sun.position.set(-1200, 1800, 400); // west-ish afternoon, mountains catch light from the side
sun.castShadow = false;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.camera.near = 50;
sun.shadow.camera.far = 8000;
sun.shadow.camera.left = sun.shadow.camera.bottom = -1500;
sun.shadow.camera.right = sun.shadow.camera.top = 1500;
scene.add(sun);
scene.add(new THREE.AmbientLight(0x99a8b8, 0.7));
const hemi = new THREE.HemisphereLight(0xd8e8ff, 0x7a6a50, 0.85);
scene.add(hemi);

const input = createInput(canvas);
let world, manifest;
let firstPerson = false;
let exag = 1;
const player = {
  x: 0, y: 1400, z: 0,
  yaw: -Math.PI / 2, // look east (+X)
  pitch: -0.08,
  radius: 0.45,
  eye: 1.65,
  vy: 0,
};

function resize() {
  const w = innerWidth, h = innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);

const params = new URLSearchParams(location.search);
const shot = params.get("shot"); // street | aerial | phone | overview

async function start() {
  manifest = await (await fetch("data/processed/manifest.json")).json();
  world = new World(scene, manifest);
  await world.init();
  const s = manifest.start?.game || { x: 0, y: 1400, z: 0 };
  player.x = s.x; player.y = s.y; player.z = s.z;
  player.yaw = -Math.PI / 2;
  if (shot === "street") firstPerson = true;
  if (shot === "aerial" || shot === "overview") {
    firstPerson = false;
  }
  if (shot === "phone") {
    renderer.setSize(390, 844, false);
    camera.aspect = 390 / 844;
    camera.updateProjectionMatrix();
  }
  await world.preloadAround(player.x, player.z, shot === "aerial" || shot === "overview" ? 1100 : 650);
  loadEl.style.display = "none";
  if (params.get("help") !== "0") document.getElementById("help").style.display = "block";
  world.updateStreaming(player.x, player.z);
  window.__READY = true;
  last = performance.now();
  requestAnimationFrame(loop);
}

let last = performance.now();
function loop(now) {
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  step(dt);
  renderer.render(scene, camera);
  drawMinimap();
  requestAnimationFrame(loop);
}

function step(dt) {
  const look = input.consumeLook();
  const sens = 0.0022;
  player.yaw -= look.dx * sens;
  player.pitch -= look.dy * sens;
  player.pitch = Math.max(-1.2, Math.min(1.2, player.pitch));

  if (shot === "aerial" || shot === "overview") {
    // fixed aerial over the box, looking east-southeast toward the mountains
    const s = manifest.start.game;
    camera.position.set(s.x - 400, s.y + 2200, s.z - 200);
    camera.lookAt(s.x + 3500, s.y + 400, s.z);
    return;
  }

  const mv = input.walkVector();
  const base = 5.6; // m/s run
  const speed = base * (0.35 + 0.65 * (mv.mag || mv.speed));
  const sp = speed * (mv.speed || (mv.x || mv.z ? 1 : 0));
  const cy = Math.cos(player.yaw), sy = Math.sin(player.yaw);
  // yaw 0 looks +Z (north); forward is yaw
  let vx = (sy * mv.z + cy * mv.x) * sp;
  let vz = (cy * mv.z - sy * mv.x) * sp;
  // Walk vector: z negative is forward (W). yaw -90 deg (east): 
  // Standard: forward = (-sin(yaw), -cos(yaw)) if yaw 0 is +Z? 
  // In Three.js, object yaw around Y, rotation.y = yaw looks toward (-sin y, 0, -cos y)? 
  // We'll use: camera looks along (sin(yaw), pitch, cos(yaw)) with yaw 0 = north (+Z).
  // W (z=-1) should move in look direction on XZ.
  vx = (-Math.sin(player.yaw) * -mv.z + Math.cos(player.yaw) * mv.x) * sp;
  vz = (-Math.cos(player.yaw) * -mv.z - Math.sin(player.yaw) * mv.x) * sp;
  // W: mv.z < 0 (from keys z-=1). We want move along look.
  // look xz = (-sin yaw, -cos yaw) if camera uses lookAt from yaw/pitch... let's set camera later consistently.
  // Simpler: W moves in the direction the camera faces on XZ.
  const lookDirX = -Math.sin(player.yaw);
  const lookDirZ = -Math.cos(player.yaw);
  const rightX = Math.cos(player.yaw);
  const rightZ = -Math.sin(player.yaw);
  // input z is negative when pressing W (walkVector uses z-=1 for W). So forward component is -mv.z
  vx = (lookDirX * (-mv.z) + rightX * mv.x) * sp;
  vz = (lookDirZ * (-mv.z) + rightZ * mv.x) * sp;

  let nx = player.x + vx * dt;
  let nz = player.z + vz * dt;
  const ground = world.sampleHeightGame(nx, nz);
  const pushed = world.collideXZ(nx, nz, player.radius, ground + 1.0);
  player.x = pushed.x;
  player.z = pushed.z;
  const g = world.sampleHeightGame(player.x, player.z);
  player.y = g;

  world.updateStreaming(player.x, player.z);

  const eye = new THREE.Vector3(player.x, player.y + player.eye, player.z);
  const fwd = new THREE.Vector3(
    -Math.sin(player.yaw) * Math.cos(player.pitch),
    Math.sin(player.pitch),
    -Math.cos(player.yaw) * Math.cos(player.pitch)
  );
  if (firstPerson || shot === "street") {
    camera.position.copy(eye);
    camera.lookAt(eye.clone().add(fwd));
    if (shot === "street") {
      // lock east toward the mountains, slight upward
      player.yaw = -Math.PI / 2;
      player.pitch = 0.12;
      camera.position.copy(eye);
      camera.lookAt(eye.x + 80, eye.y + 8, eye.z);
    }
  } else {
    const back = fwd.clone().multiplyScalar(-7.5).add(new THREE.Vector3(0, 2.6, 0));
    camera.position.copy(eye).add(back);
    camera.lookAt(eye.clone().add(fwd.multiplyScalar(6)));
  }

  const ll = utmToApproxLL(player.x + manifest.origin_utm12n_sw.easting, player.z + manifest.origin_utm12n_sw.northing);
  hud.innerHTML = `<b>Ogden environment</b><br>${ll}<br>elev ${player.y.toFixed(0)} m · ${firstPerson ? "first" : "third"} person · exag ${exag.toFixed(1)}×`;
}

function utmToApproxLL(e, n) {
  // display only; start known, small offsets
  const o = manifest.origin_utm12n_sw;
  // rough inverse around Ogden
  const lon = -112.003 + (e - o.easting) / 83600;
  const lat = 41.193 + (n - o.northing) / 111200;
  return `${lat.toFixed(5)} N, ${Math.abs(lon).toFixed(5)} W`;
}

function drawMinimap() {
  const c = document.getElementById("minimap");
  const ctx = c.getContext("2d");
  const w = c.width, h = c.height;
  ctx.fillStyle = "#1c2418";
  ctx.fillRect(0, 0, w, h);
  const scale = 0.06; // px per meter
  const cx = player.x, cz = player.z;
  // land tint
  if (!drawMinimap._img) {
    const img = new Image();
    img.src = "data/processed/terrain/landuse_mask.png";
    img.onload = () => { drawMinimap._img = img; };
  }
  if (drawMinimap._img && world?.height) {
    const hm = world.height;
    const ox = manifest.origin_utm12n_sw.easting, oy = manifest.origin_utm12n_sw.northing;
    // map visible area to splat
    // simpler: draw the whole mask in the box
    const west = hm.west - ox, east = hm.east - ox, south = hm.south - oy, north = hm.north - oy;
    const x0 = w / 2 + (west - cx) * scale;
    const y0 = h / 2 - (north - cz) * scale;
    const x1 = w / 2 + (east - cx) * scale;
    const y1 = h / 2 - (south - cz) * scale;
    ctx.globalAlpha = 0.9;
    ctx.drawImage(drawMinimap._img, x0, y0, x1 - x0, y1 - y0);
    ctx.globalAlpha = 1;
  }
  // player
  ctx.save();
  ctx.translate(w / 2, h / 2);
  ctx.rotate(-player.yaw);
  ctx.fillStyle = "#ffef6a";
  ctx.beginPath();
  ctx.moveTo(0, -8); ctx.lineTo(5, 6); ctx.lineTo(-5, 6);
  ctx.closePath(); ctx.fill();
  ctx.restore();
  ctx.strokeStyle = "rgba(255,255,255,.5)";
  ctx.strokeRect(0.5, 0.5, w - 1, h - 1);
  ctx.fillStyle = "#fff";
  ctx.font = "10px sans-serif";
  ctx.fillText("N", w / 2 - 3, 12);
}

// UI
document.getElementById("btnView").onclick = () => { firstPerson = !firstPerson; };
document.getElementById("btnExag").onclick = toggleExag;
document.getElementById("btnCredits").onclick = () => toggleCredits(true);
document.getElementById("btnCloseCredits").onclick = () => toggleCredits(false);
document.getElementById("btnCopy").onclick = async () => {
  try { await navigator.clipboard.writeText(CREDIT_BLOCK); } catch {}
};
document.getElementById("btnHelp").onclick = () => document.getElementById("help").style.display = "block";
document.getElementById("btnHelpClose").onclick = () => document.getElementById("help").style.display = "none";

function toggleExag() {
  exag = exag === 1 ? 1.5 : exag === 1.5 ? 2 : 1;
  world.setExaggeration(exag);
  document.getElementById("btnExag").textContent = `Exag. ${exag}× (E)`;
}
function toggleCredits(on) {
  document.getElementById("credits").style.display = on ? "block" : "none";
}
window.addEventListener("keydown", (e) => {
  if (e.code === "KeyV") firstPerson = !firstPerson;
  if (e.code === "KeyE") toggleExag();
  if (e.code === "KeyC") {
    const el = document.getElementById("credits");
    toggleCredits(el.style.display !== "block");
  }
  if (e.code === "Escape") toggleCredits(false);
});

start().catch((err) => {
  loadEl.innerHTML = `<div>Failed to load: ${err}</div>`;
  console.error(err);
  window.__READY = 'error';
  window.__ERROR = String(err && err.stack || err);
});
