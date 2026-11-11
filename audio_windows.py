"""Windows audio peak for the tray app (pycaw / Core Audio).

read_level() contract (same as audio_macos.py):
  -1.0 = device not active (off / disconnected), else the peak in [0..1].
"""
from contextlib import contextmanager

import comtypes
from comtypes import CLSCTX_ALL, cast, POINTER
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation


class AudioMeter:
    def __init__(self, device_match: str):
        self.device_match = device_match
        self._meter = None             # cached IAudioMeterInformation (re-resolved on loss)

    @contextmanager
    def thread_context(self):
        # MTA (multithreaded apartment): the monitor thread only polls COM and
        # never pumps a Windows message loop, so STA would be incorrect and caused
        # intermittent access-violation crashes in _ctypes. MMDevice objects are
        # apartment-agnostic ("Both"), so MTA is safe here.
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            yield
        finally:
            self._meter = None
            comtypes.CoUninitialize()

    def reset(self) -> None:
        self._meter = None

    def _resolve_meter(self):
        """Find the matching render endpoint and cache its meter interface.
        Sets self._meter to None if the device is not currently active."""
        self._meter = None
        enum = AudioUtilities.GetDeviceEnumerator()
        coll = enum.EnumAudioEndpoints(0, 0x1)  # eRender, DEVICE_STATE_ACTIVE
        match = self.device_match.lower()
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

    def read_level(self) -> float:
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
