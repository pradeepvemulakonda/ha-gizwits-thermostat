"""One shared UDP listener for all thermostats (port 12414)."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .gizwits_lan import StatusBroadcastListener

_LOGGER = logging.getLogger(__name__)
_KEY = "udp_listener"


async def async_acquire_listener(hass: HomeAssistant) -> StatusBroadcastListener | None:
    """Return the shared listener (starting it if needed), or None if the port cannot be used."""
    store = hass.data.setdefault(DOMAIN, {})
    listener: StatusBroadcastListener | None = store.get(_KEY)
    if listener is None:
        listener = StatusBroadcastListener()
        try:
            await listener.start()
        except OSError as err:
            _LOGGER.warning(
                "Cannot listen for thermostat broadcasts on UDP 12414 (%s); falling back to polling only", err
            )
            return None
        store[_KEY] = listener
    listener.refs += 1
    return listener


async def async_release_listener(hass: HomeAssistant) -> None:
    store = hass.data.get(DOMAIN, {})
    listener: StatusBroadcastListener | None = store.get(_KEY)
    if listener is None:
        return
    listener.refs -= 1
    if listener.refs <= 0:
        listener.close()
        store.pop(_KEY, None)
