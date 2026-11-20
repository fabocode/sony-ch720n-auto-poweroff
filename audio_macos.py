"""macOS audio activity for the tray app (CoreAudio through ctypes + PyObjC).

Two steps:
1. Is any other process doing output on the headphones? (CoreAudio process
   objects, macOS 14+; falls back to kAudioDevicePropertyDeviceIsRunningSomewhere.)
   If not, the headphones are silent.
2. If one is, measure the real level with a Core Audio process tap on the
   headphones' output (macOS 14.2+). Apps such as Chrome keep their output
   stream open while silent, so "a stream is open" alone is not "playing".

The tap needs the "System Audio Recording" permission for the Python binary
that runs the tray. Without it macOS may deliver silence instead of an error,
so the tap is trusted only after it has delivered real (non-zero) audio once in
this session. Until then an open stream counts as playing, as before. Only a
peak value is kept; samples are never stored.

read_level() contract (same as audio_windows.py):
  -1.0 = device not present (off / disconnected), otherwise the peak level
  (0.0 = silent). Without a trusted tap, an open stream reads as 1.0.
"""
import ctypes
import os
import uuid
from contextlib import nullcontext
from typing import Callable

_CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


K_AUDIO_OBJECT_SYSTEM_OBJECT = 1
K_HARDWARE_DEVICES = _fourcc("dev#")
K_HARDWARE_PROCESSES = _fourcc("prs#")         # kAudioHardwarePropertyProcessObjectList
K_PROCESS_PID = _fourcc("ppid")
K_PROCESS_RUNNING_OUTPUT = _fourcc("piro")
K_PROCESS_DEVICES = _fourcc("pdv#")
K_DEVICE_NAME = _fourcc("lnam")                 # kAudioObjectPropertyName
K_DEVICE_UID = _fourcc("uid ")
K_DEVICE_STREAMS = _fourcc("stm#")
K_DEVICE_RUNNING_SOMEWHERE = _fourcc("gone")
K_TAP_FORMAT = _fourcc("tfmt")
K_SCOPE_GLOBAL = _fourcc("glob")
K_SCOPE_OUTPUT = _fourcc("outp")
K_ELEMENT_MAIN = 0
K_CF_STRING_ENCODING_UTF8 = 0x08000100
K_FORMAT_LINEAR_PCM = _fourcc("lpcm")
K_FORMAT_FLAG_IS_FLOAT = 0x1
TAP_UNMUTED = 0                                 # CATapMuteBehavior: the app stays audible

LEVEL_ABSENT = -1.0
LEVEL_SILENT = 0.0
LEVEL_PLAYING = 1.0

# Log once if a trusted tap never arrives after this many reads with a stream
# open (about 10 min at the default 2 s poll).
UNPROVEN_HINT_READS = 300

LogFn = Callable[[str], None]


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


class _StreamFormat(ctypes.Structure):          # AudioStreamBasicDescription
    _fields_ = [("mSampleRate", ctypes.c_double),
                ("mFormatID", ctypes.c_uint32),
                ("mFormatFlags", ctypes.c_uint32),
                ("mBytesPerPacket", ctypes.c_uint32),
                ("mFramesPerPacket", ctypes.c_uint32),
                ("mBytesPerFrame", ctypes.c_uint32),
                ("mChannelsPerFrame", ctypes.c_uint32),
                ("mBitsPerChannel", ctypes.c_uint32),
                ("mReserved", ctypes.c_uint32)]


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


_MAX_BUFFERS = 16


class _AudioBufferList(ctypes.Structure):
    _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                ("mBuffers", _AudioBuffer * _MAX_BUFFERS)]


_IOProc = ctypes.CFUNCTYPE(ctypes.c_int32,                     # OSStatus
                           ctypes.c_uint32,                    # device
                           ctypes.c_void_p,                    # inNow
                           ctypes.POINTER(_AudioBufferList),   # inInputData
                           ctypes.c_void_p,                    # inInputTime
                           ctypes.c_void_p,                    # outOutputData
                           ctypes.c_void_p,                    # inOutputTime
                           ctypes.c_void_p)                    # inClientData

_CA.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
_CA.AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32)]
_CA.AudioObjectGetPropertyData.restype = ctypes.c_int32
_CA.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_CA.AudioHardwareCreateProcessTap.restype = ctypes.c_int32
_CA.AudioHardwareCreateProcessTap.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_CA.AudioHardwareDestroyProcessTap.restype = ctypes.c_int32
_CA.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
_CA.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
_CA.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_uint32)]
_CA.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32
_CA.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
_CA.AudioDeviceCreateIOProcID.restype = ctypes.c_int32
_CA.AudioDeviceCreateIOProcID.argtypes = [ctypes.c_uint32, _IOProc, ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p)]
_CA.AudioDeviceDestroyIOProcID.restype = ctypes.c_int32
_CA.AudioDeviceDestroyIOProcID.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_CA.AudioDeviceStart.restype = ctypes.c_int32
_CA.AudioDeviceStart.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_CA.AudioDeviceStop.restype = ctypes.c_int32
_CA.AudioDeviceStop.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
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


