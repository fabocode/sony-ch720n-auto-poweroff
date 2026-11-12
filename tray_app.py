#!/usr/bin/env python3
"""System-tray / menu-bar app for the Sony WH-CH720N audio-idle auto power-off.

Self-contained: it reads the headphones' audio activity directly, shows the live
state in the icon, and powers the headphones off (via sony_poweroff.py) after a
configurable amount of silence.
  Windows: playback peak meter (pycaw / Core Audio), see audio_windows.py.
  macOS:   "device is running somewhere" (CoreAudio), see audio_macos.py.

Icon color reflects the audio state:
  - green  = connected and playing
  - amber  = connected and silent (counting down to power-off)
  - gray   = disconnected, or monitoring paused

Menu: status, pause/resume, "Power off after" (5/10/15/30/60 min, persisted),
power off now, open log, start at login, quit.

Single-instance: a named mutex (Windows) or a file lock (macOS) prevents
duplicate icons.

Run hidden (no console):
  Windows: .venv\\Scripts\\pythonw.exe tray_app.py   (or Start-Tray.cmd)
  macOS:   .venv/bin/python tray_app.py             (or start-tray.command)
"""
import json
import subprocess
import sys
import threading
from datetime import datetime

import pystray
from pystray import Menu, MenuItem as Item
from PIL import Image, ImageDraw

import platform_glue as glue
from idle_tracker import DISCONNECTED, PLAYING, SILENT, IdleTracker

if glue.IS_WINDOWS:
    from audio_windows import AudioMeter
else:
    from audio_macos import AudioMeter

POWEROFF = glue.HERE / "sony_poweroff.py"
LOG = glue.LOG
CONFIG = glue.CONFIG

APP_NAME = "Sony CH720N Auto-Off"
INSTANCE_NAME = "SonyCH720NTray"
PRESETS = [5, 10, 15, 30, 60]          # minutes shown in the menu
PAUSED = "paused"

DEFAULTS = {
    "idle_minutes": 10,
    "poll_seconds": 2,
    # Windows: peak level. macOS: the level is 0 or 1, so any value in (0, 1) works.
    "silence_threshold": 0.0005,
    "device_match": "CH720N",
}

