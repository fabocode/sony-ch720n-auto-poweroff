"""Sony Headphones-Connect protocol: framing, handshake, and the session loop.

Pure Python, no OS imports. The Bluetooth transports (bt_windows.py,
bt_macos.py) only move bytes; this module decides what to send.

The protocol (framing, checksum, escaping, INIT handshake, power-off opcode)
comes from the open-source Gadgetbridge project:
https://codeberg.org/Freeyourgadget/Gadgetbridge  (sony/headphones/protocol/*).

Frame:  0x3E | type | seq | length(4B big-endian) | payload | checksum | 0x3C
  - length   = number of payload bytes only
  - checksum = (sum of type,seq,length,payload) & 0xFF
  - escaping = 0x3E/0x3C/0x3D between header and trailer (incl. checksum) are
               emitted as 0x3D followed by (byte & 0xEF). Header/trailer raw.
  - seq      = single bit (0/1); adopted from the device's ACKs. After INIT the
               device ACKs and seq becomes 1, used for the first real command.

Power-off payload:  V2 = 24 03 01   (POWER_SET) ;  V1 = 22 00 01  (POWER_OFF)
"""
import asyncio
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

# Sony custom RFCOMM service UUIDs (SonyHeadphonesSupport.java)
V2_UUID = uuid.UUID("956C7B26-D49A-4BA8-B03F-B17D393CB6E2")
V1_UUID = uuid.UUID("96CC203E-5068-46AD-B32D-E316F5E069BA")

HEADER = 0x3E
TRAILER = 0x3C
ESCAPE = 0x3D
ESCAPE_MASK = 0xEF          # masked byte = b & 0xEF ; unescape = b | 0x10

ACK = 0x01                  # MessageType.ACK
COMMAND_1 = 0x0C            # MessageType.COMMAND_1

PAYLOAD_INIT = bytes([0x00, 0x00])           # INIT_REQUEST opcode 0x00 + 0x00
PAYLOAD_POWEROFF_V2 = bytes([0x24, 0x03, 0x01])
PAYLOAD_POWEROFF_V1 = bytes([0x22, 0x00, 0x01])

# Exit codes shared by the CLI and the transports.
RC_OK = 0
RC_ERROR = 1
RC_NO_SERVICE = 3
RC_CONNECT_FAILED = 4
RC_HANDSHAKE_INCOMPLETE = 5

FRAME_WAIT_SECONDS = 1.5

LogFn = Callable[[str], None]
Frame = tuple[int, int, bytes]          # (type, seq, payload)


def _no_log(_msg: str) -> None:
    pass


class TransportError(Exception):
    """A transport could not reach the headphones. `rc` is the CLI exit code."""

    def __init__(self, rc: int, message: str):
        super().__init__(message)
        self.rc = rc


class Transport(Protocol):
    """An open RFCOMM link to the headphones."""

    async def send(self, data: bytes) -> None: ...

    async def read(self, timeout: float) -> bytes:
        """Return the bytes that came in, or b"" when nothing came in `timeout` s."""
        ...


@dataclass
class Link:
    """What a transport's connect() gives back."""
    transport: Transport
    version: str        # "v1"/"v2", from which Sony service UUID was found
    name: str           # device / service name, for the log
    host: str           # Bluetooth host name or address, for the log


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (HEADER, TRAILER, ESCAPE):
            out.append(ESCAPE)
            out.append(b & ESCAPE_MASK)
        else:
            out.append(b)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == ESCAPE:
            i += 1
            out.append(data[i] | (~ESCAPE_MASK & 0xFF))   # | 0x10
        else:
            out.append(b)
        i += 1
    return bytes(out)


def build(type_byte: int, seq: int, payload: bytes) -> bytes:
    body = bytes([type_byte & 0xFF, seq & 0xFF]) + len(payload).to_bytes(4, "big") + payload
    chk = sum(body) & 0xFF
    return bytes([HEADER]) + escape(body) + escape(bytes([chk])) + bytes([TRAILER])


def parse(inner: bytes) -> Frame:
    """inner = unescaped bytes between header and trailer (incl. trailing checksum)."""
    body, chk = inner[:-1], inner[-1]
    want = sum(body) & 0xFF
    if want != chk:
        raise ValueError(f"bad checksum got={chk:#04x} want={want:#04x}")
    type_byte = body[0]
    seq = body[1]
    length = int.from_bytes(body[2:6], "big")
    payload = body[6:6 + length]
    return type_byte, seq, payload


