"""Sensors read locally from the thermostat."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import RadiantConfigEntry, RadiantCoordinator
from .entity import RadiantEntity
from .gizwits_lan import RadiantStatus


@dataclass(frozen=True, kw_only=True)
class RadiantSensorDescription(SensorEntityDescription):
    value_fn: Callable[[RadiantStatus], StateType]


_TEMP = dict(
    device_class=SensorDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
)

SENSORS: tuple[RadiantSensorDescription, ...] = (
    RadiantSensorDescription(
        key="floor_temperature", name="Floor temperature", value_fn=lambda s: s.floor_temp, **_TEMP
    ),
    RadiantSensorDescription(
        key="setpoint", name="Setpoint", value_fn=lambda s: s.setpoint, **_TEMP
    ),
    RadiantSensorDescription(
        key="air_temperature",
        name="Air temperature",
        value_fn=lambda s: s.air_temp,
        **_TEMP,
    ),
    RadiantSensorDescription(
        key="device_time",
        name="Device clock",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.device_time,
    ),
    RadiantSensorDescription(
        key="raw_status",
        name="Raw status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.raw.hex(" "),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: RadiantConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(RadiantSensor(coordinator, desc) for desc in SENSORS)


class RadiantSensor(RadiantEntity, SensorEntity):
    entity_description: RadiantSensorDescription

    def __init__(self, coordinator: RadiantCoordinator, description: RadiantSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