COLORS = {
    PLAYING: (40, 200, 90, 255),       # green
    SILENT: (235, 170, 40, 255),       # amber
    DISCONNECTED: (140, 140, 140, 255),
    PAUSED: (140, 140, 140, 255),
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def make_icon(state: str) -> Image.Image:
    fg = COLORS.get(state, COLORS[DISCONNECTED])
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.arc([12, 12, 52, 50], start=180, end=360, fill=fg, width=6)   # headband
    d.rounded_rectangle([12, 32, 23, 52], radius=4, fill=fg)        # left cup
    d.rounded_rectangle([41, 32, 52, 52], radius=4, fill=fg)        # right cup
    return img


def _fmt_min(seconds: int) -> str:
    # Whole minutes, rounded up, so the tooltip changes only once per minute
    # (no second-by-second ticking).
    minutes = (max(0, int(seconds)) + 59) // 60
    return f"{minutes} min"


def log_line(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


class TrayApp:
    def __init__(self):
        self.cfg = load_config()
        self.paused = False
        self.tracker = IdleTracker()
        self.meter = AudioMeter(self.cfg["device_match"])
        self._stop = threading.Event()
        self._status = "starting…"
        self._last_ui = None
        glue.prepare_ui()
        self.icon = pystray.Icon(APP_NAME, make_icon(DISCONNECTED), APP_NAME,
                                 menu=self._build_menu())

    # --- logging ----------------------------------------------------------------
    def _log(self, msg: str) -> None:
        log_line(msg)

    # --- power off --------------------------------------------------------------
    def _power_off(self, reason: str) -> None:
        self._log(f"Powering off ({reason})…")

        def worker():
            ok = False
            try:
                r = subprocess.run([glue.venv_python(), str(POWEROFF),
                                    "--device", self.cfg["device_match"]],
                                   creationflags=glue.NO_WINDOW_FLAGS,
                                   capture_output=True, text=True, timeout=30)
                ok = r.returncode == 0
                if not ok:
                    self._log(f"sony_poweroff rc={r.returncode}: {r.stderr.strip()}")
            except Exception as e:
                self._log(f"power-off error: {e!r}")
            self._log("power-off OK" if ok else "power-off FAILED")
            if reason == "idle":
                self._notify("Headphones powered off (audio idle)." if ok
                             else "Auto power-off failed.")

        threading.Thread(target=worker, daemon=True).start()

    def _notify(self, message: str) -> None:
        try:
            self.icon.notify(message, APP_NAME)
        except Exception as e:
            self._log(f"notify error: {e!r}")

    def power_off_now(self, *_):
        self._power_off("manual")

    # --- monitor loop -----------------------------------------------------------
    def _monitor(self):
        with self.meter.thread_context():
            while not self._stop.wait(self.cfg["poll_seconds"]):
                # Keep the loop alive across any transient error (audio API
                # hiccup, icon update, device churn) so it can run 24/7.
                try:
                    self._tick()
                except Exception as e:
                    self._log(f"monitor error: {e!r}")
                    self.meter.reset()

    def _tick(self):
        if self.paused:
            self._set_state(PAUSED, 0)
            return
        t = self.tracker.tick(self.meter.read_level(),
                              threshold=self.cfg["silence_threshold"],
                              poll_seconds=self.cfg["poll_seconds"],
                              idle_minutes=self.cfg["idle_minutes"])
        if t.power_off:
            self._power_off("idle")
        self._set_state(t.state, t.idle)

    def _set_state(self, state: str, idle: int):
        target = self.cfg["idle_minutes"] * 60
        if state == PLAYING:
            tip = f"Playing • off in {_fmt_min(target)}"
        elif state == SILENT:
            tip = f"Silent • off in {_fmt_min(target - idle)}"
        elif state == DISCONNECTED:
            tip = "Disconnected"
        else:
            tip = "Paused"
        self._status = tip
        key = (state, tip)
        if key == self._last_ui:
            return
        self._last_ui = key

        def update():
            self.icon.icon = make_icon(state)
            self.icon.title = f"{APP_NAME} — {tip}"
            self.icon.update_menu()
        glue.run_on_ui(update)

    # --- menu actions -----------------------------------------------------------
    def pause(self, *_):
        self.paused = True
        self.tracker.reset()
        self._log("Monitoring paused")
        self._set_state(PAUSED, 0)

    def resume(self, *_):
        self.paused = False
        self.tracker.reset()
        self._log("Monitoring resumed")

    def _set_idle(self, minutes: int):
        self.cfg["idle_minutes"] = minutes
        save_config(self.cfg)
        self.tracker.reset()
        self._log(f"Power-off delay set to {minutes} min")
        self.icon.update_menu()

    def open_log(self, *_):
        if LOG.exists():
            glue.open_file(LOG)
        else:
            self._notify("No log file yet.")

    def toggle_autostart(self, *_):
        enable = not glue.is_autostart()
        glue.set_autostart(enable)
        self._log(f"{glue.AUTOSTART_LABEL}: {'on' if enable else 'off'}")
        self.icon.update_menu()

    def quit(self, *_):
        self._log("Tray quitting")
        self._stop.set()
        self.icon.stop()

    # --- menu / run -------------------------------------------------------------
    def _preset_item(self, minutes: int) -> Item:
        # Nested 2-arg action (icon, item); pystray rejects actions with >2 args,
        # so capture `minutes` via closure instead of a default argument.
        def action(icon, item):
            self._set_idle(minutes)

        def checked(item):
            return self.cfg["idle_minutes"] == minutes

        return Item(f"{minutes} min", action, checked=checked, radio=True)

    def _build_menu(self) -> Menu:
        return Menu(
            Item(lambda item: f"Status: {self._status}", None, enabled=False),
            Menu.SEPARATOR,
            Item("Pause monitoring", self.pause, visible=lambda i: not self.paused),
            Item("Resume monitoring", self.resume, visible=lambda i: self.paused),
            Item("Power off after", Menu(*[self._preset_item(m) for m in PRESETS])),
            Item("Power off headphones now", self.power_off_now),
            Item("Open log", self.open_log),
            Item(glue.AUTOSTART_LABEL, self.toggle_autostart,
                 checked=lambda i: glue.is_autostart()),
            Menu.SEPARATOR,
            Item("Quit", self.quit),
        )

    def run(self):
        def setup(icon):
            icon.visible = True
            self._log(f"Tray started (idle={self.cfg['idle_minutes']} min)")
            threading.Thread(target=self._monitor, daemon=True).start()
        self.icon.run(setup=setup)


if __name__ == "__main__":
    _instance = glue.acquire_single_instance(INSTANCE_NAME)
    if _instance is None:
        sys.exit(0)            # another instance is already running
    try:
        TrayApp().run()
    except Exception:
        import traceback
        log_line(f"FATAL:\n{traceback.format_exc()}")
        sys.exit(1)            # non-zero -> the watchdog (or launchd) relaunches us
