"""Lawn mower platform for Sunray / CaSSAndRA."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DATA_COORDINATOR,
    DOMAIN,
    ROBOT_STATUS_CHARGING,
    ROBOT_STATUS_DOCKED,
    ROBOT_STATUS_DOCKING,
    ROBOT_STATUS_ERROR,
    ROBOT_STATUS_IDLE,
    ROBOT_STATUS_MOW,
    ROBOT_STATUS_MOVE,
    ROBOT_STATUS_OFFLINE,
    ROBOT_STATUS_RESUME,
    ROBOT_STATUS_TRANSIT,
)
from .coordinator import SunrayCassandraCoordinator
from .select import TASK_ALL, SunrayCassandraTaskSelect

_LOGGER = logging.getLogger(__name__)

# Map CaSSAndRA robot status strings to HA LawnMowerActivity states
_STATUS_TO_ACTIVITY: dict[str, LawnMowerActivity] = {
    ROBOT_STATUS_MOW: LawnMowerActivity.MOWING,
    ROBOT_STATUS_TRANSIT: LawnMowerActivity.MOWING,     # moving between mow sections
    ROBOT_STATUS_RESUME: LawnMowerActivity.MOWING,
    ROBOT_STATUS_MOVE: LawnMowerActivity.MOWING,        # remote jog
    ROBOT_STATUS_DOCKED: LawnMowerActivity.DOCKED,
    ROBOT_STATUS_CHARGING: LawnMowerActivity.DOCKED,
    ROBOT_STATUS_DOCKING: LawnMowerActivity.RETURNING,
    ROBOT_STATUS_IDLE: LawnMowerActivity.PAUSED,
    ROBOT_STATUS_ERROR: LawnMowerActivity.ERROR,
    ROBOT_STATUS_OFFLINE: LawnMowerActivity.ERROR,
    "unknown": LawnMowerActivity.ERROR,
    "map upload": LawnMowerActivity.PAUSED,
    "reboot": LawnMowerActivity.PAUSED,
    "shutdown": LawnMowerActivity.PAUSED,
    "gps reboot": LawnMowerActivity.PAUSED,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the lawn mower entity."""
    coordinator: SunrayCassandraCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    async_add_entities([SunrayCassandraLawnMower(coordinator, entry)])


class SunrayCassandraLawnMower(LawnMowerEntity):
    """Representation of a CaSSAndRA-controlled Ardumower as a HA lawn_mower entity."""

    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )
    _attr_has_entity_name = True
    _attr_name = None  # uses device name directly

    def __init__(
        self,
        coordinator: SunrayCassandraCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.server_name}_mower"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.server_name)},
            name=f"CaSSAndRA – {coordinator.server_name}",
            manufacturer="Ardumower / CaSSAndRA",
            model="Sunray Firmware",
            sw_version=coordinator.data.get("server", {}).get("version"),
            configuration_url=entry.data.get("cassandra_url") or None,
        )
        self._unsubscribe: callback | None = None

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        self._unsubscribe = self._coordinator.async_add_listener(self._handle_update)
        self._handle_update()

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe."""
        if self._unsubscribe:
            self._unsubscribe()

    @callback
    def _handle_update(self) -> None:
        """Refresh state from coordinator data."""
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def activity(self) -> LawnMowerActivity | None:
        """Return current mower activity mapped from CaSSAndRA status."""
        status = self._coordinator.robot.get("status", "")
        return _STATUS_TO_ACTIVITY.get(status)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Additional state attributes exposed on the entity."""
        robot = self._coordinator.robot
        attrs: dict[str, Any] = {}

        if robot:
            attrs["cassandra_status"] = robot.get("status")
            attrs["dock_reason"] = robot.get("dockReason")
            attrs["firmware"] = robot.get("firmware")
            attrs["firmware_version"] = robot.get("version")
            attrs["sensor_state"] = robot.get("sensorState")

            pos = robot.get("position", {})
            if pos:
                attrs["position_x"] = pos.get("x")
                attrs["position_y"] = pos.get("y")

            gps = robot.get("gps", {})
            if gps:
                attrs["gps_solution"] = gps.get("solution")
                attrs["gps_visible_satellites"] = gps.get("visible")
                attrs["gps_dgps"] = gps.get("dgps")
                attrs["gps_age"] = gps.get("age")

            attrs["mow_point_index"] = robot.get("mowPointIdx")
            attrs["speed"] = robot.get("speed")
            attrs["average_speed"] = robot.get("averageSpeed")
            attrs["mow_motor_active"] = robot.get("mowMotorActive")
            attrs["angle"] = robot.get("angle")

        tasks = self._coordinator.data.get("tasks", {})
        if tasks:
            attrs["selected_tasks"] = tasks.get("selected", [])
            attrs["loaded_task"] = tasks.get("loaded", [])
            attrs["available_tasks"] = tasks.get("available", [])

        return attrs

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_start_mowing(self) -> None:
        """Start mowing.

        Decision order:
        1. If the mower is already mid-task (mowing/transit), resume it.
        2. If a specific task is chosen in the Task select entity, select it
           on CaSSAndRA first, then send mow with value ["task"].
        3. Otherwise mow all area.
        """
        current_status = self._coordinator.robot.get("status", "")

        # If already actively mowing, just resume where it left off
        if current_status not in (
            ROBOT_STATUS_IDLE, ROBOT_STATUS_DOCKED, ROBOT_STATUS_CHARGING
        ):
            await self._coordinator.async_publish_command(
                {"robot": {"command": "mow", "value": ["resume"]}}
            )
            return

        task_name = self._get_selected_task_value()
        if task_name != "all":
            # Step 1: tell CaSSAndRA which task to use
            await self._coordinator.async_publish_command(
                {"tasks": {"command": "select", "value": [task_name]}}
            )
            # Step 2: start mowing that task (CaSSAndRA reads its own selection)
            await self._coordinator.async_publish_command(
                {"robot": {"command": "mow", "value": ["task"]}}
            )
        else:
            await self._coordinator.async_publish_command(
                {"robot": {"command": "mow", "value": ["all"]}}
            )

    def _get_selected_task_value(self) -> str:
        """Return the task value from the companion select entity, or 'all'."""
        if self.hass is None:
            return "all"
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(self.hass)
        select_unique_id = f"{self._coordinator.server_name}_task_select"
        entity_id = registry.async_get_entity_id("select", DOMAIN, select_unique_id)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state and state.state not in ("unknown", "unavailable", TASK_ALL):
                return state.state
        return "all"

    async def async_pause(self) -> None:
        """Pause / stop mowing (CaSSAndRA uses 'stop' for pause)."""
        await self._coordinator.async_publish_command(
            {"robot": {"command": "stop"}}
        )

    async def async_dock(self) -> None:
        """Send the mower back to its dock."""
        await self._coordinator.async_publish_command(
            {"robot": {"command": "dock"}}
        )
