"""Binary sensors decoded from the local status frame."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import RadiantConfigEntry, RadiantCoordinator
from .entity import RadiantEntity
from .gizwits_lan import RadiantStatus


@dataclass(frozen=True, kw_only=True)
class RadiantBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[RadiantStatus], bool]


BINARY_SENSORS: tuple[RadiantBinarySensorDescription, ...] = (
    RadiantBinarySensorDescription(
        key="power",
        name="Power",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda s: s.power,
    ),
    # Bit 3 of the flag byte = the app's `famen1` (valve 1); it has come on together with power.
    RadiantBinarySensorDescription(
        key="valve1",
        name="Valve 1",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda s: s.valve1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: RadiantConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(RadiantBinarySensor(coordinator, desc) for desc in BINARY_SENSORS)


class RadiantBinarySensor(RadiantEntity, BinarySensorEntity):
    entity_description: RadiantBinarySensorDescription

    def __init__(
        self, coordinator: RadiantCoordinator, description: RadiantBinarySensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
