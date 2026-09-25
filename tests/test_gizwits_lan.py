"""Stand-alone tests (no Home Assistant needed):  python -m unittest tests.test_lan_and_cloud -v

Exercises the CONFIRMED protocol from a real capture of the phone app switching the
thermostat on and off. Frame timing/values are real; the device's MAC, DID and
product key have been replaced with fake placeholders below.
    -> 0x93  seq + 05 00 00        "give me your status"
    <- 0x94  seq + <14-byte status, prefixed 0x06>
    -> 0x93  seq + 05 0a <00|01>   "turn off|on"
    <- 0x94  seq + 06 0a <00|01>   short ack
    <- 0x91  <14-byte status>      unsolicited push a moment later, with byte 2 changed
"""
import asyncio
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "gizwits_thermostat"))

import gizwits_lan as lan  # noqa: E402

# Real status frames from the capture, OFF state, 22:09 Monday.
STATUS_OFF = bytes.fromhex("06 00 02 02 00 e6 01 1d 00 01 22 09 00 d7")
STATUS_ON = bytes.fromhex("06 00 0b 02 00 e6 01 1d 00 01 22 09 00 d7")
# Real UDP status broadcast (port 12414) from the same unit, with the
# device's real MAC replaced by the fake aa:bb:cc:dd:ee:ff below.
BROADCAST_ON = bytes.fromhex(
    "00 00 00 03 18 00 00 91 06 aa bb cc dd ee ff 06 00 0b 02 00 e6 01 1d 00 01 20 59 00 d7"
)
BROADCAST_OFF = bytes.fromhex(
    "00 00 00 03 18 00 00 91 06 aa bb cc dd ee ff 06 00 02 02 00 e6 01 1d 00 01 20 58 00 d7"
)

PASSCODE = b"\x00\x0aABCDEFGHIJ"


class FakeThermostat:
    """Speaks the confirmed protocol: login, 05 00 00 status, 05 0a power (+ push)."""

    def __init__(self, push_after_power: bool = True, answer_status: bool = True):
        self.state = bytearray(STATUS_OFF)
        self.push_after_power = push_after_power
        self.answer_status = answer_status
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            while True:
                cmd, payload = await lan._read_frame(reader, 30)
                if cmd == lan.CMD_PASSCODE_REQ:
                    writer.write(lan.build_frame(lan.CMD_PASSCODE_ACK, PASSCODE))
                elif cmd == lan.CMD_LOGIN_REQ:
                    writer.write(lan.build_frame(lan.CMD_LOGIN_ACK, b"\x00" if payload == PASSCODE else b"\x01"))
                elif cmd == lan.CMD_CTRL:
                    seq, sub = payload[:4], payload[4:]
                    if sub == lan.SUBCMD_READ_STATUS:
                        if self.answer_status:
                            writer.write(lan.build_frame(lan.CMD_CTRL_ACK, seq + bytes(self.state)))
                    elif sub[:2] == lan.SUBCMD_POWER:
                        value = sub[2]
                        self.state[2] = (self.state[2] | 1) if value else (self.state[2] & 0xFE)
                        writer.write(lan.build_frame(lan.CMD_CTRL_ACK, seq + bytes([0x06, 0x0A, value])))
                        if self.push_after_power:
                            writer.write(lan.build_frame(lan.CMD_STATUS, bytes(self.state)))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, lan.GizwitsError):
            pass
        finally:
            writer.close()


