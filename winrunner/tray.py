"""Stashify Runner tray companion (PySide6).

A Qt system-tray app with a OneDrive-style flyout: click the tray icon and a
compact, themed panel pops up anchored to the tray, showing node status, both
GPUs, and active jobs. It auto-hides when it loses focus; a pin button undocks
it into a movable floating window. Themed to match Kanagawa (HomeLabber/HomeBoy).

Runs in the user session (the runner itself is the headless scheduled task); it
talks to the local runner over HTTP.
"""
import os
import re
import sys
import json
import html
import webbrowser
import subprocess
import urllib.request

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QPoint
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush
from PySide6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QWidget, QFrame,
                               QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QProgressBar, QGraphicsDropShadowEffect, QSizePolicy)

HERE = os.path.dirname(os.path.abspath(__file__))

# Kanagawa palette (matches HomeLabber/HomeBoy)
C = {"base": "#1f1f28", "card": "#16161d", "elev": "#2a2a37", "line": "#363646",
     "fg": "#dcd7ba", "muted": "#89887f", "primary": "#7e9cd8", "accent": "#957fb8",
     "ok": "#76946a", "warn": "#ff9e3b", "down": "#c34043"}


def load_cfg():
    p = os.environ.get("STASHIFY_RUNNER_CONFIG") or os.path.join(
        os.environ.get("LOCALAPPDATA", HERE), "StashifyRunner", "config.json")
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"port": 8712, "token": ""}


CFG = load_cfg()
BASE = "http://localhost:%s" % CFG.get("port", 8712)
TOKEN = CFG.get("token", "")


def api(path, method="GET"):
    req = urllib.request.Request(BASE + path, data=(b"" if method == "POST" else None),
                                 headers={"X-Runner-Token": TOKEN}, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=5).read() or b"{}")


# --------------------------------------------------------------------------- #
# background poller (keeps the UI responsive)
# --------------------------------------------------------------------------- #

class Poller(QThread):
    data = Signal(dict)

    def __init__(self):
        super().__init__()
        self._cursors = {}      # jid -> last seen log seq

    def run(self):
        while not self.isInterruptionRequested():
            out = {"online": False, "log": []}
            try:
                out["health"] = api("/health")
                out["stats"] = api("/stats")
                out["jobs"] = api("/jobs")
                out["online"] = True
                active = [j for j in out["jobs"] if j.get("state") in ("queued", "running")]
                for j in active[:2]:
                    jid = j.get("id", "")
                    try:
                        r = api("/jobs/%s/log?after=%d" % (jid, self._cursors.get(jid, 0)))
                        lines = r.get("lines") or []
                        if lines:
                            self._cursors[jid] = lines[-1].get("seq", 0)
                            for x in lines:
                                out["log"].append({"op": j.get("op") or "",
                                                   "level": x.get("level", "proc"),
                                                   "text": x.get("text", "")})
                    except Exception:  # noqa: BLE001 - log fetch is best-effort
                        pass
                gone = set(self._cursors) - {j.get("id") for j in out["jobs"]}
                for jid in gone:
                    self._cursors.pop(jid, None)
            except Exception:  # noqa: BLE001
                pass
            self.data.emit(out)
            for _ in range(20):                    # ~2s, but responsive to stop
                if self.isInterruptionRequested():
                    return
                self.msleep(100)


# --------------------------------------------------------------------------- #
# small widgets
# --------------------------------------------------------------------------- #

def _lbl(text="", color=None, size=11, bold=False, muted=False):
    la = QLabel(text)
    col = color or (C["muted"] if muted else C["fg"])
    la.setStyleSheet("color:%s;font-size:%dpx;%s" % (col, size, "font-weight:600;" if bold else ""))
    return la


def gauge():
    g = QProgressBar()
    g.setRange(0, 100)
    g.setTextVisible(False)
    g.setFixedHeight(6)
    g.setStyleSheet(
        "QProgressBar{background:%s;border:none;border-radius:3px;}"
        "QProgressBar::chunk{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
        "stop:0 %s,stop:1 %s);border-radius:3px;}" % (C["elev"], C["primary"], C["accent"]))
    return g


