"""Data coordinator for the Sunray / CaSSAndRA integration.

Manages two communication channels:
1. MQTT (primary, push-based) – subscribes to CaSSAndRA's MQTT API topics.
2. HTTP (optional fallback) – polls the CaSSAndRA REST-like status endpoint
   when MQTT data has been stale for longer than STALE_TIMEOUT.

The coordinator is the single source of truth for all parsed data. Entities
register listeners via async_add_listener().
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.components import mqtt
from homeassistant.components.mqtt import async_subscribe as mqtt_subscribe
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    API_STATUS_OFFLINE,
    CONF_CASSANDRA_URL,
    CONF_MQTT_BROKER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_ORIGIN_LAT,
    CONF_ORIGIN_LON,
    CONF_SERVER_NAME,
    CONF_USE_HA_MQTT,
    DEFAULT_MQTT_PORT,
    DOMAIN,
    ROBOT_STATUS_OFFLINE,
    TOPIC_CMD,
    TOPIC_COORDS,
    TOPIC_MAP,
    TOPIC_MAPS,
    TOPIC_MOW_PARAMETERS,
    TOPIC_ROBOT,
    TOPIC_SCHEDULE,
    TOPIC_SERVER,
    TOPIC_SETTINGS,
    TOPIC_STATUS,
    TOPIC_TASKS,
)

_LOGGER = logging.getLogger(__name__)

# If no MQTT message has been received for this long, attempt HTTP fallback
STALE_TIMEOUT = timedelta(seconds=30)
# Interval for the HTTP polling fallback
HTTP_POLL_INTERVAL = timedelta(seconds=15)
# How often to refresh the mow-path coordinate data from CaSSAndRA
COORDS_REFRESH_INTERVAL = timedelta(seconds=30)


class SunrayCassandraCoordinator:
    """Central data coordinator for one CaSSAndRA instance."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._server_name: str = entry.data[CONF_SERVER_NAME]
        self._cassandra_url: str = entry.data.get(CONF_CASSANDRA_URL, "")
        self._use_ha_mqtt: bool = entry.data.get(CONF_USE_HA_MQTT, True)

        # Raw data storage – updated by MQTT callbacks / HTTP polling
        self.data: dict[str, Any] = {
            "api_status": API_STATUS_OFFLINE,
            "robot": {},
            "map": {},
            "maps": {},
            "tasks": {},
            "mow_parameters": {},
            "server": {},
            "schedule": {},
            "coords": {},   # GeoJSON FeatureCollections keyed by layer name
            "settings": {}, # CaSSAndRA settings (includes origin lat/lon)
        }

        # GPS origin – populated from config entry data or from MQTT settings response
        self.origin_lat: float = entry.data.get(CONF_ORIGIN_LAT, 0.0)
        self.origin_lon: float = entry.data.get(CONF_ORIGIN_LON, 0.0)

        self._listeners: list[callback] = []
        self._unsubscribe_mqtt: list[Any] = []
        self._unsub_poll: Any = None
        self._unsub_coords_poll: Any = None
        self._last_mqtt_message: datetime | None = None

        # External MQTT client (non-HA MQTT integration path)
        self._ext_mqtt_client: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def async_add_listener(self, update_callback: callback) -> callback:
        """Register a listener that is called whenever data changes.

        Returns an unregister callback.
        """
        self._listeners.append(update_callback)

        @callback
        def remove_listener() -> None:
            self._listeners.remove(update_callback)

        return remove_listener

    @callback
    def _notify_listeners(self) -> None:
        for listener in self._listeners:
            listener()

    # ------------------------------------------------------------------
    # Setup / Teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        """Set up MQTT subscriptions and optional HTTP polling."""
        if self._use_ha_mqtt:
            await self._subscribe_ha_mqtt()
        else:
            await self._connect_external_mqtt()

        # Request the CaSSAndRA settings once on startup so we can get the GPS origin
        await self._async_request_settings()

        # Periodically refresh the coordinate/map GeoJSON data
        self._unsub_coords_poll = async_track_time_interval(
            self.hass,
            self._async_request_coords,
            COORDS_REFRESH_INTERVAL,
        )
        # Also request an initial set of coords shortly after setup
        self.hass.async_create_task(self._async_request_coords())

        # Start the periodic HTTP fallback poll (only acts if MQTT is stale)
        if self._cassandra_url:
            self._unsub_poll = async_track_time_interval(
                self.hass,
                self._async_http_poll,
                HTTP_POLL_INTERVAL,
            )

    async def async_teardown(self) -> None:
        """Unsubscribe MQTT and cancel polling."""
        for unsub in self._unsubscribe_mqtt:
            unsub()
        self._unsubscribe_mqtt.clear()

        if self._unsub_poll:
            self._unsub_poll()
            self._unsub_poll = None

        if self._unsub_coords_poll:
            self._unsub_coords_poll()
            self._unsub_coords_poll = None

        if self._ext_mqtt_client:
            try:
                self._ext_mqtt_client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._ext_mqtt_client = None

    # ------------------------------------------------------------------
    # MQTT – HA integration path
    # ------------------------------------------------------------------

    async def _subscribe_ha_mqtt(self) -> None:
        """Subscribe to all CaSSAndRA topics via HA's MQTT integration."""
        topics = {
            TOPIC_STATUS: self._handle_status,
            TOPIC_ROBOT: self._handle_robot,
            TOPIC_MAP: self._handle_map,
            TOPIC_MAPS: self._handle_maps,
            TOPIC_TASKS: self._handle_tasks,
            TOPIC_MOW_PARAMETERS: self._handle_mow_parameters,
            TOPIC_SERVER: self._handle_server,
            TOPIC_SCHEDULE: self._handle_schedule,
            TOPIC_COORDS: self._handle_coords,
            TOPIC_SETTINGS: self._handle_settings,
        }
        for topic_template, handler in topics.items():
            topic = topic_template.format(server_name=self._server_name)
            unsub = await mqtt_subscribe(self.hass, topic, handler)
            self._unsubscribe_mqtt.append(unsub)
        _LOGGER.debug("Subscribed to CaSSAndRA MQTT topics for server '%s'", self._server_name)

    # ------------------------------------------------------------------
    # MQTT – External broker path (paho-mqtt)
    # ------------------------------------------------------------------

    async def _connect_external_mqtt(self) -> None:
        """Connect to an external MQTT broker using paho-mqtt."""
        try:
            import paho.mqtt.client as paho  # type: ignore[import]
        except ImportError:
            _LOGGER.error(
                "paho-mqtt is required for external MQTT broker support. "
                "Install it via pip or add it to requirements."
            )
            return

        entry_data = self.entry.data
        broker = entry_data.get(CONF_MQTT_BROKER, "localhost")
        port = int(entry_data.get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT))
        username = entry_data.get(CONF_MQTT_USERNAME)
        password = entry_data.get(CONF_MQTT_PASSWORD)

        client = paho.Client(client_id=f"ha_sunray_{self._server_name}")
        if username:
            client.username_pw_set(username, password)

        topic_map = {
            TOPIC_STATUS.format(server_name=self._server_name): self._handle_status_raw,
            TOPIC_ROBOT.format(server_name=self._server_name): self._handle_robot_raw,
            TOPIC_MAP.format(server_name=self._server_name): self._handle_map_raw,
            TOPIC_MAPS.format(server_name=self._server_name): self._handle_maps_raw,
            TOPIC_TASKS.format(server_name=self._server_name): self._handle_tasks_raw,
            TOPIC_MOW_PARAMETERS.format(server_name=self._server_name): self._handle_mow_parameters_raw,
            TOPIC_SERVER.format(server_name=self._server_name): self._handle_server_raw,
            TOPIC_SCHEDULE.format(server_name=self._server_name): self._handle_schedule_raw,
            TOPIC_COORDS.format(server_name=self._server_name): self._handle_coords_raw,
            TOPIC_SETTINGS.format(server_name=self._server_name): self._handle_settings_raw,
        }

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                _LOGGER.debug("External MQTT connected (broker=%s:%s)", broker, port)
                for topic in topic_map:
                    client.subscribe(topic)
            else:
                _LOGGER.warning("External MQTT connect failed: rc=%s", rc)

        def on_message(client, userdata, msg):
            handler = topic_map.get(msg.topic)
            if handler:
                payload = msg.payload.decode("utf-8", errors="replace")
                self.hass.loop.call_soon_threadsafe(handler, payload)

        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect_async(broker, port)
            client.loop_start()
            self._ext_mqtt_client = client
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Cannot connect to external MQTT broker: %s", exc)

    # ------------------------------------------------------------------
    # MQTT message handlers – HA MQTT path (receive MQTTMessage objects)
    # ------------------------------------------------------------------

    @callback
    def _handle_status(self, msg: Any) -> None:
        self._handle_status_raw(msg.payload)

    @callback
    def _handle_robot(self, msg: Any) -> None:
        self._handle_robot_raw(msg.payload)

    @callback
    def _handle_map(self, msg: Any) -> None:
        self._handle_map_raw(msg.payload)

    @callback
    def _handle_maps(self, msg: Any) -> None:
        self._handle_maps_raw(msg.payload)

    @callback
    def _handle_tasks(self, msg: Any) -> None:
        self._handle_tasks_raw(msg.payload)

    @callback
    def _handle_mow_parameters(self, msg: Any) -> None:
        self._handle_mow_parameters_raw(msg.payload)

    @callback
    def _handle_server(self, msg: Any) -> None:
        self._handle_server_raw(msg.payload)

    @callback
    def _handle_schedule(self, msg: Any) -> None:
        self._handle_schedule_raw(msg.payload)

    @callback
    def _handle_coords(self, msg: Any) -> None:
        self._handle_coords_raw(msg.payload)

    @callback
    def _handle_settings(self, msg: Any) -> None:
        self._handle_settings_raw(msg.payload)

    # ------------------------------------------------------------------
    # Raw payload handlers (accept str or bytes)
    # ------------------------------------------------------------------

    def _handle_status_raw(self, payload: str | bytes) -> None:
        self._last_mqtt_message = datetime.utcnow()
        status = payload if isinstance(payload, str) else payload.decode("utf-8", errors="replace")
        status = status.strip()
        # CaSSAndRA sends ".", "..", "..." as busy-spinners — ignore them
        if set(status) == {"."} :
            return
        self.data["api_status"] = status
        self._notify_listeners()

    def _handle_robot_raw(self, payload: str | bytes) -> None:
        self._last_mqtt_message = datetime.utcnow()
        robot = self._parse_json(payload, "robot")
        # CaSSAndRA uses dot-spinners as busy indicators in string fields too;
        # retain the last real value so they don't pollute sensor history.
        prev = self.data.get("robot", {})
        for field in ("sensorState", "status", "dockReason"):
            val = robot.get(field)
            if isinstance(val, str) and val and set(val.strip()) == {"."}:
                if field in prev:
                    robot[field] = prev[field]
                else:
                    robot.pop(field, None)
        self.data["robot"] = robot
        self._notify_listeners()

    def _handle_map_raw(self, payload: str | bytes) -> None:
        self._last_mqtt_message = datetime.utcnow()
        self.data["map"] = self._parse_json(payload, "map")
        self._notify_listeners()

    def _handle_maps_raw(self, payload: str | bytes) -> None:
        self._last_mqtt_message = datetime.utcnow()
        self.data["maps"] = self._parse_json(payload, "maps")
        self._notify_listeners()

    def _handle_tasks_raw(self, payload: str | bytes) -> None:
        self._last_mqtt_message = datetime.utcnow()
        self.data["tasks"] = self._parse_json(payload, "tasks")
        self._notify_listeners()

    def _handle_mow_parameters_raw(self, payload: str | bytes) -> None:
        self._last_mqtt_message = datetime.utcnow()
        self.data["mow_parameters"] = self._parse_json(payload, "mow_parameters")
        self._notify_listeners()

    def _handle_server_raw(self, payload: str | bytes) -> None:
        self._last_mqtt_message = datetime.utcnow()
        self.data["server"] = self._parse_json(payload, "server")
        self._notify_listeners()

    def _handle_schedule_raw(self, payload: str | bytes) -> None:
        self._last_mqtt_message = datetime.utcnow()
        self.data["schedule"] = self._parse_json(payload, "schedule")
        self._notify_listeners()

    def _handle_coords_raw(self, payload: str | bytes) -> None:
        """Handle a GeoJSON FeatureCollection published on the coords topic.

        CaSSAndRA publishes one message per requested layer (currentMap, mowPath,
        obstacles, preview).  Each message is a GeoJSON FeatureCollection whose
        features carry a "name" property identifying the layer.  We accumulate
        them all under self.data["coords"] keyed by that layer name.
        """
        self._last_mqtt_message = datetime.utcnow()
        geojson = self._parse_json(payload, "coords")
        if not geojson:
            return
        # Determine which layer this payload belongs to by inspecting feature names
        layer_name: str | None = None
        for feature in geojson.get("features", []):
            name = feature.get("properties", {}).get("name", "")
            if name:
                layer_name = name
                break
        if layer_name is None:
            # Fallback: store the whole payload under a generic key
            layer_name = "unknown"
        self.data["coords"][layer_name] = geojson
        self._notify_listeners()

    def _handle_settings_raw(self, payload: str | bytes) -> None:
        """Handle the settings payload published by CaSSAndRA.

        The payload contains rover config including GPS origin fields:
          "latitude"   → rovercfg.lat
          "longtitude" → rovercfg.lon  (note: typo in CaSSAndRA source)
        """
        self._last_mqtt_message = datetime.utcnow()
        settings = self._parse_json(payload, "settings")
        if not settings:
            return
        self.data["settings"] = settings
        # Extract GPS origin and cache on the coordinator instance
        lat = settings.get("latitude")
        lon = settings.get("longtitude")  # CaSSAndRA typo
        if lat is not None and lon is not None:
            try:
                new_lat = float(lat)
                new_lon = float(lon)
                if new_lat != self.origin_lat or new_lon != self.origin_lon:
                    self.origin_lat = new_lat
                    self.origin_lon = new_lon
                    _LOGGER.debug(
                        "GPS origin updated: lat=%s lon=%s", self.origin_lat, self.origin_lon
                    )
                    # Persist to the config entry so it survives HA restart
                    self.hass.config_entries.async_update_entry(
                        self.entry,
                        data={
                            **self.entry.data,
                            CONF_ORIGIN_LAT: self.origin_lat,
                            CONF_ORIGIN_LON: self.origin_lon,
                        },
                    )
            except (TypeError, ValueError):
                _LOGGER.warning("Could not parse GPS origin from settings: lat=%s lon=%s", lat, lon)
        self._notify_listeners()

    # ------------------------------------------------------------------
    # CaSSAndRA request helpers (MQTT request/response)
    # ------------------------------------------------------------------

    async def _async_request_settings(self) -> None:
        """Ask CaSSAndRA to publish its current settings (includes GPS origin)."""
        try:
            await self.async_publish_command({"settings": {"command": "update"}})
            _LOGGER.debug("Requested CaSSAndRA settings for server '%s'", self._server_name)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not request CaSSAndRA settings: %s", exc)

    async def _async_request_coords(self, _now: Any = None) -> None:
        """Ask CaSSAndRA to publish the current map GeoJSON coordinate data."""
        try:
            await self.async_publish_command(
                {
                    "coords": {
                        "command": "update",
                        "value": ["currentMap", "mowPath", "obstacles"],
                    }
                }
            )
            _LOGGER.debug("Requested CaSSAndRA coords for server '%s'", self._server_name)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not request CaSSAndRA coords: %s", exc)

    # ------------------------------------------------------------------
    # HTTP fallback polling
    # ------------------------------------------------------------------

    async def _async_http_poll(self, _now: datetime | None = None) -> None:
        """Poll the CaSSAndRA HTTP endpoint when MQTT data is stale."""
        if not self._cassandra_url:
            return

        if self._last_mqtt_message is not None:
            age = datetime.utcnow() - self._last_mqtt_message
            if age < STALE_TIMEOUT:
                return

        _LOGGER.debug("MQTT stale, falling back to HTTP poll")
        try:
            session = async_get_clientsession(self.hass)
            # CaSSAndRA exposes a /api/status JSON endpoint
            url = f"{self._cassandra_url.rstrip('/')}/api/status"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    payload = await resp.json(content_type=None)
                    self._merge_http_payload(payload)
                else:
                    _LOGGER.warning(
                        "HTTP poll returned status %s from %s", resp.status, url
                    )
        except aiohttp.ClientError as exc:
            _LOGGER.debug("HTTP poll failed: %s", exc)

    def _merge_http_payload(self, payload: dict[str, Any]) -> None:
        """Merge data returned by HTTP fallback into self.data."""
        # CaSSAndRA /api/status returns the same structure as the MQTT /robot topic
        if "status" in payload:
            self.data["api_status"] = payload.get("api_status", self.data["api_status"])
        if "robot" in payload:
            self.data["robot"] = payload["robot"]
        elif "status" in payload:
            # Flat robot object (older CaSSAndRA versions)
            self.data["robot"] = payload
        self._notify_listeners()

    # ------------------------------------------------------------------
    # Command publishing
    # ------------------------------------------------------------------

    async def async_publish_command(self, command: dict[str, Any]) -> None:
        """Publish a command to the CaSSAndRA api_cmd topic."""
        topic = TOPIC_CMD.format(server_name=self._server_name)
        payload = json.dumps(command)
        if self._use_ha_mqtt:
            await mqtt.async_publish(self.hass, topic, payload)
        elif self._ext_mqtt_client:
            await self.hass.async_add_executor_job(
                self._ext_mqtt_client.publish, topic, payload
            )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(payload: str | bytes, key: str) -> dict[str, Any]:
        text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            _LOGGER.warning("Unexpected non-dict JSON for '%s': %s", key, type(parsed))
        except json.JSONDecodeError as exc:
            _LOGGER.warning("Cannot parse JSON for '%s': %s", key, exc)
        return {}

    @property
    def robot(self) -> dict[str, Any]:
        """Shortcut to the latest robot telemetry dict."""
        return self.data.get("robot", {})

    @property
    def server_name(self) -> str:
        return self._server_name
