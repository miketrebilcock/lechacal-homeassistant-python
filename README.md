# LeChacal RPICT → Home Assistant (MQTT)

A lightweight Python service that reads energy-monitoring data from
[LeChacal RPICT](http://lechacal.com/) CT-clamp boards over serial and publishes
it to [Home Assistant](https://www.home-assistant.io/) via MQTT, with automatic
device/sensor discovery.

It runs as a normal **systemd service** on a Raspberry Pi — no Docker required.
This is a Python re-implementation of the Node.js
[docker-lechacal-homeassistant](https://github.com/ned-kelly/docker-lechacal-homeassistant)
project, packaged to install directly on the Pi.

## Supported boards

A JSON "mapping" file describes how each board's serial output is parsed. Bundled
mappings (`device-mapping/`):

- `RPICT3V1` – 3 CT, 1 Voltage
- `RPICT3T1` – 3 CT, 1 Temperature
- `RPICT4V3_v2.0` – 4 CT, 3 AC Voltage
- `RPICT7V1` – 7 CT, 1 AC Voltage
- `RPICT8` – 8 CT

If your board isn't listed, copy an existing file in `device-mapping/` and adjust
it, then point `deviceMapping` at it in your config.

## Prerequisites

- A LeChacal RPICT CT-clamp PCB wired to the Pi's serial pins.
- A Raspberry Pi running Raspberry Pi OS (or any Debian/Ubuntu host with
  `apt-get`).
- Home Assistant with the
  [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) configured.

### Enable the Pi serial port

Add to `/boot/config.txt` (or `/boot/firmware/config.txt` on newer images):

```
[all]
enable_uart=1
```

Then disable the serial *login console* (so it doesn't fight for the port) using
`sudo raspi-config` → **Interface Options → Serial Port** → *login shell over
serial: No*, *serial hardware enabled: Yes*. Reboot afterwards.

## Install

```bash
git clone https://github.com/miketrebilcock/lechacal-homeassistant-python.git
cd lechacal-homeassistant-python
sudo ./install.sh
```

The installer will:

1. Install `python3` / `python3-venv`.
2. Create a `lechacal` system user (added to the `dialout` group for serial
   access).
3. Copy the app to `/opt/lechacal-mqtt` and build a virtualenv there.
4. Create `/etc/lechacal-mqtt/config.yml` from the example (only if missing).
5. Install, enable and start the `lechacal-mqtt` systemd service.

Then edit your settings and restart:

```bash
sudo nano /etc/lechacal-mqtt/config.yml
sudo systemctl restart lechacal-mqtt
```

## Configuration

`/etc/lechacal-mqtt/config.yml` (see [`config.yml.example`](config.yml.example)):

| Key              | Default          | Description                                   |
| ---------------- | ---------------- | --------------------------------------------- |
| `serialPort`     | `/dev/ttyAMA0`   | Serial device the board is connected to.      |
| `baudRate`       | `38400`          | Serial baud rate.                             |
| `IrmsMAoffset`   | `0`              | Offset added to every numeric reading.        |
| `publishInterval`| `5`              | Seconds to average readings over before publishing; `0` = publish every reading. |
| `mqttServer`     | `0.0.0.0`        | MQTT broker host.                             |
| `mqttPort`       | `1883`           | MQTT broker port.                             |
| `mqttUsername`   | _(none)_         | MQTT username (optional).                     |
| `mqttPassword`   | _(none)_         | MQTT password (optional).                     |
| `mqttTopic`      | `homeassistant`  | HA MQTT discovery prefix / base topic.        |
| `mqttDevicename` | `lechacal`       | Device + entity name prefix in HA.            |
| `deviceMapping`  | `RPICT7V1.json`  | Which mapping file in `device-mapping/` to use.|

## Managing the service

```bash
systemctl status lechacal-mqtt        # current state
journalctl -u lechacal-mqtt -f        # live logs
sudo systemctl restart lechacal-mqtt  # after editing config
sudo systemctl stop lechacal-mqtt
```

Once running, the device and its sensors auto-register in Home Assistant — you
don't need to define any sensors manually. Each reading is published twice: the
sensor value (clamped to ≥0) and a `<name>_export` value for any negative
(export) reading.

## Energy & cost tracking

Every power channel (any field measured in watts — `RP*`/`Realpower*`/`P*`
depending on your board) is published as a power sensor (`device_class: power`).
For each one, the bridge also derives a **cumulative energy sensor in kWh** —
`<name>_energy` (e.g. `sensor.lechacal1_RP1_energy`) — by integrating power over
time. These are published as `device_class: energy` / `state_class:
total_increasing`, which is exactly what Home Assistant's **Energy dashboard** and
cost integrations (e.g. the **Octopus Energy cost tracker**) expect.

> **Accuracy depends on your board.** Boards with mains-voltage sensing
> (RPICT3V1, RPICT4V3, RPICT7V1) report *true real power*, so the energy figures
> account for power factor. Boards **without** voltage sensing (RPICT3T1, RPICT8)
> report *estimated* power — current × a fixed assumed voltage configured on the
> board, with power factor assumed to be 1. Their energy/cost figures are good
> ballpark estimates but will drift from a revenue meter, especially on reactive
> loads (motors, fridges). Set the board's assumed voltage close to your actual
> mains for the best results.

The running totals are persisted to `/var/lib/lechacal-mqtt/energy_state.json` so
they survive a service restart and don't reset to zero.

To get per-circuit cost (daily/weekly/monthly), add a cost tracker against each
`*_energy` sensor — for the Octopus Energy integration: *Settings → Devices &
Services → Octopus Energy → Add entry → Cost Tracker*, then pick the relevant
`sensor.<devicename>_<channel>_energy` sensor (e.g. `_RP1_energy` on an RPICT7V1,
`_P1_energy` on an RPICT3T1) and mark it as an accumulative (increasing) value.
(Cost trackers are always added manually in that integration — it doesn't create
them automatically, even for other device types.)

> Only positive (consumed) power is integrated into the energy total, which is
> what gets billed. If you also need exported energy per channel (e.g. a circuit
> wired to solar), open an issue — it's a small addition.

## Uninstall

```bash
sudo ./uninstall.sh
```

## Developing & contributing

You can develop and test on any machine — a Pi isn't required. Quick start:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.yml.example config.yml   # edit it
LECHACAL_CONFIG=./config.yml python server.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide — testing without
hardware, adding support for a new board, code style, and how to submit a pull
request.

## License

See [LICENSE](LICENSE).
