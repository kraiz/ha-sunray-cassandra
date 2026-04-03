"""Sunray / CaSSAndRA integration for Home Assistant."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import SunrayCassandraCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["lawn_mower", "select", "sensor", "switch"]

# Service names
SERVICE_MOW_TASK = "mow_task"
SERVICE_GO_TO = "go_to"
SERVICE_REBOOT = "reboot"
SERVICE_REBOOT_GPS = "reboot_gps"
SERVICE_SET_MOW_SPEED = "set_mow_speed"
SERVICE_TOGGLE_MOW_MOTOR = "toggle_mow_motor"

ATTR_TASK = "task"
ATTR_X = "x"
ATTR_Y = "y"
ATTR_SPEED = "speed"

_SERVICE_SCHEMA_MOW_TASK = vol.Schema({
    vol.Optional(ATTR_TASK, default="all"): cv.string,
})
_SERVICE_SCHEMA_GO_TO = vol.Schema({
    vol.Required(ATTR_X): vol.Coerce(float),
    vol.Required(ATTR_Y): vol.Coerce(float),
})
_SERVICE_SCHEMA_SET_MOW_SPEED = vol.Schema({
    vol.Required(ATTR_SPEED): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
})


def _get_coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> SunrayCassandraCoordinator | None:
    """Return the coordinator for the first targeted lawn_mower entity, or the only loaded entry."""
    # Try to find a coordinator from the entity context
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]

    if entity_ids:
        for entry_id, entry_data in hass.data.get(DOMAIN, {}).items():
            coord: SunrayCassandraCoordinator = entry_data[DATA_COORDINATOR]
            for eid in entity_ids:
                if coord.server_name in eid:
                    return coord

    # Fallback: if only one entry is configured, use that
    entries = list(hass.data.get(DOMAIN, {}).values())
    if len(entries) == 1:
        return entries[0][DATA_COORDINATOR]

    _LOGGER.warning(
        "sunray_cassandra service called without a target entity. "
        "Target a lawn_mower entity when multiple CaSSAndRA instances exist."
    )
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sunray / CaSSAndRA from a config entry."""
    coordinator = SunrayCassandraCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register custom services (idempotent — only on first entry)
    if not hass.services.has_service(DOMAIN, SERVICE_MOW_TASK):
        _register_services(hass)

    # Re-reload on options update
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register integration-specific services."""

    async def _handle_mow_task(call: ServiceCall) -> None:
        coord = _get_coordinator_for_call(hass, call)
        if coord:
            task = call.data.get(ATTR_TASK, "all")
            await coord.async_publish_command(
                {"robot": {"command": "mow", "value": [task]}}
            )

    async def _handle_go_to(call: ServiceCall) -> None:
        coord = _get_coordinator_for_call(hass, call)
        if coord:
            await coord.async_publish_command(
                {"robot": {
                    "command": "goTo",
                    "value": [{"x": call.data[ATTR_X], "y": call.data[ATTR_Y]}],
                }}
            )

    async def _handle_reboot(call: ServiceCall) -> None:
        coord = _get_coordinator_for_call(hass, call)
        if coord:
            await coord.async_publish_command({"robot": {"command": "reboot"}})

    async def _handle_reboot_gps(call: ServiceCall) -> None:
        coord = _get_coordinator_for_call(hass, call)
        if coord:
            await coord.async_publish_command({"robot": {"command": "rebootGps"}})

    async def _handle_set_mow_speed(call: ServiceCall) -> None:
        coord = _get_coordinator_for_call(hass, call)
        if coord:
            await coord.async_publish_command(
                {"robot": {"command": "setMowSpeed", "value": [call.data[ATTR_SPEED]]}}
            )

    async def _handle_toggle_mow_motor(call: ServiceCall) -> None:
        coord = _get_coordinator_for_call(hass, call)
        if coord:
            await coord.async_publish_command({"robot": {"command": "toggleMowMotor"}})

    hass.services.async_register(DOMAIN, SERVICE_MOW_TASK, _handle_mow_task, schema=_SERVICE_SCHEMA_MOW_TASK)
    hass.services.async_register(DOMAIN, SERVICE_GO_TO, _handle_go_to, schema=_SERVICE_SCHEMA_GO_TO)
    hass.services.async_register(DOMAIN, SERVICE_REBOOT, _handle_reboot)
    hass.services.async_register(DOMAIN, SERVICE_REBOOT_GPS, _handle_reboot_gps)
    hass.services.async_register(DOMAIN, SERVICE_SET_MOW_SPEED, _handle_set_mow_speed, schema=_SERVICE_SCHEMA_SET_MOW_SPEED)
    hass.services.async_register(DOMAIN, SERVICE_TOGGLE_MOW_MOTOR, _handle_toggle_mow_motor)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update – reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: SunrayCassandraCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
        await coordinator.async_teardown()
        hass.data[DOMAIN].pop(entry.entry_id)

        # Remove services when the last entry is gone
        if not hass.data.get(DOMAIN):
            for svc in (
                SERVICE_MOW_TASK, SERVICE_GO_TO, SERVICE_REBOOT,
                SERVICE_REBOOT_GPS, SERVICE_SET_MOW_SPEED, SERVICE_TOGGLE_MOW_MOTOR,
            ):
                hass.services.async_remove(DOMAIN, svc)

    return unload_ok
