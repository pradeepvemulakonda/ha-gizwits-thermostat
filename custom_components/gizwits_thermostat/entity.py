"""Shared base entity."""
from __future__ import annotations

from homeassistant.const import CONF_NAME
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MAC, DOMAIN
from .coordinator import RadiantCoordinator


class RadiantEntity(CoordinatorEntity[RadiantCoordinator]):
    """Base class: device info + naming."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RadiantCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{coordinator.did}_{key}"
        info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.did)},
            manufacturer="Radiant",
            model="Wi-Fi floor heating thermostat (Gizwits GAgent)",
            name=entry.data.get(CONF_NAME) or entry.title,
        )
        if mac := entry.data.get(CONF_MAC):
            info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(mac))}
        self._attr_device_info = info
