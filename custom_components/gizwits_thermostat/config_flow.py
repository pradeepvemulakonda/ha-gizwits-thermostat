"""Config flow: add a thermostat by IP address. Everything is local; no cloud accounts needed."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_DID, CONF_MAC, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, MIN_SCAN_INTERVAL
from .gizwits_lan import GizwitsError, GizwitsLanClient, discover

_LOGGER = logging.getLogger(__name__)


class RadiantConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a thermostat."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                devices = await discover(host, timeout=4)
                device = next((d for d in devices if d.ip == host), None)
                if device is None:
                    raise GizwitsError("no discovery reply from that address")
                await GizwitsLanClient(host).check_login()
            except GizwitsError as err:
                _LOGGER.debug("Cannot add %s: %s", host, err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(device.did)
                # If the IP changed, update the stored host instead of adding a duplicate
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                name = user_input.get(CONF_NAME) or f"Radiant thermostat {host}"
                return self.async_create_entry(
                    title=name,
                    data={CONF_HOST: host, CONF_NAME: name, CONF_DID: device.did, CONF_MAC: device.mac},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_NAME, default="Radiant thermostat"): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RadiantOptionsFlow()


class RadiantOptionsFlow(OptionsFlow):
    """Just the polling interval - UDP push handles instant updates on its own."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])})

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=1800,
                        step=10,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
