# Sunray / CaSSAndRA – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub release](https://img.shields.io/github/v/release/kraiz/ha-sunray-cassandra)](https://github.com/kraiz/ha-sunray-cassandra/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [Home Assistant](https://www.home-assistant.io/) integration for [Ardumower](https://www.ardumower.de/) robots running the [Sunray firmware](https://github.com/owlRobotics-GmbH/Sunray) and managed by [CaSSAndRA](https://github.com/EinEinfach/CaSSAndRA).

It uses the native Home Assistant [`lawn_mower`](https://www.home-assistant.io/integrations/lawn_mower/) entity platform and connects to CaSSAndRA via its MQTT API.

---

## Requirements

- Home Assistant 2024.1 or newer
- [CaSSAndRA](https://github.com/EinEinfach/CaSSAndRA) running and reachable
- An MQTT broker (the HA [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) or a standalone broker)

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant.
2. Go to **Integrations → Custom repositories**.
3. Add `https://github.com/kraiz/ha-sunray-cassandra` with category **Integration**.
4. Search for **Sunray / CaSSAndRA** and install it.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/sunray_cassandra` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

---

## Setup

Go to **Settings → Devices & Services → Add Integration** and search for **Sunray / CaSSAndRA**.

The config flow has three steps:

1. **Connection mode** – choose whether to use the existing HA MQTT integration or enter custom broker credentials.
2. **MQTT broker** *(only if not using HA MQTT)* – broker host, port, and optional credentials.
3. **Server name** – the CaSSAndRA server name used as the MQTT topic prefix (default: `myCaSSAndRA`). Optionally provide the CaSSAndRA web URL (e.g. `http://cassandra.local:8050`) to enable the HTTP fallback and add a configuration link to the device page.

---

## Entities

All entities are grouped under a single **device** per CaSSAndRA instance.

### Lawn Mower

| Entity | Description |
|---|---|
| `lawn_mower.<name>` | The mower itself. Supports **Start**, **Pause**, and **Dock**. Pressing Start runs the task currently selected in the Task picker, or mows all area if none is selected. |

States map from CaSSAndRA status as follows:

| CaSSAndRA status | HA activity |
|---|---|
| `mow`, `transit`, `resume`, `move` | `mowing` |
| `docked`, `charging` | `docked` |
| `docking` | `returning` |
| `idle`, `map upload`, `reboot`, `shutdown` | `paused` |
| `error`, `offline`, `unknown` | `error` |

### Sensors

| Entity | Unit | Description |
|---|---|---|
| Battery | % | State of charge |
| Battery Voltage | V | Raw battery voltage |
| Battery Current | A | Charge/discharge current (negative = charging) |
| Current Task | – | Name of the task currently loaded and running on the robot |
| Sensor State | – | Human-readable error description (`no error` when all is well) |
| Dock Reason | – | Why the robot last docked (`operator`, `schedule`, `finished`, `low battery`, `rain`, `temperature`) |
| GPS Quality | – | Fix quality: `fix`, `float`, or `invalid` |
| GPS Satellites | – | Number of visible GPS satellites |
| Mow Progress | % | Completion of the current mow path |
| Speed | m/s | Current driving speed |
| Average Speed | m/s | Exponentially smoothed average speed |
| Position X / Y | m | Robot position relative to map origin |
| Server Version | – | CaSSAndRA software version |
| Server CPU Load | % | CaSSAndRA host CPU utilisation |
| Server CPU Temperature | °C | CaSSAndRA host CPU temperature |
| Server Memory Usage | % | CaSSAndRA host RAM utilisation |
| Server Disk Usage | % | CaSSAndRA host disk utilisation |
| API Status | – | CaSSAndRA MQTT API state: `boot`, `ready`, `busy`, or `offline` |

Battery Voltage / Current, Average Speed, Position X/Y, and all Server sensors are in the **Diagnostic** category and hidden by default.

### Select

| Entity | Description |
|---|---|
| Task | Dropdown of all saved CaSSAndRA tasks. Selecting an option tells CaSSAndRA to mark that task as active. Pressing **Start** on the mower card will then run it. Choose **— mow all —** to mow the entire area without a specific task. |

The option list and current selection update live via MQTT — changes made in the CaSSAndRA web UI are reflected automatically.

### Switch

| Entity | Description |
|---|---|
| Schedule | Enables or disables the CaSSAndRA weekly mow schedule. |

---

## Services

Beyond the standard `lawn_mower.start_mowing`, `lawn_mower.pause`, and `lawn_mower.dock`, the integration registers these additional services:

| Service | Fields | Description |
|---|---|---|
| `sunray_cassandra.mow_task` | `task` (string, default `"all"`) | Start a named task, `"all"` for full area, or `"resume"` to continue. |
| `sunray_cassandra.go_to` | `x`, `y` (float, metres) | Navigate the mower to a map coordinate. |
| `sunray_cassandra.reboot` | – | Reboot the Sunray firmware. |
| `sunray_cassandra.reboot_gps` | – | Reboot the GPS module. |
| `sunray_cassandra.set_mow_speed` | `speed` (0.0 – 1.0) | Set the mowing speed set-point. |
| `sunray_cassandra.toggle_mow_motor` | – | Toggle the blade motor on/off without stopping navigation. |

All services target a `lawn_mower` entity. When only one CaSSAndRA instance is configured, the target can be omitted.

---

## How start_mowing decides what to run

```
Is the mower already mowing / in transit?
  └─ Yes → resume (continue current path)
  └─ No  → Is a specific task selected in the Task picker?
              └─ Yes → run that task
              └─ No  → mow all area
```

---

## Connection & Fallback

**Primary: MQTT (push)**
CaSSAndRA publishes telemetry every ~2 seconds to `{server_name}/robot`, `{server_name}/tasks`, etc. The integration subscribes to these topics and updates all entities immediately on receipt.

**Fallback: HTTP polling**
If a CaSSAndRA web URL is configured and no MQTT message has been received for 30 seconds, the integration polls `{url}/api/status` every 15 seconds. This covers temporary MQTT broker outages without losing visibility.

---

## Translations

English and German are included. Contributions for other languages are welcome — add a file to `custom_components/sunray_cassandra/translations/` following the existing `en.json` structure.

---

## License

MIT