def _get_ids(obj: int, selector: int, scope: int) -> list[int]:
    size = _data_size(obj, selector, scope)
    count = size // ctypes.sizeof(ctypes.c_uint32)
    if count == 0:
        return []
    ids = (ctypes.c_uint32 * count)()
    _get(obj, selector, scope, ids)
    return list(ids)


def _get_string(obj: int, selector: int) -> str:
    cfstr = ctypes.c_void_p()
    _get(obj, selector, K_SCOPE_GLOBAL, cfstr)
    if not cfstr.value:
        return ""
    try:
        buf = ctypes.create_string_buffer(1024)
        if not _CF.CFStringGetCString(cfstr, buf, len(buf), K_CF_STRING_ENCODING_UTF8):
            return ""
        return buf.value.decode("utf-8")
    finally:
        _CF.CFRelease(cfstr)


def device_ids() -> list[int]:
    return _get_ids(K_AUDIO_OBJECT_SYSTEM_OBJECT, K_HARDWARE_DEVICES, K_SCOPE_GLOBAL)


def device_name(dev: int) -> str:
    return _get_string(dev, K_DEVICE_NAME)


def device_uid(dev: int) -> str:
    return _get_string(dev, K_DEVICE_UID)


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


def other_process_outputs_to(dev: int) -> bool:
    """True when a process other than this one is doing output on `dev`.

    Uses the CoreAudio process objects (macOS 14+), so this app's own tap is
    never counted. Falls back to "device running somewhere" on older systems.
    """
    try:
        processes = _get_ids(K_AUDIO_OBJECT_SYSTEM_OBJECT, K_HARDWARE_PROCESSES,
                             K_SCOPE_GLOBAL)
    except CoreAudioError:
        return is_running_somewhere(dev)
    me = os.getpid()
    for proc in processes:
        try:
            running = ctypes.c_uint32(0)
            _get(proc, K_PROCESS_RUNNING_OUTPUT, K_SCOPE_GLOBAL, running)
            if not running.value:
                continue
            pid = ctypes.c_int32(0)
            _get(proc, K_PROCESS_PID, K_SCOPE_GLOBAL, pid)
            if pid.value == me:
                continue
            if dev in _get_ids(proc, K_PROCESS_DEVICES, K_SCOPE_OUTPUT):
                return True
        except CoreAudioError:
            continue            # the process went away between list and query
    return False


