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

# Seconds to average readings over before publishing. CT-clamp readings are
# noisy and the board emits several per second; averaging smooths the data and
# keeps Home Assistant's recorder database from growing too fast. Set to 0 to
# publish every reading as it arrives.
PUBLISH_INTERVAL = float(cfg("publishInterval", 5))

with open(os.path.join(DEVICE_MAPPING_DIR, DEVICE_MAPPING), "r") as fh:
    response_template = json.load(fh)

# Power fields (unit "W") get a derived cumulative-energy (kWh) sensor so Home
# Assistant's Energy dashboard / the Octopus cost tracker can use them.
POWER_FIELDS = [name for name, m in response_template.items()
                if m.get("unit_of_measurement") == "W"]

# Where the running energy totals are persisted so they survive a service
# restart (a total_increasing sensor must not jump back to zero).
STATE_FILE = os.environ.get("LECHACAL_STATE_FILE")
if not STATE_FILE:
    for _d in ("/var/lib/lechacal-mqtt", BASE_DIR):
        if os.path.isdir(_d) and os.access(_d, os.W_OK):
            STATE_FILE = os.path.join(_d, "energy_state.json")
            break

received_serial_data = False

# Buffer of readings accumulated between flushes when PUBLISH_INTERVAL > 0.
_readings_lock = threading.Lock()
_numeric_buffer = {}   # name -> list[float]   (averaged on flush)
_latest_strings = {}   # name -> str           (last value wins)

# Cumulative consumed energy in kWh per power field, integrated from power.
_energy_lock = threading.Lock()
_energy_kwh = {}            # name -> cumulative kWh
_last_energy_ts = None      # time.monotonic() of the last integration step
_last_save_ts = 0.0         # throttle how often we write STATE_FILE

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
# Energy integration (power W -> cumulative kWh) with persistence
# ---------------------------------------------------------------------------

def load_energy_state():
    """Restore cumulative energy totals from disk on startup."""
    if STATE_FILE and os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as fh:
                saved = json.load(fh)
            with _energy_lock:
                _energy_kwh.update({k: float(v) for k, v in saved.items()})
            print(f"Restored energy totals from {STATE_FILE}", flush=True)
        except Exception as exc:  # noqa: BLE001 - start fresh if unreadable
            print(f"Could not read {STATE_FILE}: {exc}", file=sys.stderr,
                  flush=True)


def save_energy_state(force=False):
    """Persist energy totals, throttled to at most once every 30s unless forced."""
    global _last_save_ts
    if not STATE_FILE:
        return
    now = time.monotonic()
    if not force and (now - _last_save_ts) < 30:
        return
    _last_save_ts = now
    try:
        with _energy_lock:
            snapshot = dict(_energy_kwh)
        tmp = f"{STATE_FILE}.tmp"
        with open(tmp, "w") as fh:
            json.dump(snapshot, fh)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001 - don't let persistence kill the loop
        print(f"Could not write {STATE_FILE}: {exc}", file=sys.stderr, flush=True)


def update_energy(client, power_values):
    """Integrate the given power readings (W) over elapsed time into kWh totals
    and publish each ``<name>_energy`` state. Only positive (consumed) power is
    accumulated, which is what the cost tracker bills."""
    global _last_energy_ts
    now = time.monotonic()
    dt_hours = 0.0 if _last_energy_ts is None else (now - _last_energy_ts) / 3600.0
    _last_energy_ts = now

    with _energy_lock:
        for name, power_w in power_values.items():
            consumed_w = power_w if power_w > 0 else 0.0
            _energy_kwh[name] = _energy_kwh.get(name, 0.0) + \
                consumed_w * dt_hours / 1000.0
            client.publish(state_topic(f"{name}_energy"),
                           f"{_energy_kwh[name]:.6f}")
    save_energy_state()


# ---------------------------------------------------------------------------
# Home Assistant MQTT discovery + state
# ---------------------------------------------------------------------------

def state_topic(name):
    return f"{MQTT_TOPIC}/sensor/{MQTT_DEVICENAME}_{name}"


