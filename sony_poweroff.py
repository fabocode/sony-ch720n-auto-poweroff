#!/usr/bin/env python3
"""Send Sony's app-protocol "power off" command to a WH-CH720N over RFCOMM (Windows).

The WH-CH720N implements Sony's Headphones-Connect control protocol over a
proprietary RFCOMM/SPP service. The protocol (framing, checksum, escaping, INIT
handshake, power-off opcode) is taken from the open-source Gadgetbridge project:
https://codeberg.org/Freeyourgadget/Gadgetbridge  (sony/headphones/protocol/*).

Frame:  0x3E | type | seq | length(4B big-endian) | payload | checksum | 0x3C
  - length   = number of payload bytes only
  - checksum = (sum of type,seq,length,payload) & 0xFF
  - escaping = 0x3E/0x3C/0x3D between header and trailer (incl. checksum) are
               emitted as 0x3D followed by (byte & 0xEF). Header/trailer raw.
  - seq      = single bit (0/1); adopted from the device's ACKs. After INIT the
               device ACKs and seq becomes 1, used for the first real command.

Power-off payload:  V2 = 24 03 01   (POWER_SET) ;  V1 = 22 00 01  (POWER_OFF)

Exit codes: 0 = power-off sent/acked (or dry-run handshake ok); non-zero = failure.
No admin required. Needs Windows "Let apps access Bluetooth" privacy toggle ON.
"""
import argparse
import asyncio
import sys
import uuid

from winrt.runtime import init_apartment, uninit_apartment, MTA
from winrt.windows.devices.enumeration import DeviceInformation
from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId, RfcommDeviceService
from winrt.windows.networking.sockets import StreamSocket
from winrt.windows.storage.streams import DataReader, DataWriter, InputStreamOptions

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

VERBOSE = False


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def vlog(*a):
    if VERBOSE:
        log(*a)


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


def parse(inner: bytes):
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


class FrameReader:
    """Buffers RFCOMM reads and yields complete 0x3E..0x3C frames (parsed)."""

    def __init__(self, reader):
        self.reader = reader
        self.buf = bytearray()

    def _extract(self):
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
        except Exception as e:
            vlog("parse skip:", e)
            return self._extract()

    async def next_frame(self, timeout):
        while True:
            frame = self._extract()
            if frame is not None:
                return frame
            try:
                await asyncio.wait_for(self.reader.load_async(1024), timeout)
            except asyncio.TimeoutError:
                return None
            avail = self.reader.unconsumed_buffer_length
            if avail == 0:
                return None
            for _ in range(avail):
                self.buf.append(self.reader.read_byte())


async def find_service(prefer):
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


async def run(args):
    prefer = args.version if args.version in ("v1", "v2") else "v2"
    svc, ver, di = await find_service(prefer)
    if svc is None:
        log("ERROR: no Sony RFCOMM service found. Headphones connected? "
            "Windows Bluetooth app-access privacy ON?")
        return 3
    if args.version in ("v1", "v2"):
        ver = args.version
    host = svc.connection_host_name
    log(f"Service: {di.name}  proto~{ver}  host={host.display_name}")

    sock = StreamSocket()
    try:
        await asyncio.wait_for(
            sock.connect_async(host, svc.connection_service_name), 10)
    except Exception as e:
        log(f"ERROR: RFCOMM connect failed: {e!r}")
        return 4

    await asyncio.sleep(0.5)   # SonyHeadphonesSupport: "connecting too fast fails"
    writer = DataWriter(sock.output_stream)
    reader = DataReader(sock.input_stream)
    reader.input_stream_options = InputStreamOptions.PARTIAL
    fr = FrameReader(reader)

    async def send(frame, tag):
        vlog(f"TX {tag}: {frame.hex(' ')}")
        writer.write_bytes(bytes(frame))
        await writer.store_async()

    seq = 0
    await send(build(COMMAND_1, seq, PAYLOAD_INIT), "INIT")

    init_replied = init_acked = sent_po = False
    detected = ver
    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.timeout

    while loop.time() < deadline:
        frame = await fr.next_frame(timeout=1.5)
        if frame is None:
            if sent_po:
                break
            continue
        type_, fseq, payload = frame
        vlog(f"RX: type={type_:#04x} seq={fseq} payload=[{payload.hex(' ')}]")

        if type_ == ACK:
            seq = fseq                       # adopt device's toggled seq
            if sent_po:
                log("Power-off ACKed by device — it will power down now.")
                return 0
            init_acked = True
        elif type_ == COMMAND_1:
            await send(build(ACK, (1 - fseq) & 0xFF, b""), "ACK")
            if payload and payload[0] == 0x01:           # INIT_REPLY
                init_replied = True
                if len(payload) == 8:
                    detected = "v2"
                elif len(payload) == 4:
                    detected = "v1"
                vlog(f"INIT reply -> protocol {detected}")

        if init_replied and init_acked and not sent_po:
            if args.dry_run:
                log(f"DRY-RUN OK: handshake complete, protocol={detected}, "
                    f"next seq={seq}. NOT sending power-off.")
                return 0
            po = PAYLOAD_POWEROFF_V2 if detected == "v2" else PAYLOAD_POWEROFF_V1
            await send(build(COMMAND_1, seq, po), f"POWEROFF/{detected}")
            sent_po = True
            log(f"Power-off sent (protocol={detected}, seq={seq}). Awaiting ACK...")

    if sent_po:
        log("Power-off sent; no final ACK seen (device likely powered off already).")
        return 0
    log(f"ERROR: handshake incomplete (init_replied={init_replied} "
        f"init_acked={init_acked}). Try --verbose to inspect frames.")
    return 5


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="Power off Sony WH-CH720N over RFCOMM.")
    ap.add_argument("--version", choices=["auto", "v1", "v2"], default="auto",
                    help="Force protocol version (default: auto-detect, prefer v2).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do the INIT handshake but DO NOT send power-off (safe test).")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="Overall handshake/command timeout in seconds (default 10).")
    ap.add_argument("--verbose", action="store_true", help="Log every TX/RX frame.")
    args = ap.parse_args()
    VERBOSE = args.verbose

    init_apartment(MTA)
    try:
        rc = asyncio.run(run(args))
    except Exception as e:
        log(f"ERROR: {e!r}")
        rc = 1
    finally:
        uninit_apartment()
    sys.exit(rc)


if __name__ == "__main__":
    main()