class FrameBuffer:
    """Collects received bytes and gives back complete 0x3E..0x3C frames (parsed)."""

    def __init__(self, vlog: LogFn = _no_log):
        self.buf = bytearray()
        self._vlog = vlog

    def feed(self, data: bytes) -> None:
        self.buf.extend(data)

    def next_frame(self) -> Frame | None:
        while True:
            start = self.buf.find(HEADER)
            if start < 0:
                self.buf.clear()
                return None
            end = self.buf.find(TRAILER, start + 1)
            if end < 0:
                if start > 0:
                    del self.buf[:start]
                return None
            inner_escaped = bytes(self.buf[start + 1:end])
            del self.buf[:end + 1]
            try:
                return parse(unescape(inner_escaped))
            except Exception as e:      # malformed frame: skip it, keep reading
                self._vlog(f"parse skip: {e}")


class Handshake:
    """INIT handshake, then power-off. Feed it frames; it says what to send.

    `on_frame` returns a list of (tag, frame bytes) to send, in order. When the
    session is over, `result` holds the exit code.
    """

    def __init__(self, version: str, dry_run: bool = False,
                 log: LogFn = _no_log, vlog: LogFn = _no_log):
        self.detected = version             # "v1" or "v2"; the INIT reply can change it
        self.dry_run = dry_run
        self.seq = 0
        self.init_replied = False
        self.init_acked = False
        self.sent_po = False
        self.result: int | None = None
        self._log = log
        self._vlog = vlog

    def start(self) -> tuple[str, bytes]:
        return "INIT", build(COMMAND_1, self.seq, PAYLOAD_INIT)

    def on_frame(self, frame: Frame) -> list[tuple[str, bytes]]:
        type_, fseq, payload = frame
        out: list[tuple[str, bytes]] = []

        if type_ == ACK:
            self.seq = fseq                       # adopt device's toggled seq
            if self.sent_po:
                self._log("Power-off ACKed by device — it will power down now.")
                self.result = RC_OK
                return out
            self.init_acked = True
        elif type_ == COMMAND_1:
            out.append(("ACK", build(ACK, (1 - fseq) & 0xFF, b"")))
            if payload and payload[0] == 0x01:           # INIT_REPLY
                self.init_replied = True
                if len(payload) == 8:
                    self.detected = "v2"
                elif len(payload) == 4:
                    self.detected = "v1"
                self._vlog(f"INIT reply -> protocol {self.detected}")

        if self.init_replied and self.init_acked and not self.sent_po:
            if self.dry_run:
                self._log(f"DRY-RUN OK: handshake complete, protocol={self.detected}, "
                          f"next seq={self.seq}. NOT sending power-off.")
                self.result = RC_OK
                return out
            po = PAYLOAD_POWEROFF_V2 if self.detected == "v2" else PAYLOAD_POWEROFF_V1
            out.append((f"POWEROFF/{self.detected}", build(COMMAND_1, self.seq, po)))
            self.sent_po = True
            self._log(f"Power-off sent (protocol={self.detected}, seq={self.seq}). "
                      "Awaiting ACK...")
        return out

    def finish(self) -> int:
        """Exit code when the session ends without a final result."""
        if self.sent_po:
            self._log("Power-off sent; no final ACK seen (device likely powered off already).")
            return RC_OK
        self._log(f"ERROR: handshake incomplete (init_replied={self.init_replied} "
                  f"init_acked={self.init_acked}). Try --verbose to inspect frames.")
        return RC_HANDSHAKE_INCOMPLETE


async def _next_frame(transport: Transport, frames: FrameBuffer, timeout: float) -> Frame | None:
    while True:
        frame = frames.next_frame()
        if frame is not None:
            return frame
        data = await transport.read(timeout)
        if not data:
            return None
        frames.feed(data)


async def run_session(transport: Transport, hs: Handshake, timeout: float,
                      vlog: LogFn = _no_log) -> int:
    """Drive `hs` over `transport` until it has a result or `timeout` s pass."""
    frames = FrameBuffer(vlog)

    async def send(tag: str, frame: bytes) -> None:
        vlog(f"TX {tag}: {frame.hex(' ')}")
        await transport.send(frame)

    await send(*hs.start())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        frame = await _next_frame(transport, frames, FRAME_WAIT_SECONDS)
        if frame is None:
            if hs.sent_po:
                break
            continue
        type_, fseq, payload = frame
        vlog(f"RX: type={type_:#04x} seq={fseq} payload=[{payload.hex(' ')}]")
        for tag, data in hs.on_frame(frame):
            await send(tag, data)
        if hs.result is not None:
            return hs.result
    return hs.finish()