class GpuRow(QWidget):
    def __init__(self, lane_label):
        super().__init__()
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(3)
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0)
        self.name = _lbl("—", bold=True, size=12)
        self.lane = _lbl(lane_label, muted=True, size=9)
        top.addWidget(self.name); top.addStretch(1); top.addWidget(self.lane)
        v.addLayout(top)
        self.bar = gauge(); v.addWidget(self.bar)
        self.stats = _lbl("", muted=True, size=10); v.addWidget(self.stats)

    def update_gpu(self, g):
        self.name.setText(g.get("name") or "—")
        self.bar.setValue(int(g.get("util") or 0))
        bits = []
        if g.get("util") is not None:
            bits.append("%d%%" % round(g["util"]))
        if g.get("mem_used") is not None and g.get("mem_total"):
            bits.append("%d/%d MB" % (round(g["mem_used"]), round(g["mem_total"])))
        if g.get("temp") is not None:
            bits.append("%d°C" % round(g["temp"]))
        if g.get("power") is not None:
            bits.append("%d W" % round(g["power"]))
        self.stats.setText("  ·  ".join(bits))


class JobRow(QFrame):
    def __init__(self, job):
        super().__init__()
        self.setStyleSheet("QFrame{background:%s;border:1px solid %s;border-radius:8px;}" % (C["elev"], C["line"]))
        v = QVBoxLayout(self); v.setContentsMargins(9, 7, 9, 7); v.setSpacing(4)
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0); top.setSpacing(6)
        op = QLabel((job.get("op") or "").upper())
        op.setStyleSheet("color:#9dc3ff;background:rgba(126,156,216,.18);border-radius:4px;"
                         "padding:1px 5px;font-size:9px;font-weight:600;")
        name = (job.get("output_path") or "").split("/")[-1] or job.get("id", "")
        top.addWidget(op)
        top.addWidget(_lbl(name, size=11)); top.addStretch(1)
        top.addWidget(_lbl(job.get("state", ""), muted=True, size=10))
        v.addLayout(top)
        bar = gauge(); bar.setValue(int((job.get("progress") or 0) * 100)); v.addWidget(bar)
        bits = []
        if job.get("frame") and job.get("total_frames"):
            bits.append("%s/%s" % (job["frame"], job["total_frames"]))
        if job.get("fps") is not None:
            bits.append("%.1f fps" % job["fps"])
        if job.get("eta") is not None:
            bits.append("eta %d:%02d" % (job["eta"] // 60, job["eta"] % 60))
        if bits:
            v.addWidget(_lbl("  ·  ".join(bits), muted=True, size=10))


_LOG_COLORS = {"error": C["down"], "warn": C["warn"], "event": C["primary"]}
_RE_NUMS = re.compile(r"[\d][\d.,:]*")
_RE_BAR = re.compile(r"\|[#\s.▁-▉]*\|")   # tqdm bar visual - waste of width


class LogBox(QFrame):
    """Tiny live log tail. Lines that differ only by numbers (progress ticks,
    fps, ETAs) update the existing line in place instead of appending, so a
    tqdm-style stream stays a single line; genuinely new text gets a new line."""
    MAX_LINES = 6

    def __init__(self):
        super().__init__()
        self.setStyleSheet("QFrame{background:%s;border:1px solid %s;border-radius:8px;}"
                           % (C["card"], C["line"]))
        v = QVBoxLayout(self); v.setContentsMargins(9, 6, 9, 6)
        self.lab = QLabel("")
        self.lab.setTextFormat(Qt.RichText)
        self.lab.setStyleSheet("color:%s;font-family:Consolas,monospace;font-size:9px;"
                               "background:transparent;border:none;" % C["muted"])
        v.addWidget(self.lab)
        self.entries = []   # [norm_key, text, level]

    def push(self, text, level="proc"):
        t = _RE_BAR.sub("|", str(text or "").strip())[:110]
        if not t:
            return
        norm = _RE_NUMS.sub("#", t)
        if self.entries and self.entries[-1][0] == norm:
            self.entries[-1][1] = t
            self.entries[-1][2] = level
        else:
            self.entries.append([norm, t, level])
            del self.entries[:-self.MAX_LINES]
        self._render()

    def _render(self):
        rows = []
        for _, t, level in self.entries:
            disp = html.escape(t if len(t) <= 58 else t[:57] + "…")
            rows.append('<span style="color:%s">%s</span>'
                        % (_LOG_COLORS.get(level, C["muted"]), disp))
        self.lab.setText("<br>".join(rows))


# --------------------------------------------------------------------------- #
# the flyout
# --------------------------------------------------------------------------- #

class Flyout(QWidget):
    def __init__(self):
        super().__init__()
        self.pinned = False
        self._drag = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(340)

        self.card = QFrame(self)
        self.card.setObjectName("card")
        self.card.setStyleSheet("#card{background:%s;border:1px solid %s;border-radius:14px;}" % (C["base"], C["line"]))
        shadow = QGraphicsDropShadowEffect(blurRadius=34, xOffset=0, yOffset=10)
        shadow.setColor(QColor(0, 0, 0, 170)); self.card.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self); outer.setContentsMargins(12, 12, 12, 12); outer.addWidget(self.card)
        v = QVBoxLayout(self.card); v.setContentsMargins(14, 12, 14, 12); v.setSpacing(10)

        # header
        head = QHBoxLayout(); head.setSpacing(8)
        self.dot = QLabel("●"); self.dot.setStyleSheet("color:%s;font-size:12px;" % C["muted"])
        self.title = _lbl("Stashify Runner", bold=True, size=14)
        head.addWidget(self.dot); head.addWidget(self.title); head.addStretch(1)
        self.pinBtn = self._icon_btn("⤢", "Undock / pin"); self.pinBtn.clicked.connect(self.toggle_pin)
        openBtn = self._icon_btn("⧉", "Open full dashboard"); openBtn.clicked.connect(lambda: webbrowser.open(BASE + "/"))
        self.closeBtn = self._icon_btn("✕", "Close"); self.closeBtn.clicked.connect(self.hide)
        head.addWidget(self.pinBtn); head.addWidget(openBtn); head.addWidget(self.closeBtn)
        self.header = head
        v.addLayout(head)
        self.status = _lbl("connecting…", muted=True, size=11); v.addWidget(self.status)

        # gpus
        self.aiRow = GpuRow("AI lane"); v.addWidget(self.aiRow)
        self.igRow = GpuRow("transcode lane"); v.addWidget(self.igRow)

        # jobs
        v.addWidget(self._sep())
        self.jobsLbl = _lbl("JOBS", muted=True, size=10); v.addWidget(self.jobsLbl)
        self.jobsBox = QVBoxLayout(); self.jobsBox.setSpacing(6); v.addLayout(self.jobsBox)
        self.noJobs = _lbl("No active jobs.", muted=True, size=11); self.jobsBox.addWidget(self.noJobs)

        # live log tail (hidden while idle to keep the panel compact)
        self.logBox = LogBox(); self.logBox.setVisible(False); v.addWidget(self.logBox)

        # footer
        foot = QHBoxLayout(); foot.setSpacing(8)
        self.pauseBtn = self._btn("Pause node"); self.pauseBtn.clicked.connect(self.toggle_pause)
        dashBtn = self._btn("Dashboard", primary=True); dashBtn.clicked.connect(lambda: webbrowser.open(BASE + "/"))
        foot.addWidget(self.pauseBtn); foot.addStretch(1); foot.addWidget(dashBtn)
        v.addWidget(self._sep()); v.addLayout(foot)

        self._paused = False
        self._job_rows = []

    def _icon_btn(self, glyph, tip):
        b = QPushButton(glyph); b.setToolTip(tip); b.setFixedSize(24, 24); b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet("QPushButton{background:transparent;color:%s;border:none;font-size:13px;border-radius:6px;}"
                        "QPushButton:hover{background:%s;color:%s;}" % (C["muted"], C["elev"], C["fg"]))
        return b

    def _btn(self, text, primary=False):
        b = QPushButton(text); b.setCursor(Qt.PointingHandCursor)
        if primary:
            b.setStyleSheet("QPushButton{background:%s;color:%s;border:none;border-radius:8px;padding:6px 14px;font-size:12px;font-weight:600;}"
                            "QPushButton:hover{background:%s;}" % (C["primary"], C["base"], C["accent"]))
        else:
            b.setStyleSheet("QPushButton{background:%s;color:%s;border:1px solid %s;border-radius:8px;padding:6px 14px;font-size:12px;}"
                            "QPushButton:hover{border-color:%s;color:%s;}" % (C["elev"], C["fg"], C["line"], C["primary"], C["fg"]))
        return b

    def _sep(self):
        s = QFrame(); s.setFixedHeight(1); s.setStyleSheet("background:%s;border:none;" % C["line"]); return s

    # ---- data ----
    def apply(self, d):
        online = d.get("online")
        h = d.get("health", {}); st = d.get("stats", {}); jobs = d.get("jobs", [])
        if not online:
            self.dot.setStyleSheet("color:%s;font-size:12px;" % C["down"]); self.status.setText("runner offline"); return
        self._paused = bool(h.get("paused"))
        busy = bool(h.get("busy"))
        col = C["warn"] if self._paused else (C["ok"] if busy else C["muted"])
        self.dot.setStyleSheet("color:%s;font-size:12px;" % col)
        self.title.setText("Stashify · " + (h.get("node") or "runner"))
        enc = h.get("encoders", {})
        self.status.setText(("paused" if self._paused else ("working" if busy else "idle"))
                            + "  ·  " + "/".join(h.get("ops", [])) + "  ·  enc " + (enc.get("transcode") or "?"))
        self.pauseBtn.setText("Resume node" if self._paused else "Pause node")
        gpus = st.get("gpus", {})
        self.aiRow.update_gpu(gpus.get("nvidia", {}))
        self.igRow.update_gpu(gpus.get("igpu", {}))
        # jobs (only active/recent running ones)
        for r in self._job_rows:
            r.setParent(None)
        self._job_rows = []
        active = [j for j in jobs if j.get("state") in ("queued", "running")]
        self.noJobs.setVisible(not active)
        for j in active[:4]:
            row = JobRow(j); self.jobsBox.addWidget(row); self._job_rows.append(row)
        for ln in d.get("log") or []:
            self.logBox.push(ln.get("text", ""), ln.get("level", "proc"))
        self.logBox.setVisible(bool(active) and bool(self.logBox.entries))
        self.adjustSize()

    def toggle_pause(self):
        try:
            api("/node/" + ("resume" if self._paused else "pause"), method="POST")
        except Exception:  # noqa: BLE001
            pass

    # ---- window behaviour ----
    def anchor(self):
        scr = QApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        x = scr.right() - self.width() - 8
        y = scr.bottom() - self.height() - 8
        self.move(max(scr.left(), x), max(scr.top(), y))

    def popup(self):
        if self.isVisible() and not self.pinned:
            self.hide(); return
        self.anchor(); self.show(); self.raise_(); self.activateWindow()

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.pinBtn.setText("▾" if self.pinned else "⤢")
        self.pinBtn.setStyleSheet(self.pinBtn.styleSheet() +
                                  ("QPushButton{color:%s;}" % C["primary"] if self.pinned else ""))
        if not self.pinned:
            self.anchor()

    def event(self, e):
        # OneDrive-style: hide on focus loss unless pinned
        if e.type() == e.Type.WindowDeactivate and not self.pinned:
            self.hide()
        return super().event(e)

    # drag when pinned (header area)
    def mousePressEvent(self, e):
        if self.pinned and e.position().y() < 46:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = None
        super().mouseReleaseEvent(e)


# --------------------------------------------------------------------------- #
# tray icon
# --------------------------------------------------------------------------- #

def status_icon(color):
    png = os.path.join(HERE, "tray-icon.png")
    pm = QPixmap(png) if os.path.isfile(png) else QPixmap(64, 64)
    if pm.isNull():
        pm = QPixmap(64, 64); pm.fill(QColor(C["base"]))
    pm = pm.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor(color))); p.setPen(QColor(10, 15, 20))
    p.drawEllipse(42, 42, 18, 18); p.end()
    return QIcon(pm)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    flyout = Flyout()
    colors = {"idle": C["muted"], "working": C["ok"], "paused": C["warn"], "offline": C["down"]}
    tray = QSystemTrayIcon(status_icon(colors["idle"]))
    tray.setToolTip("Stashify Runner")

    menu = QMenu()
    menu.setStyleSheet("QMenu{background:%s;color:%s;border:1px solid %s;border-radius:8px;padding:4px;}"
                       "QMenu::item{padding:6px 22px;border-radius:5px;}"
                       "QMenu::item:selected{background:%s;}" % (C["card"], C["fg"], C["line"], C["elev"]))
    act_open = menu.addAction("Open panel"); act_open.triggered.connect(flyout.popup)
    act_dash = menu.addAction("Open dashboard"); act_dash.triggered.connect(lambda: webbrowser.open(BASE + "/"))
    menu.addSeparator()
    act_restart = menu.addAction("Restart runner (admin)")
    act_restart.triggered.connect(lambda: subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", "Start-Process schtasks -Verb RunAs -ArgumentList '/End','/TN','StashifyRunner'; "
         "Start-Sleep 2; Start-Process schtasks -Verb RunAs -ArgumentList '/Run','/TN','StashifyRunner'"]))
    act_quit = menu.addAction("Quit tray"); act_quit.triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: flyout.popup() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()

    def on_data(d):
        flyout.apply(d)
        h = d.get("health", {})
        s = "offline" if not d.get("online") else ("paused" if h.get("paused") else ("working" if h.get("busy") else "idle"))
        tray.setIcon(status_icon(colors[s]))
        tray.setToolTip("Stashify Runner — %s" % s)

    poller = Poller(); poller.data.connect(on_data); poller.start()
    app.aboutToQuit.connect(lambda: (poller.requestInterruption(), poller.wait(1500)))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
