"""macOS audio activity for the tray app (CoreAudio through ctypes).

macOS has no simple per-device peak meter, so this reports whether any process
is doing output on the headphones (kAudioDevicePropertyDeviceIsRunningSomewhere).
Most players close their stream a short time after pause, so "not running" is
a good sign of silence. Limit: an app that holds the stream open while silent
counts as playing.

read_level() contract (same as audio_windows.py):
  -1.0 = device not present (off / disconnected), 0.0 = silent, 1.0 = playing.
"""
import ctypes
from contextlib import nullcontext

_CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


K_AUDIO_OBJECT_SYSTEM_OBJECT = 1
K_HARDWARE_DEVICES = _fourcc("dev#")
K_DEVICE_NAME = _fourcc("lnam")                 # kAudioObjectPropertyName
K_DEVICE_STREAMS = _fourcc("stm#")
K_DEVICE_RUNNING_SOMEWHERE = _fourcc("gone")
K_SCOPE_GLOBAL = _fourcc("glob")
K_SCOPE_OUTPUT = _fourcc("outp")
K_ELEMENT_MAIN = 0
K_CF_STRING_ENCODING_UTF8 = 0x08000100

LEVEL_ABSENT = -1.0
LEVEL_SILENT = 0.0
LEVEL_PLAYING = 1.0


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


_CA.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
_CA.AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32)]
_CA.AudioObjectGetPropertyData.restype = ctypes.c_int32
_CA.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_CF.CFStringGetCString.restype = ctypes.c_bool
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_CF.CFRelease.restype = None
_CF.CFRelease.argtypes = [ctypes.c_void_p]


class CoreAudioError(OSError):
    pass


def _check(status: int, what: str) -> None:
    if status != 0:
        raise CoreAudioError(f"CoreAudio {what} failed: OSStatus {status}")


def _data_size(obj: int, selector: int, scope: int) -> int:
    addr = _PropertyAddress(selector, scope, K_ELEMENT_MAIN)
    size = ctypes.c_uint32(0)
    _check(_CA.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None,
                                              ctypes.byref(size)), "GetPropertyDataSize")
    return size.value


def _get(obj: int, selector: int, scope: int, out) -> None:
    addr = _PropertyAddress(selector, scope, K_ELEMENT_MAIN)
    size = ctypes.c_uint32(ctypes.sizeof(out))
    _check(_CA.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None,
                                          ctypes.byref(size), ctypes.byref(out)),
           "GetPropertyData")


def device_ids() -> list[int]:
    size = _data_size(K_AUDIO_OBJECT_SYSTEM_OBJECT, K_HARDWARE_DEVICES, K_SCOPE_GLOBAL)
    ids = (ctypes.c_uint32 * (size // ctypes.sizeof(ctypes.c_uint32)))()
    _get(K_AUDIO_OBJECT_SYSTEM_OBJECT, K_HARDWARE_DEVICES, K_SCOPE_GLOBAL, ids)
    return list(ids)


def device_name(dev: int) -> str:
    cfstr = ctypes.c_void_p()
    _get(dev, K_DEVICE_NAME, K_SCOPE_GLOBAL, cfstr)
    if not cfstr.value:
        return ""
    try:
        buf = ctypes.create_string_buffer(1024)
        if not _CF.CFStringGetCString(cfstr, buf, len(buf), K_CF_STRING_ENCODING_UTF8):
            return ""
        return buf.value.decode("utf-8")
    finally:
        _CF.CFRelease(cfstr)


def has_output(dev: int) -> bool:
    return _data_size(dev, K_DEVICE_STREAMS, K_SCOPE_OUTPUT) > 0


def is_running_somewhere(dev: int) -> bool:
    running = ctypes.c_uint32(0)
    _get(dev, K_DEVICE_RUNNING_SOMEWHERE, K_SCOPE_GLOBAL, running)
    return running.value != 0


def find_output_device(match: str) -> int | None:
    want = match.lower()
    for dev in device_ids():
        if want in device_name(dev).lower() and has_output(dev):
            return dev
    return None


class AudioMeter:
    """Reports the headphones' output activity. Re-enumerates every read: device
    IDs change on reconnect, and the list is short."""

    def __init__(self, device_match: str):
        self.device_match = device_match

    def thread_context(self):
        return nullcontext()

    def reset(self) -> None:
        pass                       # nothing is cached

    def read_level(self) -> float:
        try:
            dev = find_output_device(self.device_match)
            if dev is None:
                return LEVEL_ABSENT
            return LEVEL_PLAYING if is_running_somewhere(dev) else LEVEL_SILENT
        except CoreAudioError:
            # A device can vanish between enumeration and query (disconnect).
            return LEVEL_ABSENT
