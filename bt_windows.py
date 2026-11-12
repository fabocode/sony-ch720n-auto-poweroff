"""Windows RFCOMM transport for sony_poweroff.py (WinRT). No admin required.

Needs Windows "Let apps access Bluetooth" privacy toggle ON.
"""
import asyncio
from contextlib import contextmanager

from winrt.runtime import init_apartment, uninit_apartment, MTA
from winrt.windows.devices.enumeration import DeviceInformation
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId, RfcommDeviceService
from winrt.windows.networking.sockets import StreamSocket
from winrt.windows.storage.streams import DataReader, DataWriter, InputStreamOptions

from sony_protocol import (V1_UUID, V2_UUID, RC_CONNECT_FAILED, RC_NO_SERVICE,
                           Link, LogFn, TransportError)

CONNECT_TIMEOUT_SECONDS = 10
SETTLE_SECONDS = 0.5     # SonyHeadphonesSupport: "connecting too fast fails"


@contextmanager
def runtime():
    init_apartment(MTA)
    try:
        yield
    finally:
        uninit_apartment()


class WinRfcommTransport:
    def __init__(self, sock: StreamSocket):
        self._writer = DataWriter(sock.output_stream)
        self._reader = DataReader(sock.input_stream)
        self._reader.input_stream_options = InputStreamOptions.PARTIAL

    async def send(self, data: bytes) -> None:
        self._writer.write_bytes(bytes(data))
        await self._writer.store_async()

    async def read(self, timeout: float) -> bytes:
        try:
            await asyncio.wait_for(self._reader.load_async(1024), timeout)
        except asyncio.TimeoutError:
            return b""
        avail = self._reader.unconsumed_buffer_length
        return bytes(self._reader.read_byte() for _ in range(avail))


async def _find_service(prefer: str):
    order = [(V2_UUID, "v2"), (V1_UUID, "v1")]
    if prefer == "v1":
        order.reverse()
    for u, ver in order:
        sid = RfcommServiceId.from_uuid(u)
        sel = RfcommDeviceService.get_device_selector(sid)
        for di in list(await DeviceInformation.find_all_async_aqs_filter(sel)):
            svc = await RfcommDeviceService.from_id_async(di.id)
            if svc is not None:
                return svc, ver, di
    return None, None, None


async def connect(prefer: str, device_match: str, log: LogFn) -> Link:
    """Open the Sony RFCOMM service. `device_match` is not used on Windows:
    the service UUID alone finds the headphones."""
    svc, ver, di = await _find_service(prefer)
    if svc is None:
        raise TransportError(RC_NO_SERVICE,
                             "ERROR: no Sony RFCOMM service found. Headphones connected? "
                             "Windows Bluetooth app-access privacy ON?")
    host = svc.connection_host_name
    sock = StreamSocket()
    try:
        await asyncio.wait_for(
            sock.connect_async(host, svc.connection_service_name), CONNECT_TIMEOUT_SECONDS)
    except Exception as e:
        raise TransportError(RC_CONNECT_FAILED, f"ERROR: RFCOMM connect failed: {e!r}") from e

    await asyncio.sleep(SETTLE_SECONDS)
    # The socket is not closed explicitly (same as before the split): the
    # process exits right after the session and that releases it.
    return Link(WinRfcommTransport(sock), ver, di.name, host.display_name)
