"""Climate entity: power, setpoint and floor temperature - all local."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
)

from .coordinator import (
    RadiantConfigEntry,
    RadiantCoordinator,
)
from .entity import RadiantEntity
from .gizwits_lan import GizwitsError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RadiantConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the thermostat climate entity."""

    async_add_entities(
        [RadiantClimate(entry.runtime_data)]
    )


class RadiantClimate(
    RadiantEntity,
    ClimateEntity,
):
    """The thermostat.

    * on/off is a confirmed local write
      (0x93 sub-command 0x05 0x0a)
    * hvac_action HEATING/IDLE follows flag bit 3
      (the app's famen1 / valve 1)
    * target temperature uses the confirmed
      0x93 05 0e 01 <value> command
    * current_temperature is the floor sensor;
      the air sensor is a separate sensor entity
    """

    _attr_name = None

    _attr_temperature_unit = (
        UnitOfTemperature.CELSIUS
    )

    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT,
    ]

    _attr_target_temperature_step = 0.5

    _attr_supported_features = (
        ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TARGET_TEMPERATURE
    )

    def __init__(
        self,
        coordinator: RadiantCoordinator,
    ) -> None:
        super().__init__(
            coordinator,
            "climate",
        )

    @property
    def hvac_mode(
        self,
    ) -> HVACMode | None:
        if self.coordinator.data is None:
            return None

        return (
            HVACMode.HEAT
            if self.coordinator.data.power
            else HVACMode.OFF
        )

    @property
    def hvac_action(
        self,
    ) -> HVACAction | None:
        data = self.coordinator.data

        if data is None:
            return None

        if not data.power:
            return HVACAction.OFF

        return (
            HVACAction.HEATING
            if data.valve1
            else HVACAction.IDLE
        )

    @property
    def current_temperature(
        self,
    ) -> float | None:
        return (
            self.coordinator.data.floor_temp
            if self.coordinator.data
            else None
        )

    @property
    def target_temperature(
        self,
    ) -> float | None:
        return (
            self.coordinator.data.setpoint
            if self.coordinator.data
            else None
        )

    async def async_set_hvac_mode(
        self,
        hvac_mode: HVACMode,
    ) -> None:
        await self._set_power(
            hvac_mode == HVACMode.HEAT
        )

    async def async_turn_on(
        self,
    ) -> None:
        await self._set_power(True)

    async def async_turn_off(
        self,
    ) -> None:
        await self._set_power(False)

    async def async_set_temperature(
        self,
        **kwargs: Any,
    ) -> None:
        """Set the thermostat target temperature."""

        temperature = kwargs.get(
            "temperature"
        )

        if temperature is None:
            raise HomeAssistantError(
                "No target temperature was supplied"
            )

        try:
            await self.coordinator.async_set_setpoint(
                float(temperature)
            )

        except (
            GizwitsError,
            ValueError,
        ) as err:
            raise HomeAssistantError(
                f"Could not set thermostat "
                f"temperature: {err}"
            ) from err

    async def _set_power(
        self,
        on: bool,
    ) -> None:
        try:
            await self.coordinator.async_set_power(
                on
            )

        except GizwitsError as err:
            raise HomeAssistantError(
                f"Could not reach the thermostat: "
                f"{err}"
            ) from err

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        data = self.coordinator.data

        return (
            {}
            if data is None
            else {
                "device_clock": data.device_time,
                "weekday": data.weekday,
            }
        )