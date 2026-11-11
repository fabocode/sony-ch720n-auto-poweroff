#!/usr/bin/env python3
"""Send Sony's app-protocol "power off" command to a WH-CH720N over RFCOMM.

Works on Windows (WinRT, bt_windows.py) and macOS (IOBluetooth, bt_macos.py).
The protocol itself lives in sony_protocol.py.

Exit codes: 0 = power-off sent/acked (or dry-run handshake ok); non-zero = failure.
  1 = unexpected error, 3 = Sony service not found (headphones not connected),
  4 = RFCOMM connect failed, 5 = handshake incomplete.
No admin required.
  Windows: needs "Let apps access Bluetooth" privacy toggle ON.
  macOS:   needs Bluetooth permission for the app that runs it (e.g. Terminal).
"""
import argparse
import asyncio
import sys

from sony_protocol import RC_ERROR, Handshake, TransportError, run_session

VERBOSE = False


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def vlog(*a):
    if VERBOSE:
        log(*a)


def load_transport():
    if sys.platform == "win32":
        import bt_windows
        return bt_windows
    if sys.platform == "darwin":
        import bt_macos
        return bt_macos
    raise SystemExit(f"Unsupported platform: {sys.platform}")


async def run(args, bt) -> int:
    prefer = args.version if args.version in ("v1", "v2") else "v2"
    try:
        link = await bt.connect(prefer, args.device, log)
    except TransportError as e:
        log(str(e))
        return e.rc
    ver = args.version if args.version in ("v1", "v2") else link.version
    log(f"Service: {link.name}  proto~{ver}  host={link.host}")
    try:
        hs = Handshake(ver, dry_run=args.dry_run, log=log, vlog=vlog)
        return await run_session(link.transport, hs, args.timeout, vlog=vlog)
    finally:
        close = getattr(link.transport, "close", None)
        if close is not None:
            close()


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="Power off Sony WH-CH720N over RFCOMM.")
    ap.add_argument("--version", choices=["auto", "v1", "v2"], default="auto",
                    help="Force protocol version (default: auto-detect, prefer v2).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do the INIT handshake but DO NOT send power-off (safe test).")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="Overall handshake/command timeout in seconds (default 10).")
    ap.add_argument("--device", default="CH720N",
                    help="macOS only: part of the paired device name, or its address "
                         "(default: CH720N). Windows finds the Sony service by UUID.")
    ap.add_argument("--verbose", action="store_true", help="Log every TX/RX frame.")
    args = ap.parse_args()
    VERBOSE = args.verbose

    bt = load_transport()
    try:
        with bt.runtime():
            rc = asyncio.run(run(args, bt))
    except Exception as e:
        log(f"ERROR: {e!r}")
        rc = RC_ERROR
    sys.exit(rc)


if __name__ == "__main__":
    main()
