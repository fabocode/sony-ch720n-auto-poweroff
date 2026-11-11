"""macOS RFCOMM transport for sony_poweroff.py (IOBluetooth through PyObjC).

It opens an RFCOMM channel to the Sony service on the paired, connected
headphones. The audio link (A2DP) stays up; only the control channel is used.

IOBluetooth delivers its callbacks on the run loop of the thread that opened
the channel, so every wait here pumps the current NSRunLoop.

macOS asks for Bluetooth permission for the app that runs this (e.g. Terminal).
"""
import ctypes
import time
from contextlib import nullcontext

import objc
from Foundation import NSDate, NSDefaultRunLoopMode, NSObject, NSRunLoop
import IOBluetooth

from sony_protocol import (V1_UUID, V2_UUID, RC_CONNECT_FAILED, RC_NO_SERVICE,
                           Link, LogFn, TransportError)

SDP_TIMEOUT_SECONDS = 10
SETTLE_SECONDS = 0.5     # SonyHeadphonesSupport: "connecting too fast fails"
PUMP_STEP_SECONDS = 0.05
K_IO_RETURN_SUCCESS = 0


def runtime():
    return nullcontext()


def _pump_until(done, timeout: float) -> None:
    """Run the current run loop until done() is true or `timeout` s pass."""
    loop = NSRunLoop.currentRunLoop()
    deadline = time.monotonic() + timeout
    while not done():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        step = min(PUMP_STEP_SECONDS, remaining)
        if not loop.runMode_beforeDate_(NSDefaultRunLoopMode,
                                        NSDate.dateWithTimeIntervalSinceNow_(step)):
            time.sleep(step)        # no run loop sources yet: do not spin


class _ChannelDelegate(NSObject,
                       protocols=[objc.protocolNamed("IOBluetoothRFCOMMChannelDelegate")]):
    """Collects bytes from the RFCOMM channel."""

    def init(self):
        self = objc.super(_ChannelDelegate, self).init()
        if self is None:
            return None
        self.received = bytearray()
        self.closed = False
        return self

    def rfcommChannelData_data_length_(self, channel, data, length):
        # PyObjC passes the C buffer as a raw address (int).
        self.received.extend(ctypes.string_at(data, length))

    def rfcommChannelClosed_(self, channel):
        self.closed = True


class _SdpDelegate(NSObject):
    """Target for performSDPQuery: marks the query as done."""

    def init(self):
        self = objc.super(_SdpDelegate, self).init()
        if self is None:
            return None
        self.done = False
        self.status = None
        return self

    @objc.typedSelector(b"v@:@i")
    def sdpQueryComplete_status_(self, device, status):
        self.status = status
        self.done = True


class MacRfcommTransport:
    def __init__(self, channel, delegate: _ChannelDelegate):
        self._channel = channel
        self._delegate = delegate        # keep a strong reference while open

    async def send(self, data: bytes) -> None:
        status = self._channel.writeSync_length_(bytes(data), len(data))
        if status != K_IO_RETURN_SUCCESS:
            raise OSError(f"RFCOMM write failed: IOReturn {status:#x}")

    async def read(self, timeout: float) -> bytes:
        d = self._delegate
        _pump_until(lambda: d.received or d.closed, timeout)
        data = bytes(d.received)
        d.received.clear()
        return data

    def close(self) -> None:
        # Close only the control channel; the audio connection stays up.
        if not self._delegate.closed:
            self._channel.closeChannel()


def _normalize_address(text: str) -> str:
    return text.strip().lower().replace(":", "-")


def find_device(device_match: str):
    """Return the paired device whose name contains `device_match` (or whose
    address equals it), or None."""
    want = device_match.lower()
    want_addr = _normalize_address(device_match)
    for dev in IOBluetooth.IOBluetoothDevice.pairedDevices() or []:
        name = (dev.name() or "").lower()
        addr = _normalize_address(dev.addressString() or "")
        if want in name or want_addr == addr:
            return dev
    return None


def _sdp_uuid(u):
    return IOBluetooth.IOBluetoothSDPUUID.uuidWithBytes_length_(u.bytes, 16)


def _refresh_sdp(dev, log: LogFn) -> None:
    """Ask the device for its service records again (when the cache lacks Sony's)."""
    target = _SdpDelegate.alloc().init()
    status = dev.performSDPQuery_(target)
    if status != K_IO_RETURN_SUCCESS:
        log(f"SDP query could not start: IOReturn {status:#x}")
        return
    _pump_until(lambda: target.done, SDP_TIMEOUT_SECONDS)
    if not target.done:
        log("SDP query timed out")


def _rfcomm_channel_id(record) -> int | None:
    status, channel_id = record.getRFCOMMChannelID_(None)
    if status != K_IO_RETURN_SUCCESS or channel_id is None:
        return None
    # PyObjC returns the out-parameter as bytes ('o*') or int, by version.
    return channel_id[0] if isinstance(channel_id, (bytes, bytearray)) else int(channel_id)


def _find_service(dev, prefer: str):
    """Return (channel_id, version) for the Sony service, or (None, None)."""
    order = [(V2_UUID, "v2"), (V1_UUID, "v1")]
    if prefer == "v1":
        order.reverse()
    for u, ver in order:
        record = dev.getServiceRecordForUUID_(_sdp_uuid(u))
        if record is not None:
            channel_id = _rfcomm_channel_id(record)
            if channel_id is not None:
                return channel_id, ver
    return None, None


async def connect(prefer: str, device_match: str, log: LogFn) -> Link:
    dev = find_device(device_match)
    if dev is None:
        raise TransportError(RC_NO_SERVICE,
                             f"ERROR: no paired Bluetooth device matches '{device_match}'. "
                             "Is it paired? Does this app have Bluetooth permission?")
    if not dev.isConnected():
        raise TransportError(RC_NO_SERVICE, f"ERROR: '{dev.name()}' is not connected.")

    channel_id, ver = _find_service(dev, prefer)
    if channel_id is None:
        _refresh_sdp(dev, log)
        channel_id, ver = _find_service(dev, prefer)
    if channel_id is None:
        raise TransportError(RC_NO_SERVICE,
                             f"ERROR: no Sony RFCOMM service found on '{dev.name()}'.")

    delegate = _ChannelDelegate.alloc().init()
    status, channel = dev.openRFCOMMChannelSync_withChannelID_delegate_(
        None, channel_id, delegate)
    if status != K_IO_RETURN_SUCCESS or channel is None:
        raise TransportError(RC_CONNECT_FAILED,
                             f"ERROR: RFCOMM connect failed: IOReturn {status:#x} "
                             f"(channel {channel_id})")

    _pump_until(lambda: delegate.closed, SETTLE_SECONDS)
    if delegate.closed:
        raise TransportError(RC_CONNECT_FAILED, "ERROR: RFCOMM channel closed right after open")
    return Link(MacRfcommTransport(channel, delegate), ver, dev.name(),
                f"{dev.addressString()} ch{channel_id}")
