#!/usr/bin/env python3
"""Capture prototype screenshots via Chrome DevTools, waiting for window.__READY."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from websocket import create_connection

CHROME = "google-chrome"
PORT = 9233
OUT = Path("/workspace/ogden/screens")


def chrome_cmd(width, height):
    return [
        CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
        "--hide-scrollbars", "--no-first-run", "--disable-extensions",
        f"--remote-debugging-port={PORT}",
        f"--window-size={width},{height}",
        "about:blank",
    ]


class CDP:
    def __init__(self, url):
        self.ws = create_connection(url, timeout=60)
        self.i = 0

    def send(self, method, params=None, timeout=60):
        self.i += 1
        mid = self.i
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait_http(url, tries=30):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"no debug port at {url}")


def capture(name, url, width, height, wait=45):
    proc = subprocess.Popen(chrome_cmd(width, height), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait_http(f"http://127.0.0.1:{PORT}/json/version")
        ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version"))
        # new tab
        # Use the default target
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
        wsurl = tabs[0]["webSocketDebuggerUrl"] if tabs else ver["webSocketDebuggerUrl"]
        cdp = CDP(wsurl)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 500,
        })
        cdp.send("Page.navigate", {"url": url})
        # wait for __READY
        ready = False
        start = time.time()
        while time.time() - start < wait:
            res = cdp.send("Runtime.evaluate", {"expression": "window.__READY === true", "returnByValue": True})
            if (res.get("result") or {}).get("value") is True:
                ready = True
                break
            time.sleep(0.4)
        if not ready:
            print(f"  WARNING: {name} timed out waiting for __READY", file=sys.stderr)
        # extra frames for textures
        time.sleep(1.5)
        cdp.send("Runtime.evaluate", {"expression": "document.getElementById('help') && (document.getElementById('help').style.display='none')", "returnByValue": True})
        shot = cdp.send("Page.captureScreenshot", {"format": "png", "fromSurface": True}, timeout=60)
        data = base64.b64decode(shot["data"])
        path = OUT / name
        path.write_bytes(data)
        print(f"  wrote {path} ({len(data)} bytes) ready={ready}")
        cdp.close()
        return path, ready
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        time.sleep(0.4)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    base = "http://127.0.0.1:8877/?help=0"
    jobs = [
        ("street_25th_washington.png", f"{base}&shot=street", 1280, 800),
        ("aerial_overview.png", f"{base}&shot=aerial", 1280, 800),
        ("phone_390x844.png", f"{base}&shot=phone", 390, 844),
    ]
    for name, url, w, h in jobs:
        print("capturing", name, url)
        capture(name, url, w, h, wait=50)

if __name__ == "__main__":
    main()
