"""Sensor platform for Sunray / CaSSAndRA.

Provides the following sensors for each CaSSAndRA instance:
  - Battery SOC (%)
  - Battery voltage (V)
  - Battery current (A)
  - Error / sensor state (text)
  - GPS quality (fix / float / invalid)
  - GPS visible satellites (count)
  - Mow progress (%)
  - Current speed (m/s)
  - Average speed (m/s)
  - Position X / Y (m)
  - Dock reason (text)
  - CaSSAndRA server: version, CPU load, CPU temp, RAM usage, HDD usage
  - API status (boot / ready / busy / offline)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import SunrayCassandraCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SunraySensorEntityDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with a value extractor."""

    value_fn: Any = None  # Callable[[dict, dict], Any]  – (robot_data, full_data)


def _robot(key: str, fallback: Any = None):
    """Return a value extractor that reads from robot telemetry."""
    def _fn(robot: dict, data: dict) -> Any:
        return robot.get(key, fallback)
    return _fn


def _robot_nested(*keys: str):
    """Return an extractor that reads a nested key from robot telemetry."""
    def _fn(robot: dict, data: dict) -> Any:
        obj = robot
        for k in keys:
            if not isinstance(obj, dict):
                return None
            obj = obj.get(k)
        return obj
    return _fn


def _server(key: str, fallback: Any = None):
    def _fn(robot: dict, data: dict) -> Any:
        return data.get("server", {}).get(key, fallback)
    return _fn


def _mow_progress(robot: dict, data: dict) -> float | None:
    # CaSSAndRA publishes mow progress on the `map` topic (not `robot`).
    # `mowprogressIdxPercent` is an integer 0-100 based on mow point index.
    val = data.get("map", {}).get("mowprogressIdxPercent")
    if val is not None:
        return round(float(val), 1)
    return None


SENSOR_DESCRIPTIONS: tuple[SunraySensorEntityDescription, ...] = (
    # ---- Battery --------------------------------------------------------
    SunraySensorEntityDescription(
        key="battery_soc",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_robot_nested("battery", "soc"),
        suggested_display_precision=0,
    ),
    SunraySensorEntityDescription(
        key="battery_voltage",
        name="Battery Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_robot_nested("battery", "voltage"),
        suggested_display_precision=1,
    ),
    SunraySensorEntityDescription(
        key="battery_current",
        name="Battery Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_robot_nested("battery", "electricCurrent"),
        suggested_display_precision=2,
    ),
    # ---- Current task ---------------------------------------------------
    SunraySensorEntityDescription(
        key="current_task",
        name="Current Task",
        icon="mdi:map-check-outline",
        # `loaded` is set by CaSSAndRA only while actively mowing a task;
        # fall back to `selected[0]` so the sensor shows the queued task name
        # even before mowing has started.
        value_fn=lambda robot, data: (
            (data.get("tasks", {}).get("loaded") or [None])[0]
            or (data.get("tasks", {}).get("selected") or [None])[0]
        ),
    ),
    # ---- Error / sensor state -------------------------------------------
    SunraySensorEntityDescription(
        key="sensor_state",
        name="Sensor State",
        icon="mdi:alert-circle-outline",
        value_fn=_robot("sensorState", "no error"),
    ),
    SunraySensorEntityDescription(
        key="dock_reason",
        name="Dock Reason",
        icon="mdi:home-clock",
        value_fn=_robot("dockReason"),
    ),
    # ---- GPS ------------------------------------------------------------
    SunraySensorEntityDescription(
        key="gps_quality",
        name="GPS Quality",
        icon="mdi:satellite-variant",
        value_fn=_robot_nested("gps", "solution"),
    ),
    SunraySensorEntityDescription(
        key="gps_satellites",
        name="GPS Satellites",
        icon="mdi:satellite-uplink",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_robot_nested("gps", "visible"),
        suggested_display_precision=0,
    ),
    # ---- Mowing progress ------------------------------------------------
    SunraySensorEntityDescription(
        key="mow_progress",
        name="Mow Progress",
        icon="mdi:map-check",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_mow_progress,
        suggested_display_precision=1,
    ),
    # ---- Speed ----------------------------------------------------------
    SunraySensorEntityDescription(
        key="speed",
        name="Speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        value_fn=_robot("speed"),
        suggested_display_precision=2,
    ),
    SunraySensorEntityDescription(
        key="average_speed",
        name="Average Speed",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_robot("averageSpeed"),
        suggested_display_precision=2,
    ),
    # ---- Position -------------------------------------------------------
    SunraySensorEntityDescription(
        key="position_x",
        name="Position X",
        icon="mdi:map-marker",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="m",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_robot_nested("position", "x"),
        suggested_display_precision=2,
    ),
    SunraySensorEntityDescription(
        key="position_y",
        name="Position Y",
        icon="mdi:map-marker",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="m",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_robot_nested("position", "y"),
        suggested_display_precision=2,
    ),
    # ---- CaSSAndRA server -----------------------------------------------
    SunraySensorEntityDescription(
        key="server_version",
        name="Server Version",
        icon="mdi:information-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_server("version"),
    ),
    SunraySensorEntityDescription(
        key="server_cpu_load",
        name="Server CPU Load",
        icon="mdi:cpu-64-bit",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_server("cpuLoad"),
        suggested_display_precision=1,
    ),
    SunraySensorEntityDescription(
        key="server_cpu_temp",
        name="Server CPU Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_server("cpuTemp"),
        suggested_display_precision=1,
    ),
    SunraySensorEntityDescription(
        key="server_mem_usage",
        name="Server Memory Usage",
        icon="mdi:memory",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_server("memUsage"),
        suggested_display_precision=1,
    ),
    SunraySensorEntityDescription(
        key="server_hdd_usage",
        name="Server Disk Usage",
        icon="mdi:harddisk",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_server("hddUsage"),
        suggested_display_precision=1,
    ),
    # ---- API status ------------------------------------------------------
    SunraySensorEntityDescription(
        key="api_status",
        name="API Status",
        icon="mdi:api",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda robot, data: data.get("api_status"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities."""
    coordinator: SunrayCassandraCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    async_add_entities(
        SunrayCassandraSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )


class SunrayCassandraSensor(SensorEntity):
    """A sensor entity for a single CaSSAndRA data point."""

    entity_description: SunraySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SunrayCassandraCoordinator,
        description: SunraySensorEntityDescription,
    ) -> None:
        self.entity_description = description
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.server_name}_{description.key}"
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
    def native_value(self) -> Any:
        """Return the sensor value using the description's extractor."""
        try:
            return self.entity_description.value_fn(
                self._coordinator.robot,
                self._coordinator.data,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Error extracting value for sensor '%s'",
                self.entity_description.key,
                exc_info=True,
            )
            return None
