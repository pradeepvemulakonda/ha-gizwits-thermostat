"""Radiant Wi-Fi Thermostat (Gizwits GAgent): fully local status + control."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

import logging

from .const import DOMAIN, SERVICE_SEND_RAW
from .coordinator import RadiantConfigEntry, RadiantCoordinator, parse_hex

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR, Platform.BINARY_SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_RAW_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("hex"): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the (advanced) send_raw service once."""

    async def _send_raw(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("Unknown or not loaded Radiant thermostat config entry")
        try:
            sub_payload = parse_hex(call.data["hex"])
        except ValueError as err:
            raise ServiceValidationError("'hex' is not valid hexadecimal") from err
        if not sub_payload:
            raise ServiceValidationError("'hex' is empty")
        coordinator: RadiantCoordinator = entry.runtime_data
        async with coordinator.client as session:
            ack = await session._control(sub_payload, timeout=10)  # noqa: SLF001 (advanced/diagnostic use)
        _LOGGER.warning("send_raw to %s: sent %s, ack %s", coordinator.did, sub_payload.hex(" "), ack.hex(" "))

    hass.services.async_register(DOMAIN, SERVICE_SEND_RAW, _send_raw, schema=SEND_RAW_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RadiantConfigEntry) -> bool:
    coordinator = RadiantCoordinator(hass, entry)
    # Raises ConfigEntryNotReady if the thermostat cannot be read (HA retries automatically)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await coordinator.async_start_push()
    entry.async_on_unload(lambda: hass.async_create_task(coordinator.async_shutdown()))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: RadiantConfigEntry) -> None:
    """Reload when options (e.g. the polling interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RadiantConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
