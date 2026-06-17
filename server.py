#!/usr/bin/env python3
"""Read serial data from a LeChacal RPICT CT-clamp board and publish it to
Home Assistant over MQTT (with auto-discovery).

This is a Python port of the original Node.js ``server.js``. Behaviour is kept
deliberately faithful to the original so existing Home Assistant entities keep
working unchanged.
"""

import json
import math
import operator
import os
import re
import sys
import time
import threading

import serial
import yaml
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Paths / config loading
# ---------------------------------------------------------------------------

# Where this script lives (used to find the bundled device-mapping/ directory).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Config file: env override, then the system path the installer uses, then a
# local file for development.
CONFIG_PATH = os.environ.get("LECHACAL_CONFIG")
if not CONFIG_PATH:
    for candidate in ("/etc/lechacal-mqtt/config.yml",
                      os.path.join(BASE_DIR, "config.yml")):
        if os.path.exists(candidate):
            CONFIG_PATH = candidate
            break

# Directory holding the device-mapping JSON files.
DEVICE_MAPPING_DIR = os.environ.get(
    "LECHACAL_DEVICE_MAPPING_DIR", os.path.join(BASE_DIR, "device-mapping")
)


def load_config():
    if not CONFIG_PATH or not os.path.exists(CONFIG_PATH):
        sys.exit(
            "No config file found. Set LECHACAL_CONFIG or create "
            "/etc/lechacal-mqtt/config.yml (see config.yml.example)."
        )
    with open(CONFIG_PATH, "r") as fh:
        return yaml.safe_load(fh) or {}


config = load_config()


def cfg(key, default):
    """config.get with the original's ``value || default`` semantics: empty
    strings and zero-less falsy values fall back to the default, matching the
    JavaScript ``config.foo || 'bar'`` behaviour."""
    value = config.get(key)
    return value if value not in (None, "") else default


# ---------------------------------------------------------------------------
# Resolved settings (defaults mirror the JS ``||`` fallbacks)
# ---------------------------------------------------------------------------

SERIAL_PORT = cfg("serialPort", "/dev/ttyAMA0")
BAUD_RATE = cfg("baudRate", 38400)
IRMS_MA_OFFSET = config.get("IrmsMAoffset") or 0

MQTT_SERVER = cfg("mqttServer", "0.0.0.0")
MQTT_PORT = int(cfg("mqttPort", 1883))
MQTT_USERNAME = config.get("mqttUsername")
MQTT_PASSWORD = config.get("mqttPassword")
MQTT_TOPIC = cfg("mqttTopic", "homeassistant")
MQTT_DEVICENAME = cfg("mqttDevicename", "lechacal")
DEVICE_MAPPING = cfg("deviceMapping", "RPICT7V1.json")

with open(os.path.join(DEVICE_MAPPING_DIR, DEVICE_MAPPING), "r") as fh:
    response_template = json.load(fh)

received_serial_data = False

# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

_MATH_OPS = {"+": operator.add, "-": operator.sub,
             "*": operator.mul, "/": operator.truediv}
_MATH_RE = re.compile(r"^\s*([*/+-])\s*(-?[0-9.]+)\s*$")


def apply_convert_math(value, transform):
    """Apply a ``convertMath`` expression such as ``"/ 1000"`` or ``"* 2"``.

    The original used JS ``eval`` here; we parse a single ``<op> <number>``
    expression instead to avoid evaluating arbitrary code.
    """
    match = _MATH_RE.match(transform)
    if not match:
        raise ValueError(f"Unsupported convertMath expression: {transform!r}")
    op, operand = match.group(1), float(match.group(2))
    return _MATH_OPS[op](value, operand)


def parse_value(data, config_item):
    """Port of ``parseDataFromTemplateParams``. Returns a float (or string),
    or ``nan`` when the data can't be parsed as the declared numeric type."""
    item_type = response_template[config_item].get("type")

    if item_type == "float":
        try:
            return_value = float(data) + IRMS_MA_OFFSET
        except (TypeError, ValueError):
            return math.nan
    elif item_type == "integer":
        try:
            return_value = int(data) + IRMS_MA_OFFSET
        except (TypeError, ValueError):
            return math.nan
    elif item_type == "string":
        return_value = data
    else:
        return_value = data

    transform = response_template[config_item].get("convertMath")
    if transform:
        return apply_convert_math(return_value, transform)
    return return_value


# ---------------------------------------------------------------------------
# Home Assistant MQTT discovery + state
# ---------------------------------------------------------------------------

def state_topic(name):
    return f"{MQTT_TOPIC}/sensor/{MQTT_DEVICENAME}_{name}"


def create_ha_sensor(client, name, unit_of_measurement, icon):
    config_payload = {
        "name": name,
        "unique_id": f"{MQTT_DEVICENAME}_{name}",
        "unit_of_measurement": unit_of_measurement,
        "state_topic": state_topic(name),
        "icon": f"mdi:{icon}",
        "device": {
            "identifiers": [MQTT_DEVICENAME],
            "name": MQTT_DEVICENAME,
            "model": DEVICE_MAPPING,
            "manufacturer": "Lechacal",
        },
    }
    if unit_of_measurement == "W":
        config_payload["device_class"] = "power"
        config_payload["state_class"] = "measurement"

    client.publish(
        f"{state_topic(name)}/config",
        json.dumps(config_payload),
        retain=True,
    )


def push_ha_sensor_data(client, name, data):
    import_value = data if data > 0 else 0
    export_value = abs(data) if data < 0 else 0
    client.publish(state_topic(name), str(import_value))
    client.publish(state_topic(f"{name}_export"), str(export_value))


def create_ha_sensors(client):
    print("Creating HA Sensors...", flush=True)
    for key, mapping in response_template.items():
        unit = mapping.get("unit_of_measurement", "")
        icon = mapping.get("icon", "")
        create_ha_sensor(client, key, unit, icon)
        create_ha_sensor(client, f"{key}_export", unit, icon)


def schedule_sensor_refresh(client):
    """Re-create HA sensors every 5 minutes (in case HA is restarted etc)."""
    def _refresh():
        while True:
            time.sleep(5 * 60)
            try:
                create_ha_sensors(client)
            except Exception as exc:  # noqa: BLE001 - keep the thread alive
                print(f"Error refreshing sensors: {exc}", file=sys.stderr,
                      flush=True)

    thread = threading.Thread(target=_refresh, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Serial handling
# ---------------------------------------------------------------------------

def handle_serial_line(client, line):
    """Port of the ``parser.on('data')`` handler."""
    global received_serial_data

    # Values from the sensor are separated by spaces/tabs/commas.
    values = re.split(r"[ ,\t]+", line.strip())

    for count, key in enumerate(response_template.keys()):
        try:
            if count >= len(values):
                continue
            value = parse_value(values[count], key)
            if isinstance(value, str) or not math.isnan(value):
                push_ha_sensor_data(client, key, value)
        except Exception as exc:  # noqa: BLE001 - mirror JS try/catch per field
            print(exc, file=sys.stderr, flush=True)

    if not received_serial_data:
        print("Received data from sensor, and posted to MQTT... "
              "Program is up and running!", flush=True)
        received_serial_data = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        create_ha_sensors(client)
    else:
        print(f"MQTT connection failed with code {rc}", file=sys.stderr,
              flush=True)


def main():
    print("Establishing MQTT connection...", flush=True)
    client = mqtt.Client()
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect

    client.connect_async(MQTT_SERVER, MQTT_PORT)
    client.loop_start()

    schedule_sensor_refresh(client)

    print("Connecting to serial port...", flush=True)
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=10)

    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            handle_serial_line(client, line)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
