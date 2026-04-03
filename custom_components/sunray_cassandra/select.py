"""Select platform for Sunray / CaSSAndRA.

Provides a task-picker select entity that:
  - Lists all saved CaSSAndRA tasks as options (updated live via MQTT).
  - Reflects which task is currently selected on the CaSSAndRA server.
  - On selection, sends the CaSSAndRA "select" command so the server knows
    which task to run next.  Does NOT start mowing immediately.
  - Selecting the special option "— mow all —" (TASK_ALL) clears any task
    selection and will cause the next start_mowing call to mow the full area.

The lawn_mower entity's async_start_mowing reads back from this select entity
so the UI flow is:
  1. Pick a task in the select dropdown.
  2. Press Start on the mower card  →  runs that task.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import SunrayCassandraCoordinator

_LOGGER = logging.getLogger(__name__)

# Synthetic option that means "mow all area, no specific task"
TASK_ALL = "— mow all —"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the task picker select entity."""
    coordinator: SunrayCassandraCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    async_add_entities([SunrayCassandraTaskSelect(coordinator)])


class SunrayCassandraTaskSelect(SelectEntity):
    """A select entity whose options are the saved CaSSAndRA tasks.

    Selecting an option immediately sends the CaSSAndRA "select task" command
    so the server is in sync.  The lawn_mower entity reads the current_option
    back when start_mowing is called.
    """

    _attr_has_entity_name = True
    _attr_name = "Task"
    _attr_icon = "mdi:map-check-outline"

    # The select entity should always reflect the *current* server state, so
    # we deliberately do NOT persist the selected value in HA storage.
    _attr_should_poll = False

    def __init__(self, coordinator: SunrayCassandraCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.server_name}_task_select"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.server_name)},
        )
        self._unsubscribe: callback | None = None

        # Initialise from whatever the coordinator already has
        self._update_options_and_current()

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        self._unsubscribe = self._coordinator.async_add_listener(self._handle_update)
        self._handle_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()

    @callback
    def _handle_update(self) -> None:
        """Re-derive options and current value from coordinator data."""
        self._update_options_and_current()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_options_and_current(self) -> None:
        """Rebuild the option list and figure out the current selection."""
        tasks: dict[str, Any] = self._coordinator.data.get("tasks", {})
        available: list[str] = tasks.get("available", [])

        # Always put TASK_ALL first so the user can un-select any specific task
        self._attr_options = [TASK_ALL] + sorted(available)

        # Prefer the "selected" list from CaSSAndRA (what the server considers active)
        selected: list[str] = tasks.get("selected", [])
        if selected and selected[0] in available:
            self._attr_current_option = selected[0]
        else:
            self._attr_current_option = TASK_ALL

    # ------------------------------------------------------------------
    # User interaction
    # ------------------------------------------------------------------

    async def async_select_option(self, option: str) -> None:
        """Called when the user picks an option in the HA UI."""
        if option == TASK_ALL:
            # Nothing to tell CaSSAndRA — just reflect locally
            self._attr_current_option = TASK_ALL
            self.async_write_ha_state()
            return

        if option not in self._attr_options:
            _LOGGER.warning("Task '%s' is not in the available task list", option)
            return

        _LOGGER.debug("Selecting CaSSAndRA task: %s", option)
        # Tell CaSSAndRA to mark this task as selected (does not start mowing)
        await self._coordinator.async_publish_command(
            {"tasks": {"command": "select", "value": [option]}}
        )
        # Optimistically update local state; the MQTT echo will confirm it
        self._attr_current_option = option
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Property read by lawn_mower.py
    # ------------------------------------------------------------------

    @property
    def selected_task_value(self) -> str:
        """Return the value to pass to the mow command.

        Returns the task name, or "all" when TASK_ALL is selected.
        """
        opt = self._attr_current_option
        if opt is None or opt == TASK_ALL:
            return "all"
        return opt
