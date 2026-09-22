"""Asyncio client for the Gizwits GAgent LAN protocol (TCP 12416, UDP 12414).

Pure Python: no Home Assistant imports, so it can be tested on its own.

Frame layout (as observed on the thermostat):
    00 00 00 03 | LL | 00 | CC CC | payload
        LL = len(flag + cmd + payload)

Read sequence that works on this device:
    0x06 passcode request -> 0x07 passcode
    0x08 login (passcode) -> 0x09 result (last byte 0 = OK)
    0x90 payload 0x02     -> 0x91 status frame
        (can take 10-30 s; also pushed unasked)

Status payload (14 bytes), confirmed against the app's JSON and captures:
    [0]      0x06 (device -> app passthrough marker)
    [2]      flag byte, LSB first, in the app's field order:
             bit0 kaiguan (power)
             bit1 shouzi
             bit2 jianpan (key lock)
             bit3 famen1 (valve 1)
             bit4 famen2 ...
    [3]      0x02 in every capture
    [4:6]    air temperature x10
    [6:8]    setpoint x10
    [8]      chengxu (program)
    [9]      week (0 = Sunday)
    [10:12]  device clock, hour and minute as BCD
    [12:14]  floor temperature x10

The thermostat also broadcasts its status by UDP to
255.255.255.255:12414 whenever it changes.
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

# Sub-commands inside a 0x93/0x94 control packet.
#
# Confirmed from real captures:
#
#   05 00 00
#       -> request full status
#
#   05 0a <00|01>
#       -> set power off/on
#
#   05 0e 01 <value>
#       -> set target temperature
#
# Setpoint encoding confirmed from the captures:
#
#   wire_value = round(temperature * 10) - 256
#
# Captured values:
#
#   27.5 C -> 0x13
#   28.0 C -> 0x18
#   28.5 C -> 0x1D
#   29.0 C -> 0x22
#
# Setpoint ACK:
#
#   06 0e 01 <same value>
#
SUBCMD_READ_STATUS = bytes.fromhex("05 00 00")
SUBCMD_POWER = bytes.fromhex("05 0a")
SUBCMD_SETPOINT = bytes.fromhex("05 0e 01")

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
    weekday: int
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
    """Build a GAgent frame."""

    body = b"\x00" + struct.pack(">H", cmd) + payload

    if len(body) > 127:
        raise ValueError("payload too long for a single-byte length")

    return MAGIC + bytes([len(body)]) + body


def _bcd(value: int) -> int | None:
    hi, lo = value >> 4, value & 0x0F

    return (
        hi * 10 + lo
        if hi < 10 and lo < 10
        else None
    )


def encode_setpoint(temperature: float) -> int:
    """Convert a Celsius setpoint to the thermostat wire value.

    The thermostat accepts 0.5 C increments.
    This version automatically normalizes/rounds values to the nearest 0.5 C increment.

    Wire encoding confirmed from packet captures:

        wire_value = round(temperature * 10) - 256

    Examples:

        27.5 C -> 0x13
        28.0 C -> 0x18
        28.5 C -> 0x1D
        29.0 C -> 0x22
    """

    temperature = float(temperature)

    # Automatically normalize/round to the nearest 0.5 C increment
    temperature = round(temperature * 2) / 2

    if not 0.0 <= temperature <= 51.1:
        raise ValueError(
            f"target temperature {temperature} C is outside "
            "the representable thermostat range"
        )

    # Map to wire value: e.g., 28.5 * 10 = 285 -> 285 - 256 = 29 (0x1D)
    wire_value = round(temperature * 10) - 256
    return wire_value


def decode_status(payload: bytes) -> RadiantStatus:
    """Decode the 14-byte status payload of a 0x91 frame."""

    if len(payload) < STATUS_LEN:
        raise GizwitsProtocolError(
            f"status payload too short ({len(payload)} bytes)"
        )

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


def parse_status_broadcast(
    data: bytes,
) -> tuple[str, RadiantStatus] | None:
    """Parse a UDP status broadcast."""

    if len(data) < 9 or data[:4] != MAGIC:
        return None

    body = data[5 : 5 + data[4]]

    if len(body) < 4:
        return None

    if struct.unpack(">H", body[1:3])[0] != CMD_STATUS:
        return None

    payload = body[3:]

    if not payload or payload[0] != 6:
        return None

    if len(payload) < 7 + STATUS_LEN:
        return None

    mac = ":".join(
        f"{b:02x}"
        for b in payload[1:7]
    )

    try:
        return mac, decode_status(payload[7:])
    except GizwitsProtocolError:
        return None


async def _read_frame(
    reader: asyncio.StreamReader,
    idle_timeout: float,
) -> tuple[int, bytes]:
    """Read one GAgent frame."""

    try:
        magic = await asyncio.wait_for(
            reader.readexactly(4),
            idle_timeout,
        )
    except asyncio.IncompleteReadError as err:
        raise GizwitsConnectionError(
            "connection closed by the device"
        ) from err

    try:
        if magic != MAGIC:
            raise GizwitsProtocolError(
                f"bad frame prefix {magic.hex(' ')}"
            )

        length = 0
        shift = 0

        while True:
            byte = (
                await asyncio.wait_for(
                    reader.readexactly(1),
                    5,
                )
            )[0]

            length |= (byte & 0x7F) << shift
            shift += 7

            if not byte & 0x80:
                break

        body = await asyncio.wait_for(
            reader.readexactly(length),
            5,
        )

    except (
        asyncio.IncompleteReadError,
        asyncio.TimeoutError,
    ) as err:
        raise GizwitsConnectionError(
            "truncated frame"
        ) from err

    if len(body) < 3:
        raise GizwitsProtocolError(
            "frame too short"
        )

    return struct.unpack(">H", body[1:3])[0], body[3:]


class GizwitsLanClient:
    """One TCP session.

    Connect, log in, then read/set status.

    Use as an async context manager to send several commands:

        async with GizwitsLanClient(host) as client:
            await client.set_power(True)
            await client.set_setpoint(28.5)

    Or use read_status(), send_power(), or send_setpoint() for
    one-shot operations.
    """

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

    async def _connect(
        self,
    ) -> tuple[
        asyncio.StreamReader,
        asyncio.StreamWriter,
    ]:
        last: Exception | None = None

        for attempt in range(
            self._connect_retries + 1
        ):
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(
                        self.host,
                        self.port,
                    ),
                    self._connect_timeout,
                )

            except (
                OSError,
                asyncio.TimeoutError,
            ) as err:
                last = err

                if attempt < self._connect_retries:
                    await asyncio.sleep(
                        self._retry_delay
                    )

        raise GizwitsConnectionError(
            f"cannot connect to "
            f"{self.host}:{self.port}: {last!r}"
        )

    @staticmethod
    async def _expect(
        reader: asyncio.StreamReader,
        want: int,
        timeout: float,
    ) -> bytes:
        deadline = time.monotonic() + timeout

        while True:
            left = deadline - time.monotonic()

            if left <= 0:
                raise GizwitsTimeout(
                    f"no 0x{want:04x} frame "
                    f"within {timeout:.0f}s"
                )

            try:
                cmd, payload = await _read_frame(
                    reader,
                    left,
                )

            except asyncio.TimeoutError as err:
                raise GizwitsTimeout(
                    f"no 0x{want:04x} frame "
                    f"within {timeout:.0f}s"
                ) from err

            if cmd == want:
                return payload

    async def _login(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(
            build_frame(CMD_PASSCODE_REQ)
        )
        await writer.drain()

        payload = await self._expect(
            reader,
            CMD_PASSCODE_ACK,
            8,
        )

        if len(payload) < 2:
            raise GizwitsProtocolError(
                "short passcode reply"
            )

        n = struct.unpack(
            ">H",
            payload[:2],
        )[0]

        passcode = payload[2 : 2 + n]

        if not passcode:
            raise GizwitsProtocolError(
                "device returned an empty passcode"
            )

        writer.write(
            build_frame(
                CMD_LOGIN_REQ,
                struct.pack(
                    ">H",
                    len(passcode),
                ) + passcode,
            )
        )
        await writer.drain()

        result = await self._expect(
            reader,
            CMD_LOGIN_ACK,
            8,
        )

        if not result or result[-1] != 0:
            raise GizwitsProtocolError(
                f"login rejected ({result.hex(' ')})"
            )

    @staticmethod
    async def _close(
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.close()

        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def check_login(self) -> None:
        """Connect, passcode and login only."""

        reader, writer = await self._connect()

        try:
            await self._login(
                reader,
                writer,
            )

        except OSError as err:
            raise GizwitsConnectionError(
                str(err)
            ) from err

        finally:
            await self._close(writer)

    # ------------------------------------------------------------- session

    async def __aenter__(
        self,
    ) -> "GizwitsLanClient":
        self._reader, self._writer = (
            await self._connect()
        )

        try:
            await self._login(
                self._reader,
                self._writer,
            )

        except OSError as err:
            await self._close(self._writer)
            self._reader = self._writer = None

            raise GizwitsConnectionError(
                str(err)
            ) from err

        return self

    async def __aexit__(
        self,
        *exc_info,
    ) -> None:
        if self._writer is not None:
            await self._close(
                self._writer
            )

        self._reader = self._writer = None

    def _next_seq(self) -> bytes:
        self._seq = (
            self._seq + 1
        ) & 0xFFFFFFFF

        return struct.pack(
            ">I",
            self._seq,
        )

    async def _control(
        self,
        sub_payload: bytes,
        timeout: float,
    ) -> bytes:
        """Send a 0x93 frame and return the matching 0x94 ACK payload."""

        if (
            self._writer is None
            or self._reader is None
        ):
            raise GizwitsConnectionError(
                "not connected "
                "(use 'async with "
                "GizwitsLanClient(...)')"
            )

        seq = self._next_seq()

        self._writer.write(
            build_frame(
                CMD_CTRL,
                seq + sub_payload,
            )
        )
        await self._writer.drain()

        deadline = (
            time.monotonic() + timeout
        )

        while True:
            left = (
                deadline
                - time.monotonic()
            )

            if left <= 0:
                raise GizwitsTimeout(
                    f"no 0x94 ack within "
                    f"{timeout:.0f}s"
                )

            try:
                cmd, payload = (
                    await _read_frame(
                        self._reader,
                        left,
                    )
                )

            except asyncio.TimeoutError as err:
                raise GizwitsTimeout(
                    f"no 0x94 ack within "
                    f"{timeout:.0f}s"
                ) from err

            if (
                cmd == CMD_CTRL_ACK
                and payload[:4] == seq
            ):
                return payload[4:]

    async def read_status_fast(
        self,
        timeout: float = 10.0,
    ) -> RadiantStatus:
        """Ask for status with 05 00 00."""

        try:
            return decode_status(
                await self._control(
                    SUBCMD_READ_STATUS,
                    timeout,
                )
            )

        except OSError as err:
            raise GizwitsConnectionError(
                str(err)
            ) from err

    async def set_power(
        self,
        on: bool,
        timeout: float = 10.0,
    ) -> None:
        """Send the confirmed power write."""

        value = 1 if on else 0

        try:
            ack = await self._control(
                SUBCMD_POWER
                + bytes([value]),
                timeout,
            )

        except OSError as err:
            raise GizwitsConnectionError(
                str(err)
            ) from err

        if (
            len(ack) < 3
            or ack[0] != 0x06
            or ack[1] != 0x0A
            or ack[2] != value
        ):
            raise GizwitsProtocolError(
                f"unexpected power ack: "
                f"{ack.hex(' ')}"
            )

    async def set_setpoint(
        self,
        temperature: float,
        timeout: float = 10.0,
    ) -> None:
        """Set the thermostat target temperature.

        The command is:

            05 0e 01 <wire_value>

        where:

            wire_value = round(temperature * 10) - 256

        The thermostat operates in 0.5 C increments.

        The acknowledgement is:

            06 0e 01 <wire_value>
        """

        wire_value = encode_setpoint(
            temperature
        )

        try:
            ack = await self._control(
                SUBCMD_SETPOINT
                + bytes([wire_value]),
                timeout,
            )

        except OSError as err:
            raise GizwitsConnectionError(
                str(err)
            ) from err

        if (
            len(ack) < 4
            or ack[0] != 0x06
            or ack[1] != 0x0E
            or ack[2] != 0x01
            or ack[3] != wire_value
        ):
            raise GizwitsProtocolError(
                f"unexpected setpoint ack: "
                f"{ack.hex(' ')}"
            )

    async def read_status(
        self,
        timeout: float = 10.0,
    ) -> RadiantStatus:
        """One-shot status read."""

        async with self as session:
            return await session.read_status_fast(
                timeout
            )

    async def send_power(
        self,
        on: bool,
        timeout: float = 10.0,
    ) -> None:
        """One-shot power command."""

        async with self as session:
            await session.set_power(
                on,
                timeout,
            )

    async def send_setpoint(
        self,
        temperature: float,
        timeout: float = 10.0,
    ) -> None:
        """One-shot setpoint command."""

        async with self as session:
            await session.set_setpoint(
                temperature,
                timeout,
            )


# ------------------------------------------------------------------ discovery


def _read_len_bytes(
    data: bytes,
    pos: int,
) -> tuple[bytes, int]:
    if pos + 2 > len(data):
        raise ValueError(
            "truncated discovery packet"
        )

    n = struct.unpack(
        ">H",
        data[pos : pos + 2],
    )[0]

    pos += 2

    if pos + n > len(data):
        raise ValueError(
            "bad field length in discovery packet"
        )

    return (
        data[pos : pos + n],
        pos + n,
    )


def parse_discovery(
    data: bytes,
    ip: str,
) -> DiscoveredDevice:
    if len(data) < 8 or data[:4] != MAGIC:
        raise ValueError(
            "not a GAgent packet"
        )

    if (
        struct.unpack(
            ">H",
            data[6:8],
        )[0]
        != CMD_DISCOVER_ACK
    ):
        raise ValueError(
            "not a discovery response"
        )

    did, pos = _read_len_bytes(
        data,
        8,
    )

    mac, pos = _read_len_bytes(
        data,
        pos,
    )

    wifi_fw, pos = _read_len_bytes(
        data,
        pos,
    )

    product_key, pos = _read_len_bytes(
        data,
        pos,
    )

    pos += 8

    strings = data[pos:].split(
        b"\x00"
    )

    version = (
        strings[1].decode(
            "ascii",
            "replace",
        )
        if len(strings) > 1
        else ""
    )

    return DiscoveredDevice(
        ip=ip,
        did=did.decode(
            "ascii",
            "replace",
        ),
        mac=":".join(
            f"{b:02x}"
            for b in mac
        ),
        product_key=product_key.decode(
            "ascii",
            "replace",
        ),
        wifi_firmware=wifi_fw.decode(
            "ascii",
            "replace",
        ),
        gagent_version=version,
    )


class _DiscoveryProtocol(
    asyncio.DatagramProtocol
):
    def __init__(self) -> None:
        self.responses: list[
            tuple[bytes, str]
        ] = []

    def datagram_received(
        self,
        data: bytes,
        addr: tuple[str, int],
    ) -> None:
        self.responses.append(
            (data, addr[0])
        )


async def discover(
    host: str | None = None,
    timeout: float = 4.0,
) -> list[DiscoveredDevice]:
    """UDP discovery."""

    loop = asyncio.get_running_loop()

    transport, proto = (
        await loop.create_datagram_endpoint(
            _DiscoveryProtocol,
            local_addr=(
                "0.0.0.0",
                0,
            ),
            allow_broadcast=True,
        )
    )

    try:
        targets = (
            [host] if host else []
        ) + [
            "255.255.255.255"
        ]

        for target in targets:
            with contextlib.suppress(
                OSError
            ):
                transport.sendto(
                    DISCOVERY_PACKET,
                    (
                        target,
                        DISCOVERY_PORT,
                    ),
                )

        await asyncio.sleep(timeout)

    finally:
        transport.close()

    found: dict[
        str,
        DiscoveredDevice,
    ] = {}

    for data, ip in proto.responses:
        try:
            dev = parse_discovery(
                data,
                ip,
            )
        except ValueError:
            continue

        found[dev.did] = dev

    return list(found.values())


# ------------------------------------------------------- broadcast listener


class _StatusProtocol(
    asyncio.DatagramProtocol
):
    def __init__(
        self,
        owner: "StatusBroadcastListener",
    ) -> None:
        self._owner = owner

    def datagram_received(
        self,
        data: bytes,
        addr: tuple[str, int],
    ) -> None:
        self._owner.dispatch(data)


class StatusBroadcastListener:
    """Receives thermostat UDP status broadcasts."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DISCOVERY_PORT,
    ) -> None:
        self._host = host
        self._port = port

        self._subs: dict[
            str,
            list[
                Callable[
                    [RadiantStatus],
                    None,
                ]
            ],
        ] = {}

        self._transport: (
            asyncio.DatagramTransport | None
        ) = None

        self.refs = 0

    @property
    def port(self) -> int:
        assert self._transport is not None

        return self._transport.get_extra_info(
            "sockname"
        )[1]

    async def start(self) -> None:
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        try:
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            )

            if hasattr(
                socket,
                "SO_REUSEPORT",
            ):
                with contextlib.suppress(
                    OSError
                ):
                    sock.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_REUSEPORT,
                        1,
                    )

            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BROADCAST,
                1,
            )

            sock.bind(
                (
                    self._host,
                    self._port,
                )
            )

            sock.setblocking(False)

        except OSError:
            sock.close()
            raise

        self._transport, _ = (
            await asyncio.get_running_loop()
            .create_datagram_endpoint(
                lambda: _StatusProtocol(self),
                sock=sock,
            )
        )

    def register(
        self,
        mac: str,
        callback: Callable[
            [RadiantStatus],
            None,
        ],
    ) -> Callable[[], None]:
        mac = mac.lower()

        self._subs.setdefault(
            mac,
            [],
        ).append(callback)

        def _unregister() -> None:
            with contextlib.suppress(
                ValueError,
                KeyError,
            ):
                self._subs[mac].remove(
                    callback
                )

        return _unregister

    def dispatch(
        self,
        data: bytes,
    ) -> None:
        parsed = parse_status_broadcast(
            data
        )

        if parsed is None:
            return

        mac, status = parsed

        for callback in list(
            self._subs.get(mac, ())
        ):
            try:
                callback(status)
            except Exception:
                _LOGGER.exception(
                    "error handling status "
                    "broadcast from %s",
                    mac,
                )

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None