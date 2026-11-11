import asyncio

from sony_protocol import (ACK, COMMAND_1, PAYLOAD_POWEROFF_V1, PAYLOAD_POWEROFF_V2,
                           RC_HANDSHAKE_INCOMPLETE, RC_OK, Handshake, build, run_session)

INIT_REPLY_V2 = bytes([0x01] + [0x00] * 7)       # 8 bytes -> protocol v2
INIT_REPLY_V1 = bytes([0x01, 0x00, 0x00, 0x00])  # 4 bytes -> protocol v1


def tags(sends):
    return [tag for tag, _ in sends]


def test_v2_reply_and_ack_send_ack_then_poweroff_with_device_seq():
    hs = Handshake("v2")
    assert hs.on_frame((ACK, 1, b"")) == []
    sends = hs.on_frame((COMMAND_1, 0, INIT_REPLY_V2))
    assert sends == [("ACK", build(ACK, 1, b"")),
                     ("POWEROFF/v2", build(COMMAND_1, 1, PAYLOAD_POWEROFF_V2))]


def test_v1_reply_switches_to_v1_poweroff_payload():
    hs = Handshake("v2")
    hs.on_frame((ACK, 1, b""))
    sends = hs.on_frame((COMMAND_1, 0, INIT_REPLY_V1))
    assert sends[-1] == ("POWEROFF/v1", build(COMMAND_1, 1, PAYLOAD_POWEROFF_V1))


def test_reply_before_ack_waits_for_ack_before_poweroff():
    hs = Handshake("v2")
    assert tags(hs.on_frame((COMMAND_1, 0, INIT_REPLY_V2))) == ["ACK"]
    assert tags(hs.on_frame((ACK, 1, b""))) == ["POWEROFF/v2"]


def test_ack_after_poweroff_ends_with_ok():
    hs = Handshake("v2")
    hs.on_frame((ACK, 1, b""))
    hs.on_frame((COMMAND_1, 0, INIT_REPLY_V2))
    hs.on_frame((ACK, 0, b""))
    assert hs.result == RC_OK


def test_dry_run_ends_ok_without_poweroff():
    hs = Handshake("v2", dry_run=True)
    hs.on_frame((ACK, 1, b""))
    sends = hs.on_frame((COMMAND_1, 0, INIT_REPLY_V2))
    assert (tags(sends), hs.result, hs.sent_po) == (["ACK"], RC_OK, False)


def test_finish_without_handshake_is_incomplete():
    assert Handshake("v2").finish() == RC_HANDSHAKE_INCOMPLETE


class FakeTransport:
    """Answers each frame we send with the next scripted chunk of bytes."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self._pending = b""

    async def send(self, data):
        self.sent.append(data)
        if self.replies:
            self._pending += self.replies.pop(0)

    async def read(self, timeout):
        data, self._pending = self._pending, b""
        return data


def test_session_powers_off_and_returns_ok():
    t = FakeTransport([
        build(ACK, 1, b"") + build(COMMAND_1, 0, INIT_REPLY_V2),   # after INIT
        b"",                                                       # after our ACK
        build(ACK, 0, b""),                                        # after POWEROFF
    ])
    rc = asyncio.run(run_session(t, Handshake("v2"), timeout=5))
    assert (rc, t.sent[-1]) == (RC_OK, build(COMMAND_1, 1, PAYLOAD_POWEROFF_V2))


def test_session_without_reply_times_out_incomplete():
    rc = asyncio.run(run_session(FakeTransport([]), Handshake("v2"), timeout=0.01))
    assert rc == RC_HANDSHAKE_INCOMPLETE
