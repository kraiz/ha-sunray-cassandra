"""Switch platform for Sunray / CaSSAndRA.

Provides:
  - Schedule switch – enables / disables the CaSSAndRA weekly mow schedule.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import SunrayCassandraCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: SunrayCassandraCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    async_add_entities([SunrayCassandraScheduleSwitch(coordinator)])


class SunrayCassandraScheduleSwitch(SwitchEntity):
    """Switch to enable / disable the CaSSAndRA mow schedule."""

    _attr_has_entity_name = True
    _attr_name = "Schedule"
    _attr_icon = "mdi:calendar-clock"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: SunrayCassandraCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.server_name}_schedule"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.server_name)},
        )
        self._unsubscribe: callback | None = None

    async def async_added_to_hass(self) -> None:
        self._unsubscribe = self._coordinator.async_add_listener(self._handle_update)
        self._handle_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """Return True when the CaSSAndRA schedule is active."""
        schedule = self._coordinator.data.get("schedule", {})
        return schedule.get("scheduleActive")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate the CaSSAndRA schedule."""
        await self._set_schedule_active(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Deactivate the CaSSAndRA schedule."""
        await self._set_schedule_active(False)

    async def _set_schedule_active(self, active: bool) -> None:
        """Send schedule update command preserving existing time ranges / tasks."""
        schedule = dict(self._coordinator.data.get("schedule", {}))
        schedule["scheduleActive"] = active
        await self._coordinator.async_publish_command(
            {"schedule": {"command": "save", "value": schedule}}
        )