class DecodeTests(unittest.TestCase):
    def test_decode_on(self):
        s = lan.decode_status(STATUS_ON)
        self.assertTrue(s.power)
        self.assertTrue(s.valve1)
        self.assertEqual((s.air_temp, s.setpoint, s.floor_temp), (23.0, 28.5, 21.5))
        self.assertEqual((s.weekday, s.device_time), (1, "22:09"))

    def test_decode_off(self):
        s = lan.decode_status(STATUS_OFF)
        self.assertFalse(s.power)
        self.assertFalse(s.valve1)

    def test_short_payload_rejected(self):
        with self.assertRaises(lan.GizwitsProtocolError):
            lan.decode_status(b"\x06\x00")

    def test_parse_discovery(self):
        # Field layout confirmed from a real discovery response; the
        # DID, MAC and product key below are fake placeholders, not
        # the real device's values.
        def field(b):
            return struct.pack(">H", len(b)) + b

        body = (
            field(b"FakeDid00000TestDevice")
            + field(bytes.fromhex("aabbccddeeff"))
            + field(b"04020037")
            + field(b"00112233445566778899aabbccddeeff")
            + b"\x00" * 8
            + b"api.gizwits.com:80\x004.1.2\x00"
        )
        pkt = lan.MAGIC + bytes([len(body) + 3]) + b"\x00\x00\x04" + body
        dev = lan.parse_discovery(pkt, "192.0.2.1")
        self.assertEqual(dev.did, "FakeDid00000TestDevice")
        self.assertEqual(dev.mac, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(dev.gagent_version, "4.1.2")


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_status_fast(self):
        async with FakeThermostat() as dev:
            s = await lan.GizwitsLanClient("127.0.0.1", dev.port).read_status(timeout=5)
        self.assertFalse(s.power)
        self.assertEqual((s.setpoint, s.floor_temp, s.air_temp), (28.5, 21.5, 23.0))

    async def test_set_power_on_and_off_same_session(self):
        async with FakeThermostat() as dev:
            client = lan.GizwitsLanClient("127.0.0.1", dev.port)
            async with client as session:
                await session.set_power(True, timeout=5)
                self.assertTrue((await session.read_status_fast(timeout=5)).power)
                await session.set_power(False, timeout=5)
                self.assertFalse((await session.read_status_fast(timeout=5)).power)

    async def test_set_power_one_shot(self):
        async with FakeThermostat() as dev:
            client = lan.GizwitsLanClient("127.0.0.1", dev.port)
            await client.send_power(True, timeout=5)
            self.assertTrue((await client.read_status(timeout=5)).power)

    async def test_bad_power_ack_raises_protocol_error(self):
        class BadAckThermostat(FakeThermostat):
            async def _handle(self, reader, writer):
                try:
                    while True:
                        cmd, payload = await lan._read_frame(reader, 30)
                        if cmd == lan.CMD_PASSCODE_REQ:
                            writer.write(lan.build_frame(lan.CMD_PASSCODE_ACK, PASSCODE))
                        elif cmd == lan.CMD_LOGIN_REQ:
                            writer.write(lan.build_frame(lan.CMD_LOGIN_ACK, b"\x00"))
                        elif cmd == lan.CMD_CTRL:
                            seq = payload[:4]
                            writer.write(lan.build_frame(lan.CMD_CTRL_ACK, seq + bytes([0x06, 0x0A, 0x99])))
                        await writer.drain()
                except (asyncio.IncompleteReadError, ConnectionError, OSError, lan.GizwitsError):
                    pass
                finally:
                    writer.close()

        async with BadAckThermostat() as dev:
            with self.assertRaises(lan.GizwitsProtocolError):
                await lan.GizwitsLanClient("127.0.0.1", dev.port).send_power(True, timeout=5)

    async def test_status_timeout_when_device_silent(self):
        async with FakeThermostat(answer_status=False) as dev:
            with self.assertRaises(lan.GizwitsTimeout):
                await lan.GizwitsLanClient("127.0.0.1", dev.port).read_status(timeout=2)

    async def test_refused_connection(self):
        client = lan.GizwitsLanClient("127.0.0.1", 1, connect_retries=1, retry_delay=0.1)
        with self.assertRaises(lan.GizwitsConnectionError):
            await client.read_status(timeout=2)

    async def test_check_login(self):
        async with FakeThermostat() as dev:
            await lan.GizwitsLanClient("127.0.0.1", dev.port).check_login()


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_real_broadcast(self):
        mac, s = lan.parse_status_broadcast(BROADCAST_ON)
        self.assertEqual(mac, "aa:bb:cc:dd:ee:ff")
        self.assertTrue(s.power)
        mac, s = lan.parse_status_broadcast(BROADCAST_OFF)
        self.assertFalse(s.power)

    def test_ignores_other_udp(self):
        self.assertIsNone(lan.parse_status_broadcast(lan.DISCOVERY_PACKET))
        self.assertIsNone(lan.parse_status_broadcast(b"garbage"))
        self.assertIsNone(lan.parse_status_broadcast(BROADCAST_ON[:20]))

    async def test_listener_dispatches_by_mac(self):
        listener = lan.StatusBroadcastListener("127.0.0.1", 0)
        await listener.start()
        got, other = [], []
        listener.register("AA:BB:CC:DD:EE:FF", got.append)
        listener.register("11:22:33:44:55:66", other.append)
        loop = asyncio.get_running_loop()
        tr, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=("127.0.0.1", listener.port))
        tr.sendto(BROADCAST_ON)
        tr.sendto(lan.DISCOVERY_PACKET)
        for _ in range(50):
            if got:
                break
            await asyncio.sleep(0.05)
        tr.close()
        listener.close()
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].power)
        self.assertEqual(other, [])


if __name__ == "__main__":
    unittest.main()
