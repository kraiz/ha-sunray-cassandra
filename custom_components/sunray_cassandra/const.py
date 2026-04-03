"""Constants for the Sunray / CaSSAndRA integration."""
from __future__ import annotations

DOMAIN = "sunray_cassandra"

# Config entry keys
CONF_SERVER_NAME = "server_name"
CONF_MQTT_BROKER = "mqtt_broker"
CONF_MQTT_PORT = "mqtt_port"
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"
CONF_USE_HA_MQTT = "use_ha_mqtt"
CONF_CASSANDRA_URL = "cassandra_url"
CONF_ORIGIN_LAT = "origin_lat"
CONF_ORIGIN_LON = "origin_lon"

# Defaults
DEFAULT_MQTT_PORT = 1883
DEFAULT_SERVER_NAME = "myCaSSAndRA"
DEFAULT_CASSANDRA_PORT = 8050

# MQTT topic templates  (format with server_name)
TOPIC_STATUS = "{server_name}/status"
TOPIC_ROBOT = "{server_name}/robot"
TOPIC_MAP = "{server_name}/map"
TOPIC_MAPS = "{server_name}/maps"
TOPIC_TASKS = "{server_name}/tasks"
TOPIC_MOW_PARAMETERS = "{server_name}/mowParameters"
TOPIC_SERVER = "{server_name}/server"
TOPIC_SCHEDULE = "{server_name}/schedule"
TOPIC_CMD = "{server_name}/api_cmd"
TOPIC_COORDS = "{server_name}/coords"
TOPIC_SETTINGS = "{server_name}/settings"

# CaSSAndRA API server status values
API_STATUS_BOOT = "boot"
API_STATUS_READY = "ready"
API_STATUS_BUSY = "busy"
API_STATUS_OFFLINE = "offline"

# CaSSAndRA robot status values → mapped to LawnMowerActivity
ROBOT_STATUS_OFFLINE = "offline"
ROBOT_STATUS_IDLE = "idle"
ROBOT_STATUS_TRANSIT = "transit"
ROBOT_STATUS_MOW = "mow"
ROBOT_STATUS_DOCKED = "docked"
ROBOT_STATUS_CHARGING = "charging"
ROBOT_STATUS_DOCKING = "docking"
ROBOT_STATUS_ERROR = "error"
ROBOT_STATUS_UNKNOWN = "unknown"
ROBOT_STATUS_MOVE = "move"
ROBOT_STATUS_MAP_UPLOAD = "map upload"
ROBOT_STATUS_RESUME = "resume"
ROBOT_STATUS_REBOOT = "reboot"
ROBOT_STATUS_SHUTDOWN = "shutdown"
ROBOT_STATUS_GPS_REBOOT = "gps reboot"

# GPS solution values
GPS_SOLUTION_FIX = "fix"
GPS_SOLUTION_FLOAT = "float"
GPS_SOLUTION_INVALID = "invalid"

# Sensor error code descriptions
SENSOR_STATE_MAP: dict[int, str] = {
    0: "no error",
    1: "undervoltage",
    2: "obstacle",
    3: "gps timeout",
    4: "imu timeout",
    5: "imu tilt",
    6: "kidnapped",
    7: "overload",
    8: "motor error",
    9: "gps invalid",
    10: "odo error",
    11: "no route",
    12: "memory error",
    13: "bumper error",
    14: "sonar error",
    15: "lifted",
    16: "rain sensor",
    17: "emergency/stop",
    18: "temperature out of range",
}

# Data update coordinator key stored on hass.data[DOMAIN]
DATA_COORDINATOR = "coordinator"
