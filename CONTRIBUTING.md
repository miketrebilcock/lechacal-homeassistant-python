# Developing & Contributing

Thanks for your interest in improving this project! This guide covers how to set
up a development environment, test changes (including without any hardware), add
support for a new board, and submit a pull request.

## Project overview

The whole app is a single script, [`server.py`](server.py). The data flow is:

```
LeChacal board ──serial──> server.py ──MQTT──> Home Assistant
```

1. Read config from `config.yml` (path resolved via `LECHACAL_CONFIG`, then
   `/etc/lechacal-mqtt/config.yml`, then `./config.yml`).
2. Load a **device-mapping** JSON describing each serial column in order.
3. On each serial line: split on whitespace/commas, parse each column per its
   mapping (type coercion, optional `IrmsMAoffset`, optional `convertMath`).
   Readings are then averaged over `publishInterval` seconds (numeric fields →
   mean, string fields → last value) and published to MQTT with Home Assistant
   auto-discovery. Set `publishInterval: 0` to publish every reading immediately.
4. Re-publish the discovery config every 5 minutes so entities survive an HA
   restart.

### Repository layout

| Path | What it is |
| --- | --- |
| `server.py` | The entire application. |
| `device-mapping/*.json` | Per-board column descriptions (see below). |
| `config.yml.example` | Template copied to `/etc/lechacal-mqtt/config.yml` on install. |
| `lechacal-mqtt.service` | systemd unit template. |
| `install.sh` / `uninstall.sh` | Pi-side installer / remover. |
| `requirements.txt` | Runtime Python dependencies. |

## Setting up a dev environment

You can develop on any machine — a Pi isn't required.

```bash
git clone https://github.com/miketrebilcock/lechacal-homeassistant-python.git
cd lechacal-homeassistant-python

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.yml.example config.yml   # edit as needed
```

Run against your config with:

```bash
LECHACAL_CONFIG=./config.yml python server.py
```

`server.py` honours two environment variables, which is what makes local dev easy:

- `LECHACAL_CONFIG` — path to the config file.
- `LECHACAL_DEVICE_MAPPING_DIR` — directory of mapping JSONs (defaults to the
  bundled `device-mapping/`).
- `LECHACAL_STATE_FILE` — where cumulative energy totals are persisted (defaults
  to `/var/lib/lechacal-mqtt/energy_state.json`, falling back to the app dir for
  local dev).

## Testing without hardware

The parsing/publishing path is decoupled from the serial port, so you can feed it
a sample line and inspect what *would* be published using a stub MQTT client.
Save this as `smoke_test.py` (it's git-ignored by the `*.py[cod]`/local patterns,
or just delete it after):

```python
import server

class FakeClient:
    def __init__(self): self.msgs = []
    def publish(self, topic, payload=None, retain=False):
        self.msgs.append((topic, payload, retain))

c = FakeClient()
# A sample RPICT7V1 line: NodeID + 7 RP + 7 Irms + Vrms
line = "11 0.0 0.0 0.0 -0.0 0.0 0.0 -0.0 202.1 208.6 235.3 207.2 223.4 3296.3 2310.8 0.9"
server.handle_serial_line(c, line)

for topic, payload, _ in c.msgs:
    print(f"{topic} = {payload}")
print(f"\nTotal messages: {len(c.msgs)}")
```

Run it with the same env vars:

```bash
LECHACAL_CONFIG=./config.yml \
LECHACAL_DEVICE_MAPPING_DIR=./device-mapping \
python smoke_test.py
```

A quick syntax check before committing:

```bash
python -m py_compile server.py
```

## Adding support for a new board

Boards are described entirely by a JSON file in `device-mapping/`. To add one,
copy the closest existing file and adjust it. The **order of keys matters** — it
must match the order of values the board emits on the serial line.

Each key maps to an object:

| Field | Required | Notes |
| --- | --- | --- |
| `type` | yes | `"float"`, `"integer"`, or `"string"`. |
| `unit_of_measurement` | yes | HA unit, e.g. `"A"`, `"V"`, `"W"`. A value of `"W"` tags the entity as `device_class: power` / `state_class: measurement` **and** makes the bridge derive a cumulative `<name>_energy` sensor in kWh (`device_class: energy`, `total_increasing`) by integrating that power over time — used by the Energy dashboard / cost trackers. |
| `icon` | yes | [MDI](https://pictogrammers.com/library/mdi/) icon name *without* the `mdi:` prefix, e.g. `"current-ac"`. |
| `convertMath` | no | A single `<op> <number>` transform applied after parsing, e.g. `"/ 1000"` to convert mA→A, or `"* 2"`. Operators: `+ - * /`. |

Example (one column):

```json
"Irms1": {
    "type": "float",
    "unit_of_measurement": "A",
    "icon": "current-ac",
    "convertMath": "/ 1000"
}
```

Then point `deviceMapping` in your config at the new filename and test it with the
smoke-test approach above. Please also add the board to the supported list in the
[README](README.md#supported-boards).

> Note: `IrmsMAoffset` from the config is added to **every** numeric field before
> `convertMath` runs (this matches the original project's behaviour). Keep that in
> mind when choosing values.

## Code style

- Plain Python 3, standard library plus the three runtime deps — avoid adding new
  dependencies unless there's a clear need.
- Match the existing style in `server.py` (4-space indent, descriptive names,
  small functions that mirror the data flow).
- Keep `server.py` runnable both from a checkout and from the installed
  `/opt/lechacal-mqtt` location — don't hard-code paths; use the existing
  `BASE_DIR` / env-var resolution.

## Submitting changes

1. Fork the repo and create a branch off `main`
   (`git checkout -b my-change`).
2. Make your change and verify it (`py_compile` + the smoke test, and a real run
   on a Pi if your change touches serial/MQTT behaviour).
3. Keep commits focused with clear messages.
4. Open a pull request describing **what** changed and **why**, and how you
   tested it. If you added or changed a device mapping, mention which board and
   how it was verified.

Thanks for contributing!