class _LevelTap:
    """A private, unmuted process tap on everything playing to one output
    device, read through a private aggregate device. Keeps the peak level since
    the last take(); the samples themselves are dropped."""

    def __init__(self, dev: int):
        import objc
        from Foundation import NSArray, NSDictionary, NSNumber

        objc.loadBundle("CoreAudio", {},
                        bundle_path="/System/Library/Frameworks/CoreAudio.framework")
        tap_description = objc.lookUpClass("CATapDescription")

        self.dev = dev
        self._tap = None
        self._agg = None
        self._proc_id = None
        self._started = False
        self._calls = 0
        self._peak = 0.0
        self._callback = _IOProc(self._on_io)      # keep a reference while running
        try:
            desc = tap_description.alloc().initExcludingProcesses_andDeviceUID_withStream_(
                NSArray.array(), device_uid(dev), 0)
            desc.setPrivate_(True)
            desc.setMuteBehavior_(TAP_UNMUTED)
            tap = ctypes.c_uint32(0)
            _check(_CA.AudioHardwareCreateProcessTap(objc.pyobjc_id(desc), ctypes.byref(tap)),
                   "CreateProcessTap")
            self._tap = tap.value

            fmt = _StreamFormat()
            _get(self._tap, K_TAP_FORMAT, K_SCOPE_GLOBAL, fmt)
            if (fmt.mFormatID != K_FORMAT_LINEAR_PCM
                    or not fmt.mFormatFlags & K_FORMAT_FLAG_IS_FLOAT
                    or fmt.mBitsPerChannel != 32):
                raise CoreAudioError(f"unexpected tap format {fmt.mFormatID:#x} "
                                     f"flags {fmt.mFormatFlags:#x} bits {fmt.mBitsPerChannel}")

            yes, no = NSNumber.numberWithBool_(True), NSNumber.numberWithBool_(False)
            agg_desc = {
                "name": "Sony CH720N Auto-Off level tap",
                "uid": f"sony-ch720n-tap-{uuid.uuid4()}",
                "private": yes,             # hidden from Sound settings, gone at exit
                "stacked": no,
                "tapautostart": yes,
                "taps": [{"uid": str(desc.UUID().UUIDString()), "drift": yes}],
            }
            agg_dict = NSDictionary.dictionaryWithDictionary_(agg_desc)
            agg = ctypes.c_uint32(0)
            _check(_CA.AudioHardwareCreateAggregateDevice(objc.pyobjc_id(agg_dict),
                                                          ctypes.byref(agg)),
                   "CreateAggregateDevice")
            self._agg = agg.value

            proc_id = ctypes.c_void_p()
            _check(_CA.AudioDeviceCreateIOProcID(self._agg, self._callback, None,
                                                 ctypes.byref(proc_id)), "CreateIOProcID")
            self._proc_id = proc_id
            _check(_CA.AudioDeviceStart(self._agg, self._proc_id), "DeviceStart")
            self._started = True
        except Exception:
            self.close()
            raise

    def _on_io(self, _dev, _now, input_data, _in_time, _out, _out_time, _client):
        # Runs on a CoreAudio thread: keep it short and never raise.
        try:
            self._calls += 1
            if not input_data:
                return 0
            buffers = input_data.contents
            for i in range(min(buffers.mNumberBuffers, _MAX_BUFFERS)):
                b = buffers.mBuffers[i]
                n = b.mDataByteSize // 4
                if not b.mData or n == 0:
                    continue
                samples = memoryview((ctypes.c_float * n).from_address(b.mData)).cast("B").cast("f")
                peak = max(max(samples), -min(samples))
                if peak > self._peak:
                    self._peak = peak
        except Exception:
            pass
        return 0

    def take(self) -> tuple[int, float]:
        """(IO callbacks, peak level) since the last call; then reset both."""
        calls, peak = self._calls, self._peak
        self._calls, self._peak = 0, 0.0
        return calls, peak

    def close(self) -> None:
        if self._agg is not None and self._proc_id is not None:
            if self._started:
                _CA.AudioDeviceStop(self._agg, self._proc_id)
                self._started = False
            _CA.AudioDeviceDestroyIOProcID(self._agg, self._proc_id)
            self._proc_id = None
        if self._agg is not None:
            _CA.AudioHardwareDestroyAggregateDevice(self._agg)
            self._agg = None
        if self._tap is not None:
            _CA.AudioHardwareDestroyProcessTap(self._tap)
            self._tap = None


def _no_log(_msg: str) -> None:
    pass


class AudioMeter:
    """Reports the headphones' output level. Re-enumerates every read: device
    IDs change on reconnect, and the list is short."""

    def __init__(self, device_match: str, log: LogFn = _no_log):
        self.device_match = device_match
        self._log = log
        self._tap: _LevelTap | None = None
        self._tap_failed = False     # tap creation failed: use the open-stream check
        self._tap_trusted = False    # the tap delivered real audio once
        self._unproven_reads = 0

    def thread_context(self):
        return nullcontext()

    def reset(self) -> None:
        self._close_tap()

    def _close_tap(self) -> None:
        if self._tap is not None:
            tap, self._tap = self._tap, None
            tap.close()

    def _tap_level(self, dev: int) -> float:
        if self._tap_failed:
            return LEVEL_PLAYING
        if self._tap is None or self._tap.dev != dev:
            self._close_tap()
            try:
                self._tap = _LevelTap(dev)
            except Exception as e:
                self._tap_failed = True
                self._log(f"audio level tap unavailable ({e}); an open audio stream "
                          "counts as playing")
            return LEVEL_PLAYING     # no data yet
        calls, peak = self._tap.take()
        if peak > 0.0 and not self._tap_trusted:
            self._tap_trusted = True
            self._log("audio level tap active: silence is measured, not guessed")
        if not self._tap_trusted:
            self._unproven_reads += 1
            if self._unproven_reads == UNPROVEN_HINT_READS:
                self._log("audio level tap has only seen silence so far. If audio was "
                          "playing, allow Python under System Settings > Privacy & "
                          "Security > System Audio Recording and restart the app")
            return LEVEL_PLAYING
        if calls == 0:
            return LEVEL_PLAYING     # the tap stalled: do not guess silence
        return peak

    def read_level(self) -> float:
        try:
            dev = find_output_device(self.device_match)
            if dev is None:
                self._close_tap()
                return LEVEL_ABSENT
            if not other_process_outputs_to(dev):
                self._close_tap()
                return LEVEL_SILENT
            return self._tap_level(dev)
        except CoreAudioError:
            # A device can vanish between enumeration and query (disconnect).
            self._close_tap()
            return LEVEL_ABSENT
