"""Asyncio client for the Gizwits GAgent LAN protocol (TCP 12416, UDP 12414).

Pure Python: no Home Assistant imports, so it can be tested on its own.

Frame layout (as observed on the thermostat):
    00 00 00 03 | LL | 00 | CC CC | payload      LL = len(flag + cmd + payload)

Read sequence that works on this device:
    0x06 passcode request -> 0x07 passcode
    0x08 login (passcode) -> 0x09 result (last byte 0 = OK)
    0x90 payload 0x02     -> 0x91 status frame   (can take 10-30 s; also pushed unasked)

Status payload (14 bytes), confirmed against the app's JSON and your captures:
    [0]      0x06 (device -> app passthrough marker)
    [2]      flag byte, LSB first, in the app's field order:
             bit0 kaiguan (power: 0x0b ON / 0x02 OFF), bit1 shouzi (1 in every capture),
             bit2 jianpan (key lock), bit3 famen1 (valve 1), bit4 famen2 ...
    [3]      0x02 in every capture (probably "cewen", sensor mode)
    [4:6]    air temperature x10
    [6:8]    setpoint x10
    [8]      chengxu (program)      [9] week (0 = Sunday)
    [10:12]  device clock, hour and minute as BCD (0x13 0x43 = 13:43)
    [12:14]  floor temperature x10

The thermostat also BROADCASTS its status by UDP to 255.255.255.255:12414 whenever it changes:
    00 00 00 03 LL 00 00 91 | 06 <6-byte MAC> | <14-byte status payload>
so state changes can be received instantly without opening a TCP session.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

MAGIC = b"\x00\x00\x00\x03"
DISCOVERY_PORT = 12414
LAN_PORT = 12416
DISCOVERY_PACKET = bytes.fromhex("00 00 00 03 03 00 00 03")

CMD_DISCOVER_ACK = 0x0004
CMD_PASSCODE_REQ = 0x0006
CMD_PASSCODE_ACK = 0x0007
CMD_LOGIN_REQ = 0x0008
CMD_LOGIN_ACK = 0x0009
CMD_PING = 0x0015
CMD_PONG = 0x0016
CMD_READ = 0x0090
CMD_STATUS = 0x0091
CMD_CTRL = 0x0093
CMD_CTRL_ACK = 0x0094

# Sub-commands inside a 0x93/0x94 P0 (confirmed from a real capture of the phone app):
#   05 00 00           -> ask for a full status reply (arrives as 0x94, not 0x91 - fast & reliable)
#   05 0a <00|01>       -> set power off/on; ack is 0x94 with '06 0a <value>'; a 0x91 push with the
#                          new status normally follows a moment later
SUBCMD_READ_STATUS = bytes.fromhex("05 00 00")
SUBCMD_POWER = bytes.fromhex("05 0a")

POWER_BIT = 0x01
VALVE1_BIT = 0x08
STATUS_LEN = 14


class GizwitsError(Exception):
    """Base error."""


class GizwitsConnectionError(GizwitsError):
    """Could not connect / connection dropped."""


class GizwitsProtocolError(GizwitsError):
    """Unexpected data from the device."""


class GizwitsTimeout(GizwitsError):
    """The device did not answer in time."""


@dataclass(frozen=True)
class RadiantStatus:
    """Decoded status frame."""

    raw: bytes
    power: bool
    valve1: bool
    setpoint: float
    floor_temp: float
    air_temp: float
    program: int
    weekday: int  # 0 = Sunday
    hour: int | None
    minute: int | None

    @property
    def device_time(self) -> str | None:
        if self.hour is None or self.minute is None:
            return None
        return f"{self.hour:02d}:{self.minute:02d}"


@dataclass(frozen=True)
class DiscoveredDevice:
    """Result of UDP discovery."""

    ip: str
    did: str
    mac: str
    product_key: str
    wifi_firmware: str
    gagent_version: str


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    body = b"\x00" + struct.pack(">H", cmd) + payload
    if len(body) > 127:
        raise ValueError("payload too long for a single-byte length")
    return MAGIC + bytes([len(body)]) + body


def _bcd(value: int) -> int | None:
    hi, lo = value >> 4, value & 0x0F
    return hi * 10 + lo if hi < 10 and lo < 10 else None


def decode_status(payload: bytes) -> RadiantStatus:
    """Decode the 14-byte status payload of a 0x91 frame."""
    if len(payload) < STATUS_LEN:
        raise GizwitsProtocolError(f"status payload too short ({len(payload)} bytes)")
    flags = payload[2]
    return RadiantStatus(
        raw=bytes(payload[:STATUS_LEN]),
        power=bool(flags & POWER_BIT),
        valve1=bool(flags & VALVE1_BIT),
        setpoint=struct.unpack(">H", payload[6:8])[0] / 10,
        floor_temp=struct.unpack(">H", payload[12:14])[0] / 10,
        air_temp=struct.unpack(">H", payload[4:6])[0] / 10,
        program=payload[8],
        weekday=payload[9],
        hour=_bcd(payload[10]),
        minute=_bcd(payload[11]),
    )


def parse_status_broadcast(data: bytes) -> tuple[str, RadiantStatus] | None:
    """Parse a UDP status broadcast -> (mac 'aa:bb:..', status), or None if it is something else."""
    if len(data) < 9 or data[:4] != MAGIC:
        return None
    body = data[5 : 5 + data[4]]
    if len(body) < 4 or struct.unpack(">H", body[1:3])[0] != CMD_STATUS:
        return None
    payload = body[3:]
    if not payload or payload[0] != 6 or len(payload) < 7 + STATUS_LEN:
        return None
    mac = ":".join(f"{b:02x}" for b in payload[1:7])
    try:
        return mac, decode_status(payload[7:])
    except GizwitsProtocolError:
        return None


async def _read_frame(reader: asyncio.StreamReader, idle_timeout: float) -> tuple[int, bytes]:
    """Read one frame. Raises asyncio.TimeoutError only if nothing arrived in idle_timeout."""
    try:
        magic = await asyncio.wait_for(reader.readexactly(4), idle_timeout)
    except asyncio.IncompleteReadError as err:
        raise GizwitsConnectionError("connection closed by the device") from err
    try:
        if magic != MAGIC:
            raise GizwitsProtocolError(f"bad frame prefix {magic.hex(' ')}")
        length, shift = 0, 0
        while True:
            byte = (await asyncio.wait_for(reader.readexactly(1), 5))[0]
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        body = await asyncio.wait_for(reader.readexactly(length), 5)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError) as err:
        raise GizwitsConnectionError("truncated frame") from err
    if len(body) < 3:
        raise GizwitsProtocolError("frame too short")
    return struct.unpack(">H", body[1:3])[0], body[3:]


class GizwitsLanClient:
    """One TCP session: connect, log in, then read/set status. Use as an async context manager
    to send several commands over one connection (`async with GizwitsLanClient(host) as c: ...`),
    or call read_status()/set_power() directly for a single one-shot connection."""

    def __init__(
        self,
        host: str,
        port: int = LAN_PORT,
        *,
        connect_timeout: float = 8.0,
        connect_retries: int = 2,
        retry_delay: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._connect_retries = connect_retries
        self._retry_delay = retry_delay
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._seq = int(time.time()) & 0xFFFF

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last: Exception | None = None
        for attempt in range(self._connect_retries + 1):
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), self._connect_timeout
                )
            except (OSError, asyncio.TimeoutError) as err:
                last = err
                if attempt < self._connect_retries:
                    await asyncio.sleep(self._retry_delay)
        raise GizwitsConnectionError(f"cannot connect to {self.host}:{self.port}: {last!r}")

    @staticmethod
    async def _expect(reader: asyncio.StreamReader, want: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise GizwitsTimeout(f"no 0x{want:04x} frame within {timeout:.0f}s")
            try:
                cmd, payload = await _read_frame(reader, left)
            except asyncio.TimeoutError as err:
                raise GizwitsTimeout(f"no 0x{want:04x} frame within {timeout:.0f}s") from err
            if cmd == want:
                return payload

    async def _login(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(build_frame(CMD_PASSCODE_REQ))
        await writer.drain()
        payload = await self._expect(reader, CMD_PASSCODE_ACK, 8)
        if len(payload) < 2:
            raise GizwitsProtocolError("short passcode reply")
        n = struct.unpack(">H", payload[:2])[0]
        passcode = payload[2 : 2 + n]
        if not passcode:
            raise GizwitsProtocolError("device returned an empty passcode")
        writer.write(build_frame(CMD_LOGIN_REQ, struct.pack(">H", len(passcode)) + passcode))
        await writer.drain()
        result = await self._expect(reader, CMD_LOGIN_ACK, 8)
        if not result or result[-1] != 0:
            raise GizwitsProtocolError(f"login rejected ({result.hex(' ')})")

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def check_login(self) -> None:
        """Connect + passcode + login only (fast); used by the config flow."""
        reader, writer = await self._connect()
        try:
            await self._login(reader, writer)
        except OSError as err:
            raise GizwitsConnectionError(str(err)) from err
        finally:
            await self._close(writer)

    # ------------------------------------------------------------- session

    async def __aenter__(self) -> "GizwitsLanClient":
        self._reader, self._writer = await self._connect()
        try:
            await self._login(self._reader, self._writer)
        except OSError as err:
            await self._close(self._writer)
            self._reader = self._writer = None
            raise GizwitsConnectionError(str(err)) from err
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._writer is not None:
            await self._close(self._writer)
        self._reader = self._writer = None

    def _next_seq(self) -> bytes:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return struct.pack(">I", self._seq)

    async def _control(self, sub_payload: bytes, timeout: float) -> bytes:
        """Send a 0x93 frame in the open session and return the 0x94 ack payload (seq stripped)."""
        if self._writer is None or self._reader is None:
            raise GizwitsConnectionError("not connected (use 'async with GizwitsLanClient(...)')")
        seq = self._next_seq()
        self._writer.write(build_frame(CMD_CTRL, seq + sub_payload))
        await self._writer.drain()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise GizwitsTimeout(f"no 0x94 ack within {timeout:.0f}s")
            try:
                cmd, payload = await _read_frame(self._reader, left)
            except asyncio.TimeoutError as err:
                raise GizwitsTimeout(f"no 0x94 ack within {timeout:.0f}s") from err
            if cmd == CMD_CTRL_ACK and payload[:4] == seq:
                return payload[4:]

    async def read_status_fast(self, timeout: float = 10.0) -> RadiantStatus:
        """Ask for status with the confirmed 05 00 00 sub-command; answers via 0x94, not 0x91/0x90."""
        try:
            return decode_status(await self._control(SUBCMD_READ_STATUS, timeout))
        except OSError as err:
            raise GizwitsConnectionError(str(err)) from err

    async def set_power(self, on: bool, timeout: float = 10.0) -> None:
        """Send the confirmed power write and check the ack echoes the value we asked for."""
        value = 1 if on else 0
        try:
            ack = await self._control(SUBCMD_POWER + bytes([value]), timeout)
        except OSError as err:
            raise GizwitsConnectionError(str(err)) from err
        if len(ack) < 3 or ack[0] != 0x06 or ack[1] != 0x0A or ack[2] != value:
            raise GizwitsProtocolError(f"unexpected power ack: {ack.hex(' ')}")

    async def read_status(self, timeout: float = 10.0) -> RadiantStatus:
        """One-shot: open a session, read status via the fast 05 00 00 request, close."""
        async with self as session:
            return await session.read_status_fast(timeout)

    async def send_power(self, on: bool, timeout: float = 10.0) -> None:
        """One-shot: open a session, send the power command, close."""
        async with self as session:
            await session.set_power(on, timeout)


# ------------------------------------------------------------------ discovery

def _read_len_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 2 > len(data):
        raise ValueError("truncated discovery packet")
    n = struct.unpack(">H", data[pos : pos + 2])[0]
    pos += 2
    if pos + n > len(data):
        raise ValueError("bad field length in discovery packet")
    return data[pos : pos + n], pos + n


def parse_discovery(data: bytes, ip: str) -> DiscoveredDevice:
    if len(data) < 8 or data[:4] != MAGIC:
        raise ValueError("not a GAgent packet")
    if struct.unpack(">H", data[6:8])[0] != CMD_DISCOVER_ACK:
        raise ValueError("not a discovery response")
    did, pos = _read_len_bytes(data, 8)
    mac, pos = _read_len_bytes(data, pos)
    wifi_fw, pos = _read_len_bytes(data, pos)
    product_key, pos = _read_len_bytes(data, pos)
    pos += 8  # MCU attributes
    strings = data[pos:].split(b"\x00")
    version = strings[1].decode("ascii", "replace") if len(strings) > 1 else ""
    return DiscoveredDevice(
        ip=ip,
        did=did.decode("ascii", "replace"),
        mac=":".join(f"{b:02x}" for b in mac),
        product_key=product_key.decode("ascii", "replace"),
        wifi_firmware=wifi_fw.decode("ascii", "replace"),
        gagent_version=version,
    )


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.responses: list[tuple[bytes, str]] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.responses.append((data, addr[0]))


async def discover(host: str | None = None, timeout: float = 4.0) -> list[DiscoveredDevice]:
    """UDP discovery. With `host`, also sends a unicast probe (works across subnets/VLAN quirks)."""
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        _DiscoveryProtocol, local_addr=("0.0.0.0", 0), allow_broadcast=True
    )
    try:
        targets = ([host] if host else []) + ["255.255.255.255"]
        for target in targets:
            with contextlib.suppress(OSError):
                transport.sendto(DISCOVERY_PACKET, (target, DISCOVERY_PORT))
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    found: dict[str, DiscoveredDevice] = {}
    for data, ip in proto.responses:
        try:
            dev = parse_discovery(data, ip)
        except ValueError:
            continue
        found[dev.did] = dev
    return list(found.values())


# ------------------------------------------------------- broadcast listener

class _StatusProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "StatusBroadcastListener") -> None:
        self._owner = owner

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._owner.dispatch(data)


class StatusBroadcastListener:
    """Receives the thermostats' UDP status broadcasts (port 12414) and fans them out by MAC.

    One listener is shared by every thermostat in Home Assistant. The socket uses SO_REUSEADDR
    so other tools listening on the same port keep working.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = DISCOVERY_PORT) -> None:
        self._host = host
        self._port = port
        self._subs: dict[str, list[Callable[[RadiantStatus], None]]] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self.refs = 0

    @property
    def port(self) -> int:
        assert self._transport is not None
        return self._transport.get_extra_info("sockname")[1]

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self._host, self._port))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: _StatusProtocol(self), sock=sock
        )

    def register(self, mac: str, callback: Callable[[RadiantStatus], None]) -> Callable[[], None]:
        mac = mac.lower()
        self._subs.setdefault(mac, []).append(callback)

        def _unregister() -> None:
            with contextlib.suppress(ValueError, KeyError):
                self._subs[mac].remove(callback)

        return _unregister

    def dispatch(self, data: bytes) -> None:
        parsed = parse_status_broadcast(data)
        if parsed is None:
            return
        mac, status = parsed
        for callback in list(self._subs.get(mac, ())):
            try:
                callback(status)
            except Exception:  # never let one subscriber break the socket
                _LOGGER.exception("error handling status broadcast from %s", mac)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
