"""Data coordinator: local status (push + poll) and local power control."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_DID,
    CONF_MAC,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .gizwits_lan import (
    GizwitsError,
    GizwitsLanClient,
    RadiantStatus,
)
from .push import (
    async_acquire_listener,
    async_release_listener,
)


_LOGGER = logging.getLogger(__name__)

RadiantConfigEntry = ConfigEntry["RadiantCoordinator"]


def parse_hex(value: str | None) -> bytes | None:
    """'05 00 ab' -> b'\\x05\\x00\\xab'; empty -> None; raises ValueError if malformed."""

    if not value or not value.strip():
        return None

    return bytes.fromhex(
        value.replace(",", " ").replace("0x", "")
    )


class RadiantCoordinator(
    DataUpdateCoordinator[RadiantStatus]
):
    """Polls the thermostat locally.

    Every entity also updates instantly from
    its UDP pushes.
    """

    config_entry: RadiantConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RadiantConfigEntry,
    ) -> None:
        interval = max(
            int(
                entry.options.get(
                    CONF_SCAN_INTERVAL,
                    DEFAULT_SCAN_INTERVAL,
                )
            ),
            MIN_SCAN_INTERVAL,
        )

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_DID]}",
            update_interval=timedelta(
                seconds=interval
            ),
        )

        self.did: str = entry.data[CONF_DID]

        self.mac: str = (
            entry.data.get(CONF_MAC) or ""
        ).lower()

        self.client = GizwitsLanClient(
            entry.data[CONF_HOST]
        )

        self._lock = asyncio.Lock()
        self._unregister_push = None
        self._listener_held = False

    # ---------------------------------------------------------------- polling

    async def _async_update_data(
        self,
    ) -> RadiantStatus:
        async with self._lock:
            try:
                return await self.client.read_status()

            except GizwitsError as err:
                raise UpdateFailed(
                    f"{self.client.host}: {err}"
                ) from err

    # ---------------------------------------------------------------- control

    async def async_set_power(
        self,
        on: bool,
    ) -> None:
        """Send the power command, then refresh."""

        async with self._lock:
            await self.client.send_power(on)

        await self.async_request_refresh()

    async def async_set_setpoint(
        self,
        temperature: float,
    ) -> None:
        """Send the setpoint command, then refresh."""

        async with self._lock:
            await self.client.send_setpoint(
                temperature
            )

        await self.async_request_refresh()

    # ------------------------------------------------------------ UDP push

    async def async_start_push(self) -> None:
        """Listen for thermostat UDP status broadcasts."""

        if not self.mac:
            return

        listener = await async_acquire_listener(
            self.hass
        )

        if listener is None:
            return

        self._listener_held = True

        self._unregister_push = listener.register(
            self.mac,
            self._handle_push,
        )

    @callback
    def _handle_push(
        self,
        status: RadiantStatus,
    ) -> None:
        if (
            self.data is None
            or status != self.data
        ):
            _LOGGER.debug(
                "%s: push update %s",
                self.did,
                status.raw.hex(" "),
            )

        self.async_set_updated_data(
            status
        )

    async def _async_stop_push(
        self,
    ) -> None:
        if self._unregister_push is not None:
            self._unregister_push()
            self._unregister_push = None

        if self._listener_held:
            self._listener_held = False
            await async_release_listener(
                self.hass
            )

    async def async_shutdown(self) -> None:
        await self._async_stop_push()
        await super().async_shutdown()