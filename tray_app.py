#!/usr/bin/env python3
"""System-tray app for the Sony WH-CH720N audio-idle auto power-off.

Self-contained: it reads the headphone playback peak meter directly (pycaw /
Core Audio), shows the live state in the tray icon, and powers the headphones
off (via sony_poweroff.py) after a configurable amount of silence.

Tray icon color reflects the audio state:
  - green  = connected and playing
  - amber  = connected and silent (counting down to power-off)
  - gray   = disconnected, or monitoring paused

Menu: status, pause/resume, "Power off after" (5/10/15/30/60 min, persisted),
power off now, open log, start with Windows, quit.

Single-instance: a named mutex prevents duplicate icons.

Run hidden (no console) with the venv's pythonw, e.g. via Start-Tray.cmd:
  .venv\\Scripts\\pythonw.exe tray_app.py
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import comtypes
import pystray
from pystray import Menu, MenuItem as Item
from PIL import Image, ImageDraw
from comtypes import CLSCTX_ALL, cast, POINTER
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

HERE = Path(__file__).resolve().parent
POWEROFF = HERE / "sony_poweroff.py"
LAUNCHER = HERE / "tray_launcher.py"      # watchdog that autostart points at
VENV_PY = HERE / ".venv" / "Scripts" / "python.exe"
VENV_PYW = HERE / ".venv" / "Scripts" / "pythonw.exe"
_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
LOG = _LOCALAPPDATA / "sony-ch720n-tray.log"
CONFIG = _LOCALAPPDATA / "sony-ch720n-tray.json"
STARTUP_LNK = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" /
               "Start Menu" / "Programs" / "Startup" / "SonyCH720NTray.lnk")

APP_NAME = "Sony CH720N Auto-Off"
MUTEX_NAME = "Global\\SonyCH720NTray"
PRESETS = [5, 10, 15, 30, 60]          # minutes shown in the menu
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183

DEFAULTS = {
    "idle_minutes": 10,
    "poll_seconds": 2,
    "silence_threshold": 0.0005,
    "device_match": "CH720N",
}

COLORS = {
    "playing": (40, 200, 90, 255),     # green
    "silent": (235, 170, 40, 255),     # amber
    "disconnected": (140, 140, 140, 255),
    "paused": (140, 140, 140, 255),
}


def acquire_single_instance():
    """Return a mutex handle, or None if another instance already holds it."""
    h = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return h


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def make_icon(state: str) -> Image.Image:
    fg = COLORS.get(state, COLORS["disconnected"])
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


class TrayApp:
    def __init__(self):
        self.cfg = load_config()
        self.paused = False
        self.idle = 0
        self.suppress = False          # set after a power-off until the device drops
        self._meter = None             # cached IAudioMeterInformation (re-resolved on loss)
        self._stop = threading.Event()
        self._status = "starting…"
        self._last_ui = None
        self.icon = pystray.Icon(APP_NAME, make_icon("disconnected"), APP_NAME,
                                 menu=self._build_menu())

    # --- logging ----------------------------------------------------------------
    def _log(self, msg: str) -> None:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
        except OSError:
            pass

    # --- audio peak (pycaw / Core Audio) ---------------------------------------
    def _resolve_meter(self):
        """Find the matching render endpoint and cache its meter interface.
        Sets self._meter to None if the device is not currently active."""
        self._meter = None
        enum = AudioUtilities.GetDeviceEnumerator()
        coll = enum.EnumAudioEndpoints(0, 0x1)  # eRender, DEVICE_STATE_ACTIVE
        match = self.cfg["device_match"].lower()
        for i in range(coll.GetCount()):
            dev = coll.Item(i)
            try:
                name = AudioUtilities.CreateDevice(dev).FriendlyName or ""
            except Exception:
                name = ""
            if match in name.lower():
                iface = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
                self._meter = cast(iface, POINTER(IAudioMeterInformation))
                return

    def _read_peak(self) -> float:
        """Peak [0..1] of the matching endpoint; -1 if not active (off/disconnected).
        Caches the meter interface and only re-enumerates when it's gone — far
        fewer COM calls per poll than re-enumerating every time. On any COM error
        (e.g. device invalidated on disconnect) it resets and reports -1."""
        try:
            if self._meter is None:
                self._resolve_meter()
            if self._meter is None:
                return -1.0
            return self._meter.GetPeakValue()
        except Exception:
            self._meter = None
            return -1.0

    # --- power off --------------------------------------------------------------
    def _power_off(self, reason: str) -> None:
        self._log(f"Powering off ({reason})…")

        def worker():
            py = str(VENV_PY) if VENV_PY.exists() else "python"
            ok = False
            try:
                r = subprocess.run([py, str(POWEROFF)], creationflags=CREATE_NO_WINDOW,
                                   capture_output=True, text=True, timeout=30)
                ok = r.returncode == 0
            except Exception as e:
                self._log(f"power-off error: {e!r}")
            self._log("power-off OK" if ok else "power-off FAILED")
            if reason == "idle":
                self.icon.notify(
                    "Headphones powered off (audio idle)." if ok else "Auto power-off failed.",
                    APP_NAME)

        threading.Thread(target=worker, daemon=True).start()

    def power_off_now(self, *_):
        self._power_off("manual")

    # --- monitor loop -----------------------------------------------------------
    def _monitor(self):
        # MTA (multithreaded apartment): this worker only polls COM and never
        # pumps a Windows message loop, so STA would be incorrect and caused
        # intermittent access-violation crashes in _ctypes. MMDevice objects are
        # apartment-agnostic ("Both"), so MTA is safe here.
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            while not self._stop.wait(self.cfg["poll_seconds"]):
                # Keep the loop alive across any transient error (COM hiccup,
                # icon update, device churn) so it can run 24/7.
                try:
                    self._tick()
                except Exception as e:
                    self._log(f"monitor error: {e!r}")
                    self._meter = None
        finally:
            comtypes.CoUninitialize()

    def _tick(self):
        if self.paused:
            self._set_state("paused", 0)
            return
        peak = self._read_peak()
        target = self.cfg["idle_minutes"] * 60
        if peak < 0:
            self.suppress = False
            self.idle = 0
            self._set_state("disconnected", 0)
        elif peak < self.cfg["silence_threshold"]:
            if not self.suppress:
                self.idle += self.cfg["poll_seconds"]
                if self.idle >= target:
                    self.suppress = True
                    self._power_off("idle")
            self._set_state("silent", self.idle)
        else:
            self.suppress = False
            self.idle = 0
            self._set_state("playing", 0)

    def _set_state(self, state: str, idle: int):
        target = self.cfg["idle_minutes"] * 60
        if state == "playing":
            tip = f"Playing • off in {_fmt_min(target)}"
        elif state == "silent":
            tip = f"Silent • off in {_fmt_min(target - idle)}"
        elif state == "disconnected":
            tip = "Disconnected"
        else:
            tip = "Paused"
        self._status = tip
        key = (state, tip)
        if key == self._last_ui:
            return
        self._last_ui = key
        self.icon.icon = make_icon(state)
        self.icon.title = f"{APP_NAME} — {tip}"
        self.icon.update_menu()

    # --- menu actions -----------------------------------------------------------
    def pause(self, *_):
        self.paused = True
        self.idle = 0
        self._log("Monitoring paused")
        self._set_state("paused", 0)

    def resume(self, *_):
        self.paused = False
        self.idle = 0
        self._log("Monitoring resumed")

    def _set_idle(self, minutes: int):
        self.cfg["idle_minutes"] = minutes
        save_config(self.cfg)
        self.idle = 0
        self._log(f"Power-off delay set to {minutes} min")
        self.icon.update_menu()

    def open_log(self, *_):
        if LOG.exists():
            os.startfile(str(LOG))  # noqa: S606
        else:
            self.icon.notify("No log file yet.", APP_NAME)

    def is_autostart(self) -> bool:
        return STARTUP_LNK.exists()

    def toggle_autostart(self, *_):
        if self.is_autostart():
            try:
                STARTUP_LNK.unlink()
            except FileNotFoundError:
                pass
        else:
            target = str(VENV_PYW if VENV_PYW.exists() else VENV_PY)
            STARTUP_LNK.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 "$ws = New-Object -ComObject WScript.Shell; "
                 f"$s = $ws.CreateShortcut('{STARTUP_LNK}'); "
                 f"$s.TargetPath = '{target}'; "
                 f"$s.Arguments = '\"{LAUNCHER}\"'; "
                 f"$s.WorkingDirectory = '{HERE}'; "
                 "$s.Save()"],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            Item("Start with Windows", self.toggle_autostart,
                 checked=lambda i: self.is_autostart()),
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
    _mutex = acquire_single_instance()
    if _mutex is None:
        sys.exit(0)            # another instance is already running
    try:
        TrayApp().run()
    except Exception:
        import traceback
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  FATAL:\n"
                        f"{traceback.format_exc()}\n")
        except OSError:
            pass
        sys.exit(1)            # non-zero -> the watchdog relaunches us
