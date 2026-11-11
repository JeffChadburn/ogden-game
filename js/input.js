export function createInput(canvas) {
  const keys = new Set();
  const look = { dx: 0, dy: 0 };
  const move = { x: 0, y: 0 };
  let pointerLocked = false;
  const isTouch = matchMedia("(pointer: coarse)").matches || "ontouchstart" in window;

  window.addEventListener("keydown", (e) => {
    keys.add(e.code);
    if (["KeyW","KeyA","KeyS","KeyD","Space"].includes(e.code)) e.preventDefault();
  });
  window.addEventListener("keyup", (e) => keys.delete(e.code));

  canvas.addEventListener("click", () => {
    if (!isTouch) canvas.requestPointerLock?.();
  });
  document.addEventListener("pointerlockchange", () => {
    pointerLocked = document.pointerLockElement === canvas;
  });
  document.addEventListener("mousemove", (e) => {
    if (!pointerLocked) return;
    look.dx += e.movementX;
    look.dy += e.movementY;
  });

  // Virtual joystick (left) and look pad (right) for phones
  const joy = document.getElementById("joy");
  const knob = joy?.querySelector(".knob");
  const lookpad = document.getElementById("lookpad");
  if (isTouch) {
    joy.style.display = "block";
    lookpad.style.display = "block";
  }

  function bindStick(el, target, onEndZero) {
    let pid = null, cx, cy, r;
    const start = (e) => {
      const t = e.changedTouches[0];
      pid = t.identifier;
      const b = el.getBoundingClientRect();
      cx = b.left + b.width / 2;
      cy = b.top + b.height / 2;
      r = b.width / 2;
      el.setPointerCapture?.(t.identifier);
      moveFrom(t);
      e.preventDefault();
    };
    const moveFrom = (t) => {
      const dx = t.clientX - cx;
      const dy = t.clientY - cy;
      const m = Math.hypot(dx, dy);
      const k = m > r ? r / m : 1;
      target.x = (dx * k) / r;
      target.y = (dy * k) / r;
      if (knob && el === joy) {
        knob.style.left = `${33 + target.x * 30}px`;
        knob.style.top = `${33 + target.y * 30}px`;
      }
    };
    const moveH = (e) => {
      for (const t of e.changedTouches) if (t.identifier === pid) moveFrom(t);
    };
    const end = (e) => {
      for (const t of e.changedTouches) if (t.identifier === pid) {
        pid = null;
        if (onEndZero) { target.x = 0; target.y = 0; }
        if (knob && el === joy) { knob.style.left = "33px"; knob.style.top = "33px"; }
      }
    };
    el.addEventListener("touchstart", start, { passive: false });
    el.addEventListener("touchmove", moveH, { passive: false });
    el.addEventListener("touchend", end);
    el.addEventListener("touchcancel", end);
  }
  if (isTouch) {
    bindStick(joy, move, true);
    let lastX, lastY, lookId = null;
    lookpad.addEventListener("touchstart", (e) => {
      const t = e.changedTouches[0];
      lookId = t.identifier; lastX = t.clientX; lastY = t.clientY;
      e.preventDefault();
    }, { passive: false });
    lookpad.addEventListener("touchmove", (e) => {
      for (const t of e.changedTouches) if (t.identifier === lookId) {
        look.dx += t.clientX - lastX;
        look.dy += t.clientY - lastY;
        lastX = t.clientX; lastY = t.clientY;
      }
    }, { passive: false });
    lookpad.addEventListener("touchend", () => { lookId = null; });
  }

  return {
    keys, look, move, isTouch,
    consumeLook() {
      const d = { dx: look.dx, dy: look.dy };
      look.dx = look.dy = 0;
      return d;
    },
    walkVector() {
      let x = move.x, z = move.y;
      if (keys.has("KeyA")) x -= 1;
      if (keys.has("KeyD")) x += 1;
      if (keys.has("KeyW")) z -= 1;
      if (keys.has("KeyS")) z += 1;
      const mag = Math.hypot(x, z);
      if (mag > 1) { x /= mag; z /= mag; }
      const slow = keys.has("ShiftLeft") || keys.has("ShiftRight");
      const speed = mag === 0 ? 0 : (slow ? 0.45 : Math.min(1, mag));
      return { x, z, speed, mag: Math.min(1, mag || (x || z ? 1 : 0)) };
    },
  };
}
