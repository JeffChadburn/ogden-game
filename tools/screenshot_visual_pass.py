#!/usr/bin/env python3
"""Capture visual-pass before/after screenshots via Chrome DevTools."""
from __future__ import annotations

import argparse
import os
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
OUT = Path("/workspace/ogden/screens/visual-pass")
BASE = "http://127.0.0.1:8877/?help=0"

JOBS = [
    ("street_25th_washington.png", f"{BASE}&shot=street", 1280, 800),
    ("aerial_overview.png", f"{BASE}&shot=aerial", 1280, 800),
    ("foothills.png", f"{BASE}&shot=foothills", 1280, 800),
    ("phone_390x844.png", f"{BASE}&shot=phone", 390, 844),
]


def chrome_cmd(width, height):
    return [
        CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
        "--hide-scrollbars", "--no-first-run", "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disk-cache-size=1",
        f"--user-data-dir=/tmp/ogden-chrome-{os.getpid()}-{width}",
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


def wait_http(url, tries=40):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"no debug port at {url}")


def pick_page_ws():
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
    pages = [t for t in tabs if t.get("type") == "page" and not t.get("url", "").startswith("chrome-extension://")]
    if not pages:
        # open a fresh tab
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
        tab = json.load(urllib.request.urlopen(req))
        return tab["webSocketDebuggerUrl"]
    # prefer about:blank or the most recently opened page
    pages.sort(key=lambda t: 0 if t.get("url", "").startswith("about:") else 1)
    return pages[0]["webSocketDebuggerUrl"]


def capture(name, url, width, height, wait=60):
    proc = subprocess.Popen(chrome_cmd(width, height), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait_http(f"http://127.0.0.1:{PORT}/json/version")
        time.sleep(0.3)
        wsurl = pick_page_ws()
        cdp = CDP(wsurl)
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        try:
            cdp.send("Network.enable")
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        except Exception:
            pass
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 500,
        })
        cdp.send("Page.navigate", {"url": url})
        ready = False
        start = time.time()
        last = None
        while time.time() - start < wait:
            res = cdp.send("Runtime.evaluate", {
                "expression": "({ready: window.__READY, err: String(window.__ERROR||''), href: location.href})",
                "returnByValue": True,
            })
            last = (res.get("result") or {}).get("value")
            if last and last.get("ready") is True:
                ready = True
                break
            if last and last.get("ready") == "error":
                print(f"  PAGE ERROR: {last}", file=sys.stderr)
                break
            time.sleep(0.4)
        if not ready:
            print(f"  WARNING: {name} timed out waiting for __READY last={last}", file=sys.stderr)
        time.sleep(2.0)
        cdp.send("Runtime.evaluate", {
            "expression": "document.getElementById('help') && (document.getElementById('help').style.display='none')",
            "returnByValue": True,
        })
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
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", choices=["before", "after"])
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, url, w, h in JOBS:
        out_name = f"{args.prefix}_{name}"
        print("capturing", out_name, url)
        capture(out_name, url, w, h, wait=70)


if __name__ == "__main__":
    main()
