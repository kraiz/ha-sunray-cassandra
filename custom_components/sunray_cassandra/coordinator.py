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
    CONF_SERVER_NAME,
    CONF_USE_HA_MQTT,
    DEFAULT_MQTT_PORT,
    DOMAIN,
    ROBOT_STATUS_OFFLINE,
    TOPIC_CMD,
    TOPIC_MAPS,
    TOPIC_MOW_PARAMETERS,
    TOPIC_ROBOT,
    TOPIC_SCHEDULE,
    TOPIC_SERVER,
    TOPIC_STATUS,
    TOPIC_TASKS,
)

_LOGGER = logging.getLogger(__name__)

# If no MQTT message has been received for this long, attempt HTTP fallback
STALE_TIMEOUT = timedelta(seconds=30)
# Interval for the HTTP polling fallback
HTTP_POLL_INTERVAL = timedelta(seconds=15)


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
            "maps": {},
            "tasks": {},
            "mow_parameters": {},
            "server": {},
            "schedule": {},
        }

        self._listeners: list[callback] = []
        self._unsubscribe_mqtt: list[Any] = []
        self._unsub_poll: Any = None
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
            TOPIC_MAPS: self._handle_maps,
            TOPIC_TASKS: self._handle_tasks,
            TOPIC_MOW_PARAMETERS: self._handle_mow_parameters,
            TOPIC_SERVER: self._handle_server,
            TOPIC_SCHEDULE: self._handle_schedule,
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
            TOPIC_MAPS.format(server_name=self._server_name): self._handle_maps_raw,
            TOPIC_TASKS.format(server_name=self._server_name): self._handle_tasks_raw,
            TOPIC_MOW_PARAMETERS.format(server_name=self._server_name): self._handle_mow_parameters_raw,
            TOPIC_SERVER.format(server_name=self._server_name): self._handle_server_raw,
            TOPIC_SCHEDULE.format(server_name=self._server_name): self._handle_schedule_raw,
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
        self.data["robot"] = self._parse_json(payload, "robot")
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
