"""Tests for Gizwits thermostat set-point control."""

from __future__ import annotations

import asyncio
import struct

import pytest

from custom_components.gizwits_thermostat.gizwits_lan import (
    CMD_CTRL,
    GizwitsLanClient,
    GizwitsProtocolError,
    SUBCMD_SETPOINT,
    build_frame,
)


# ---------------------------------------------------------------------------
# Protocol encoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("temperature", "expected_wire"),
    [
        (27.5, 0x13),
        (28.0, 0x18),
        (28.5, 0x1D),
        (29.0, 0x22),
    ],
)
def test_setpoint_wire_encoding(
    temperature: float,
    expected_wire: int,
) -> None:
    """Verify the captured thermostat set-point encoding."""

    wire_value = round(temperature * 10) - 256

    assert wire_value == expected_wire


@pytest.mark.parametrize(
    ("wire_value", "expected_temperature"),
    [
        (0x13, 27.5),
        (0x18, 28.0),
        (0x1D, 28.5),
        (0x22, 29.0),
    ],
)
def test_captured_setpoint_values(
    wire_value: int,
    expected_temperature: float,
) -> None:
    """Verify the reverse mapping of captured values."""

    temperature = (wire_value + 256) / 10

    assert temperature == expected_temperature


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("temperature", "expected_command"),
    [
        (27.5, bytes.fromhex("05 0e 01 13")),
        (28.0, bytes.fromhex("05 0e 01 18")),
        (28.5, bytes.fromhex("05 0e 01 1d")),
        (29.0, bytes.fromhex("05 0e 01 22")),
    ],
)
def test_setpoint_command(
    temperature: float,
    expected_command: bytes,
) -> None:
    """Verify the complete 0x93 sub-command."""

    wire_value = round(temperature * 10) - 256

    command = SUBCMD_SETPOINT + bytes([wire_value])

    assert command == expected_command


def test_setpoint_subcommand_constant() -> None:
    """The set-point sub-command must be 05 0e 01."""

    assert SUBCMD_SETPOINT == bytes.fromhex("05 0e 01")


# ---------------------------------------------------------------------------
# LAN client
# ---------------------------------------------------------------------------


class FakeWriter:
    """Minimal asyncio writer used by the tests."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        pass


class FakeReader:
    """Return pre-built thermostat frames."""

    def __init__(self, data: bytes) -> None:
        self.data = bytearray(data)

    async def readexactly(self, n: int) -> bytes:
        if len(self.data) < n:
            raise asyncio.IncompleteReadError(
                bytes(self.data),
                n,
            )

        result = bytes(self.data[:n])
        del self.data[:n]

        return result


def make_control_ack(
    sequence: bytes,
    wire_value: int,
) -> bytes:
    """Build a realistic 0x94 set-point ACK."""

    payload = (
        sequence
        + bytes.fromhex("06 0e 01")
        + bytes([wire_value])
    )

    return build_frame(
        CMD_CTRL + 1,
        payload,
    )


@pytest.mark.asyncio
async def test_setpoint_sends_correct_command() -> None:
    """set_setpoint() must send 05 0e 01 <value>."""

    client = GizwitsLanClient("192.0.2.1")

    reader = FakeReader(b"")
    writer = FakeWriter()

    client._reader = reader
    client._writer = writer

    # The sequence is generated internally.
    # Provide a matching ACK by replacing _control for this focused test.

    captured: list[bytes] = []

    async def fake_control(
        payload: bytes,
        timeout: float,
    ) -> bytes:
        captured.append(payload)
        return bytes.fromhex("06 0e 01 1d")

    client._control = fake_control  # type: ignore[method-assign]

    await client.set_setpoint(28.5)

    assert captured == [
        bytes.fromhex("05 0e 01 1d")
    ]


@pytest.mark.asyncio
async def test_setpoint_accepts_28_degrees() -> None:
    """28.0 C must generate wire value 0x18."""

    client = GizwitsLanClient("192.0.2.1")

    captured: list[bytes] = []

    async def fake_control(
        payload: bytes,
        timeout: float,
    ) -> bytes:
        captured.append(payload)
        return bytes.fromhex("06 0e 01 18")

    client._control = fake_control  # type: ignore[method-assign]

    await client.set_setpoint(28.0)

    assert captured[0] == bytes.fromhex(
        "05 0e 01 18"
    )


@pytest.mark.asyncio
async def test_setpoint_accepts_29_degrees() -> None:
    """29.0 C must generate wire value 0x22."""

    client = GizwitsLanClient("192.0.2.1")

    captured: list[bytes] = []

    async def fake_control(
        payload: bytes,
        timeout: float,
    ) -> bytes:
        captured.append(payload)
        return bytes.fromhex("06 0e 01 22")

    client._control = fake_control  # type: ignore[method-assign]

    await client.set_setpoint(29.0)

    assert captured[0] == bytes.fromhex(
        "05 0e 01 22"
    )


@pytest.mark.asyncio
async def test_setpoint_rejects_wrong_ack() -> None:
    """A mismatching ACK must raise GizwitsProtocolError."""

    client = GizwitsLanClient("192.0.2.1")

    async def fake_control(
        payload: bytes,
        timeout: float,
    ) -> bytes:
        # Asked for 28.5 (0x1d), but thermostat claims 28.0 (0x18).
        return bytes.fromhex("06 0e 01 18")

    client._control = fake_control  # type: ignore[method-assign]

    with pytest.raises(GizwitsProtocolError):
        await client.set_setpoint(28.5)


@pytest.mark.asyncio
async def test_setpoint_rejects_wrong_command_ack() -> None:
    """An ACK for a different command must be rejected."""

    client = GizwitsLanClient("192.0.2.1")

    async def fake_control(
        payload: bytes,
        timeout: float,
    ) -> bytes:
        return bytes.fromhex("06 0a 01")

    client._control = fake_control  # type: ignore[method-assign]

    with pytest.raises(GizwitsProtocolError):
        await client.set_setpoint(28.5)


@pytest.mark.asyncio
async def test_setpoint_rejects_short_ack() -> None:
    """A truncated ACK must be rejected."""

    client = GizwitsLanClient("192.0.2.1")

    async def fake_control(
        payload: bytes,
        timeout: float,
    ) -> bytes:
        return bytes.fromhex("06 0e")

    client._control = fake_control  # type: ignore[method-assign]

    with pytest.raises(GizwitsProtocolError):
        await client.set_setpoint(28.5)


# ---------------------------------------------------------------------------
# Boundary / invalid values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "temperature",
    [
        -1.0,
        -10.0,
        100.0,
        1000.0,
    ],
)
async def test_setpoint_rejects_unrepresentable_temperature(
    temperature: float,
) -> None:
    """Temperatures that cannot fit in one protocol byte are rejected."""

    client = GizwitsLanClient("192.0.2.1")

    async def fake_control(
        payload: bytes,
        timeout: float,
    ) -> bytes:
        pytest.fail("No command should be sent")

    client._control = fake_control  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        await client.set_setpoint(temperature)


@pytest.mark.asyncio
async def test_setpoint_normalizes_to_half_degree() -> None:
    """HA's 0.5 C resolution is enforced by the client."""

    client = GizwitsLanClient("192.0.2.1")

    captured: list[bytes] = []

    async def fake_control(
        payload: bytes,
        timeout: float,
    ) -> bytes:
        captured.append(payload)
        return bytes.fromhex("06 0e 01 1d")

    client._control = fake_control  # type: ignore[method-assign]

    await client.set_setpoint(28.49)

    assert captured[0] == bytes.fromhex(
        "05 0e 01 1d"
    )