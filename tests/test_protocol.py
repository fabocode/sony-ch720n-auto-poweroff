import pytest

from sony_protocol import (ACK, COMMAND_1, ESCAPE, HEADER, PAYLOAD_INIT, TRAILER,
                           FrameBuffer, build, escape, parse, unescape)


def test_init_frame_matches_known_bytes():
    assert build(COMMAND_1, 0, PAYLOAD_INIT) == bytes.fromhex("3e 0c 00 00 00 00 02 00 00 0e 3c")


def test_escape_replaces_all_special_bytes():
    assert escape(bytes([HEADER, TRAILER, ESCAPE, 0x41])) == bytes([0x3D, 0x2E, 0x3D, 0x2C, 0x3D, 0x2D, 0x41])


def test_unescape_reverses_escape_for_every_byte_value():
    data = bytes(range(256))
    assert unescape(escape(data)) == data


def test_parse_reads_back_a_built_frame():
    frame = build(COMMAND_1, 1, bytes([0x24, 0x03, 0x01]))
    assert parse(unescape(frame[1:-1])) == (COMMAND_1, 1, bytes([0x24, 0x03, 0x01]))


def test_parse_rejects_bad_checksum():
    inner = bytearray(unescape(build(ACK, 0, b"")[1:-1]))
    inner[-1] ^= 0xFF
    with pytest.raises(ValueError, match="bad checksum"):
        parse(bytes(inner))


def test_frame_buffer_joins_a_frame_split_across_reads():
    frame = build(COMMAND_1, 0, bytes([0x01, 0x02]))
    fb = FrameBuffer()
    fb.feed(frame[:4])
    assert fb.next_frame() is None
    fb.feed(frame[4:])
    assert fb.next_frame() == (COMMAND_1, 0, bytes([0x01, 0x02]))


def test_frame_buffer_drops_garbage_before_header():
    fb = FrameBuffer()
    fb.feed(b"\x00\x11" + build(ACK, 1, b""))
    assert fb.next_frame() == (ACK, 1, b"")


def test_frame_buffer_skips_a_corrupt_frame_and_returns_the_next():
    good = build(ACK, 1, b"")
    corrupt = bytearray(build(ACK, 0, b""))
    corrupt[-2] ^= 0x01                       # damage the checksum
    fb = FrameBuffer()
    fb.feed(bytes(corrupt) + good)
    assert fb.next_frame() == (ACK, 1, b"")


def test_frame_buffer_gives_two_frames_from_one_read():
    fb = FrameBuffer()
    fb.feed(build(ACK, 1, b"") + build(COMMAND_1, 0, b"\x01"))
    assert [fb.next_frame(), fb.next_frame(), fb.next_frame()] == [
        (ACK, 1, b""), (COMMAND_1, 0, b"\x01"), None]