def create_ha_sensor(client, name, unit_of_measurement, icon,
                     device_class=None, state_class=None):
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
    # Power fields get auto-classified; energy fields pass explicit classes.
    if unit_of_measurement == "W" and not device_class:
        device_class = "power"
        state_class = "measurement"
    if device_class:
        config_payload["device_class"] = device_class
    if state_class:
        config_payload["state_class"] = state_class

    client.publish(
        f"{state_topic(name)}/config",
        json.dumps(config_payload),
        retain=True,
    )


def push_ha_sensor_data(client, name, data):
    if isinstance(data, str):
        # Non-numeric field: publish the raw value, nothing to export.
        client.publish(state_topic(name), data)
        client.publish(state_topic(f"{name}_export"), "0")
        return
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
        # Cumulative energy (kWh) for power channels - what the Energy dashboard
        # and the Octopus cost tracker consume.
        if key in POWER_FIELDS:
            create_ha_sensor(client, f"{key}_energy", "kWh", "lightning-bolt",
                             device_class="energy",
                             state_class="total_increasing")


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

def parse_line(line):
    """Parse one serial line into ``{name: value}`` for every field that parses
    cleanly. Values are floats for numeric fields, strings for string fields."""
    # Values from the sensor are separated by spaces/tabs/commas.
    values = re.split(r"[ ,\t]+", line.strip())

    parsed = {}
    for count, key in enumerate(response_template.keys()):
        try:
            if count >= len(values):
                continue
            value = parse_value(values[count], key)
            if isinstance(value, str) or not math.isnan(value):
                parsed[key] = value
        except Exception as exc:  # noqa: BLE001 - mirror JS try/catch per field
            print(exc, file=sys.stderr, flush=True)
    return parsed


def handle_serial_line(client, line):
    """Port of the ``parser.on('data')`` handler.

    When ``PUBLISH_INTERVAL`` is 0 each reading is published immediately;
    otherwise readings are buffered and averaged by the flush thread.
    """
    global received_serial_data

    parsed = parse_line(line)

    if PUBLISH_INTERVAL > 0:
        with _readings_lock:
            for name, value in parsed.items():
                if isinstance(value, str):
                    _latest_strings[name] = value
                else:
                    _numeric_buffer.setdefault(name, []).append(value)
    else:
        for name, value in parsed.items():
            push_ha_sensor_data(client, name, value)
        power_values = {n: v for n, v in parsed.items() if n in POWER_FIELDS}
        update_energy(client, power_values)

    if not received_serial_data:
        print("Received data from sensor... Program is up and running!",
              flush=True)
        received_serial_data = True


def flush_averages(client):
    """Publish the mean of each buffered numeric field (and the latest value of
    each string field), then clear the buffers."""
    with _readings_lock:
        numeric = {name: vals for name, vals in _numeric_buffer.items() if vals}
        strings = dict(_latest_strings)
        _numeric_buffer.clear()
        _latest_strings.clear()

    means = {name: sum(vals) / len(vals) for name, vals in numeric.items()}
    for name, value in means.items():
        push_ha_sensor_data(client, name, value)
    for name, value in strings.items():
        push_ha_sensor_data(client, name, value)

    power_means = {n: v for n, v in means.items() if n in POWER_FIELDS}
    update_energy(client, power_means)


def schedule_average_flush(client):
    """Flush averaged readings to MQTT every ``PUBLISH_INTERVAL`` seconds."""
    def _flush():
        while True:
            time.sleep(PUBLISH_INTERVAL)
            try:
                flush_averages(client)
            except Exception as exc:  # noqa: BLE001 - keep the thread alive
                print(f"Error flushing averages: {exc}", file=sys.stderr,
                      flush=True)

    thread = threading.Thread(target=_flush, daemon=True)
    thread.start()


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
    load_energy_state()

    print("Establishing MQTT connection...", flush=True)
    client = mqtt.Client()
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect

    client.connect_async(MQTT_SERVER, MQTT_PORT)
    client.loop_start()

    schedule_sensor_refresh(client)
    if PUBLISH_INTERVAL > 0:
        print(f"Averaging readings over {PUBLISH_INTERVAL:g}s before publishing.",
              flush=True)
        schedule_average_flush(client)

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
        save_energy_state(force=True)
        ser.close()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
