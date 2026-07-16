"""Stashify Runner tray companion.

Runs in the user session (the service itself is headless in session 0). Shows
node status as a colored badge on the Stashify icon and offers quick controls,
all by talking to the runner service over its localhost HTTP API. Launch at
login from the Startup folder (install.ps1 wires this up).
"""
import os
import json
import time
import threading
import webbrowser
import subprocess
import urllib.request

import pystray
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))


def load_cfg():
    p = os.environ.get("STASHIFY_RUNNER_CONFIG") or os.path.join(
        os.environ.get("LOCALAPPDATA", HERE), "StashifyRunner", "config.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"port": 8712, "token": ""}


CFG = load_cfg()
BASE = "http://localhost:%s" % CFG.get("port", 8712)
TOKEN = CFG.get("token", "")
ICON_PNG = os.path.join(HERE, "tray-icon.png")
STATE = {"status": "connecting"}
COLORS = {"idle": (150, 150, 150, 255), "working": (106, 192, 106, 255),
          "paused": (224, 165, 74, 255), "offline": (192, 57, 43, 255),
          "connecting": (120, 120, 120, 255)}


def api(path, method="GET"):
    req = urllib.request.Request(BASE + path, data=(b"" if method == "POST" else None),
                                 headers={"X-Lada-Token": TOKEN}, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=5).read() or b"{}")


def _base_img():
    try:
        return Image.open(ICON_PNG).convert("RGBA").resize((64, 64))
    except Exception:  # noqa: BLE001 - fallback: plain tile
        im = Image.new("RGBA", (64, 64), (28, 41, 64, 255))
        ImageDraw.Draw(im).text((22, 20), "S", fill=(53, 224, 202, 255))
        return im


def status_img(status):
    im = _base_img()
    d = ImageDraw.Draw(im)
    d.ellipse([42, 42, 62, 62], fill=COLORS.get(status, COLORS["connecting"]),
              outline=(10, 15, 20, 255), width=2)
    return im


def poll(icon):
    while True:
        try:
            h = api("/health")
            status = "paused" if h.get("paused") else ("working" if h.get("busy") else "idle")
            node = h.get("node", "runner")
        except Exception:  # noqa: BLE001
            status, node = "offline", "runner"
        STATE["status"] = status
        icon.icon = status_img(status)
        icon.title = "Stashify Runner (%s) — %s" % (node, status)
        try:
            icon.update_menu()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(4)


def on_open(icon, item):
    webbrowser.open(BASE + "/")


def on_toggle(icon, item):
    try:
        h = api("/health")
        api("/node/" + ("resume" if h.get("paused") else "pause"), method="POST")
    except Exception:  # noqa: BLE001
        pass


def on_restart(icon, item):
    # service control needs admin -> elevate a one-shot restart
    subprocess.Popen(["powershell", "-NoProfile", "-Command",
                      "Start-Process sc.exe -Verb RunAs -ArgumentList 'stop','stashify-runner';"
                      "Start-Sleep 3;"
                      "Start-Process sc.exe -Verb RunAs -ArgumentList 'start','stashify-runner'"])


def on_quit(icon, item):
    icon.stop()


def build_menu():
    return pystray.Menu(
        pystray.MenuItem(lambda i: "● %s" % STATE["status"], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open dashboard", on_open, default=True),
        pystray.MenuItem(lambda i: "Resume node" if STATE["status"] == "paused" else "Pause node", on_toggle),
        pystray.MenuItem("Restart service (admin)", on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit tray", on_quit))


def main():
    icon = pystray.Icon("stashify-runner", status_img("connecting"), "Stashify Runner", build_menu())
    threading.Thread(target=poll, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
