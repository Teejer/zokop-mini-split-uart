# Zokop/Tuya Mini Split UART → MQTT Bridge

Replace the Tuya WiFi module in a Zokop 12000 BTU mini split with a Raspberry Pi Zero 2W and a USB-to-TTL serial adapter, giving you full control of the AC over MQTT with native Home Assistant integration.

## How it works

The indoor unit of the Zokop mini split contains a small Tuya WiFi module that talks to the AC's main control board over a 4-wire harness: 5 V, GND, TX, and RX. The data is a Tuya-style UART protocol at 115200 baud, made up of frames containing data points (DPs) for power, temperature, mode, fan, and louvers.

Since this is a generic Tuya UART protocol, the same approach — and much of this code — will probably work with other Tuya-based mini splits. Expect some variation between units: wire colors, available DP values, and the exact captured frames may differ, so verify the wiring and re-capture frames if your unit behaves differently.

Instead of letting the Tuya module handle the radio, this project:

- **`ac_bridge.py`** — the main bridge. Opens the serial port, publishes AC state to MQTT, accepts commands from MQTT, and auto-publishes Home Assistant MQTT discovery.
- **`ambient_sensor.py`** — optional companion service. Reads a DHT22 (via GPIO bit-bang, or the kernel `dht11` IIO overlay on a Pi Zero W) and publishes ambient temperature + humidity to the same broker, joining the same Home Assistant device as the AC. The temperature also becomes the climate entity's current (ambient) temperature.
- **`ac_control.py`** — a standalone CLI tool for sending/receiving frames without MQTT (handy for testing wiring).
- **`config.example.json`** — sample configuration (copy to `config.json` and edit).
- **`ac_bridge.service`** — systemd unit to run the bridge on boot.
- **`ambient_sensor.service`** — systemd unit for the DHT22 ambient sensor service.
- **`apply-low-power.sh`** + **`boot-config.txt`** / **`boot-config-zero-w.txt`** — optional low-power tuning for a Pi Zero 2W or Zero W.
- **`protoInfo.txt`** / **`info.txt`** — reverse-engineered protocol notes and wiring references.

## Parts

| Part | Notes |
|------|-------|
| Raspberry Pi Zero 2W | Any small Pi works; the Zero 2W tucks easily into the indoor unit |
| USB-to-TTL serial adapter | FTDI, CP2102, or CH340 — 115200 baud, 8N1. It must operate at 5 V levels, since the AC board's UART is 5 V — this build's adapter has a 3.3 V/5 V I/O jumper, set to 5 V |
| Dupont wires + connectors | Terminate the soldered harness leads |
| Hook-up wire + soldering iron | The harness connector wouldn't unplug, so it was cut and re-terminated |
| Hi-Link HLK-PM01 (or similar) AC-DC module | Taps the 120 V line feeding the indoor unit and converts it to 5 V to power the Pi (the USB-TTL adapter draws its power from the Pi's USB port) |

> **Level shifter?** Not needed with a 5 V USB-TTL adapter. If you adapt this project to a 3.3 V-only MCU (ESP32, Raspberry Pi GPIO, …) you'll need a 5 V ↔ 3.3 V level shifter — a 74AHCT125 for TX, or a bidirectional MOSFET module (e.g. BSS138-based) for both lines. Wiring details are in `info.txt`.

## 1. Replacing the Tuya WiFi module

> **Warning:** cut power to the AC at the breaker before opening the indoor unit. The unit is a 208/240 V appliance — stay clear of the refrigerant lines, capacitors, and high-voltage wiring while working.

1. **Open the indoor unit.** Slide the front panel up and off. The main control board is visible inside; the Tuya module is a small PCB (often with a visible antenna) plugged into the main board via a short 4-wire harness.
2. **Disconnect the harness from the Tuya module.** On this unit the connector wouldn't unplug, so I cut the harness at the module end and soldered hook-up wires to the cut ends, terminating the other side with dupont connectors for the USB-TTL adapter.
3. **Identify the wires** (colors from this project's unit — verify with a multimeter on yours):

   | Wire | Signal | Purpose |
   |------|--------|---------|
   | Yellow | 5 V | Powered the Tuya module — left unused in this build (the HLK-PM01 supplies power instead) |
   | White | GND | Common ground |
   | Black | Module → Board | Board's RX input — this is where you send commands |
   | Red | Board → Module | Board's TX output — this is where you receive state |

   Sanity check: yellow/white should measure ~5 V DC between them, and the remaining two wires are the data lines.
4. **Wire the harness leads to the adapter:**

   | Adapter pin | Connect to |
   |-------------|--------------|
   | TX | Black wire (board RX) — soldered dupont lead |
   | RX | Red wire (board TX) — soldered dupont lead |
   | GND | White wire (GND) — soldered dupont lead |

   Common ground is mandatory. Never try to power the AC board from the USB adapter. The adapter is plugged into the Pi's USB port and draws its power from there; its 3.3 V/5 V I/O jumper is set to 5 V, and nothing is wired to its VCC pin. The harness's yellow 5 V wire is unused. If your adapter or MCU is 3.3 V-only, insert a level shifter between it and the harness (see the note above).
5. **Mount everything and wire the power.** Tuck the HLK-PM01 into the cavity where the 120 V line enters the unit — its AC input is tapped into that line (hot + neutral). Hot glue the Pi Zero 2W and the USB-TTL adapter to the housing where the original Tuya module sat. Route the HLK-PM01's 5 V output to the Pi's header pins 4 (5 V) and 6 (GND) — the USB-TTL adapter draws its power from the Pi's USB port, and the yellow 5 V harness wire is not used. Since the module is primary-side isolated, tie its GND output to the harness GND (white wire) so the Pi, USB adapter, and AC board all share a common ground. This is a live 120 V connection — use proper insulation/wire nuts, and pick a module whose current rating covers the peaks of the Pi and adapter (WiFi bursts alone can draw over 1 A).
6. **Restore power.** The AC keeps running without the WiFi module, and on most units the IR remote still works because it talks directly to the main board — so you aren't bricked if the bridge misbehaves.

## 2. Setting up the Raspberry Pi Zero 2W

1. Flash **Raspberry Pi OS Lite (64-bit)** to a microSD card with Raspberry Pi Imager, configuring Wi‑Fi during flashing (or set up later over USB-OTG Ethernet).
2. Once it's on the network:

   ```bash
   sudo apt update
   sudo apt install -y python3-serial python3-paho-mqtt git
   git clone https://github.com/Teejer/zokop-mini-split-uart.git
   cd zokop-mini-split-uart
   cp config.example.json config.json
   nano config.json   # fill in your values
   ```

3. Plug the USB-TTL adapter into the Pi's USB port and confirm the device:

   ```bash
   ls /dev/ttyUSB*   # FTDI/CH340/CP2102; some adapters show up as /dev/ttyACM0
   ```

   If you get permission errors: `sudo usermod -aG dialout $USER` (then re-login).
4. **Test the wiring without MQTT** — with the AC off:

   ```bash
   python3 ac_control.py --port /dev/ttyUSB0 --command power_on
   ```

   You should see `TX power_on: ...` followed by an `RX` frame, and the AC should start. Try `--command power_off`, `--temp 78`, `--command fan_high`. `python3 ac_control.py --list` shows all named commands.

   No RX data or no response from the AC? Re-check TX/RX direction (adapter TX → black wire) and the common ground.
5. **Run the bridge:**

   ```bash
   python3 ac_bridge.py
   ```

   You should see `Loaded config`, `Connected to MQTT`, the discovery publishes, and a state frame shortly after (the bridge sends a status ping to request one).
6. **Run at boot:**

```bash
sudo cp ac_bridge.service /etc/systemd/system/
sudoedit /etc/systemd/system/ac_bridge.service   # fix User= and the paths
sudo systemctl daemon-reload
sudo systemctl enable --now ac_bridge
journalctl -u ac_bridge -f
```

## 3. DHT22 ambient sensor (optional)

A DHT22 wired to the Pi's GPIO publishes room temperature and humidity as part of the same MQTT device, and the temperature feeds the climate entity's "current temperature".

**Wiring:** VCC → 3.3 V (pin 1 or 17), GND → GND, DATA → the GPIO set by `dht_gpio` in `config.json` (default GPIO 4 / physical pin 7). DHT22 breakout boards usually carry the 10 kΩ pull-up already; for a bare sensor add a 10 kΩ resistor between DATA and VCC.

Raspbian 13 (trixie) blocks system-wide pip installs, so the Adafruit libraries go in a venv:

```bash
cd ~/acMqtt   # or wherever this repo lives
sudo apt install -y python3-venv gpiod build-essential python3-dev libgpiod-dev liblgpio-dev swig
python3 -m venv venv
venv/bin/pip install adafruit-circuitpython-dht paho-mqtt
sudo usermod -aG gpio $USER   # re-login afterwards
```

> The `build-essential`/`python3-dev`/`libgpiod-dev`/`liblgpio-dev`/`swig` packages are needed
> because the GPIO libraries (`RPi.GPIO`, `lgpio`, `rpi_ws281x`) compile from source on install —
> without them you'll see `Failed building wheel for RPi.GPIO`. `lgpio` in particular fails with
> `error: command 'swig' failed: No such file or directory` unless `swig` is installed, and on
> trixie (Python 3.13) there is no prebuilt `lgpio` wheel at all — PyPI only ships up to cp312 —
> so it always builds from source and needs `liblgpio-dev` for the `lgpio.h` headers. Don't list
> the libgpiod runtime package by hand — trixie renamed it (`libgpiod2` → `libgpiod2t64`) and it's
> pulled in automatically as a dependency of `gpiod` and `libgpiod-dev`.

Test it:

```bash
venv/bin/python ambient_sensor.py   # Ctrl+C to stop
```

You should see `Loaded config`, `Connected to MQTT`, the discovery publishes, and a `PUBLISH …/state/ambient_temp_F = …` line every `ambient_interval_sec` seconds. Then install the service (it uses the venv's Python, so the path in `ExecStart=` matters):

```bash
sudo cp ambient_sensor.service /etc/systemd/system/
sudoedit /etc/systemd/system/ambient_sensor.service   # fix User= and the paths
sudo systemctl daemon-reload
sudo systemctl enable --now ambient_sensor
journalctl -u ambient_sensor -f
```

If reads fail, first try again a second later (the sensor needs ~2 s between reads and occasional checksum failures are normal — the script retries), then check wiring and the pull-up resistor.

### Pi Zero W: kernel `dht11` overlay instead of blinka

On the original Pi Zero W the single-core BCM2835 can't do the userspace bit-bang timing reliably (and blinka's PulseIO helper doesn't work there at all). Use the in-kernel DHT driver, which samples in interrupt context and exposes the readings through IIO:

1. Add to `/boot/firmware/config.txt` (see `boot-config-zero-w.txt`):

   ```
   dtoverlay=dht11,gpio_pin=4     # must match dht_gpio in config.json
   ```

2. Load the IIO core and the DHT driver (they ship as modules and don't always
   autoload from the overlay):

   ```bash
   sudo modprobe industrialio
   sudo modprobe dht11
   ```

   Make it persistent across reboots:

   ```bash
   printf 'industrialio\ndht11\n' | sudo tee /etc/modules-load.d/dht11.conf
   ```

3. Reboot and confirm the device exists:

   ```bash
   dmesg | grep -i dht
   ls /sys/bus/iio/devices/          # expect iio:deviceN with name "dht11"
   cat /sys/bus/iio/devices/iio:device*/in_temp_input
   ```

   Needs kernel ≥ 6.6 with `CONFIG_IIO_DHT11` (module `dht11`) — stock on current
   Raspberry Pi OS kernels. If no IIO device appears after the modules load, the
   running kernel lacks the driver and you'll need a newer kernel.

4. Set `"dht_method": "iio"` in `config.json`. No venv, no pip packages, and no pull-up
   concerns beyond the usual: the script just reads the sysfs files (the overlay's
   `dht11` model reports integer degrees; readings are otherwise identical).

   ```bash
   python3 ambient_sensor.py    # plain python3 works — only paho-mqtt is needed
   ```

## 4. Home Assistant

The bridge publishes MQTT discovery on every connect. With an MQTT integration in Home Assistant pointing at the same broker (same user/password), these entities appear automatically:

- **Climate** (Mini Split AC) — modes auto/cool/dry/fan/heat, temperature 61–88 °F, fan speeds; with `ambient_sensor.py` running, its DHT22 reading shows as the climate's current temperature
- **Power** switch
- **Fan Speed** select (auto, mute, low, mid_low, mid, mid_high, high, extra_high)
- **Horizontal Louver** select
- **Vertical Louver** select
- **Sleep Mode** select (off, standard, aged, child)
- **Beep** switch
- **LED Display** switch
- **Restart Pi** switch (sends `ON` to reboot the Pi)
- **Ambient Temperature** / **Ambient Humidity** sensors (from `ambient_sensor.py`, same device)

### MQTT topics

All topics use the `mqtt_topic_prefix` from `config.json`:

| Direction | Topic | Payload |
|-----------|-------|---------|
| cmd | `{prefix}/cmd/power` | `on` / `off` |
| cmd | `{prefix}/cmd/mode` | `auto`, `cool`, `dry`, `fan`, `heat` |
| cmd | `{prefix}/cmd/temp` | e.g. `75` (°F) |
| cmd | `{prefix}/cmd/fan` (or `/cmd/fan_select`) | `auto` … `extra_high` |
| cmd | `{prefix}/cmd/h_louver`, `{prefix}/cmd/v_louver` | see the entity options |
| cmd | `{prefix}/cmd/sleep` | `off`, `standard`, `aged`, `child` |
| cmd | `{prefix}/cmd/beep`, `{prefix}/cmd/led_display` | `on` / `off` |
| cmd | `{prefix}/cmd/restart_pi` | `ON` |
| state | `{prefix}/state/…` | `power`, `mode`, `fan`, `temp_F`, `h_louver`, `v_louver`, `sleep`, `beep`, `led_display` |
| state | `{prefix}/state/ambient_temp_F`, `/ambient_temp_C`, `/humidity` | from `ambient_sensor.py` (°F, °C, %) |

## 5. Configuration

`config.json` (copy from `config.example.json`; it's gitignored so it never leaves your machine):

| Key | Example | Description |
|-----|---------|-------------|
| `serial_port` | `/dev/ttyUSB0` | USB-TTL adapter device |
| `baud_rate` | `115200` | UART baud rate |
| `mqtt_host` | `192.168.1.100` | MQTT broker address |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_user` | `hass` | Optional broker user |
| `mqtt_pass` | `...` | Optional broker password |
| `mqtt_topic_prefix` | `Zokop-MiniSplit-LivingRoom` | Make it unique per unit if you bridge multiple splits |
| `dht_method` | `gpio` | `gpio` = Adafruit/blinka userspace reads (Pi Zero 2W); `iio` = in-kernel `dht11` overlay via `/sys/bus/iio` (Pi Zero W — blinka timing is unreliable on the BCM2835) |
| `dht_gpio` | `4` | GPIO (BCM) the DHT22 data pin is on — `ambient_sensor.py` only |
| `dht_use_pulseio` | `true` | `gpio` method only: use blinka's hardware pulse capture. The script auto-falls back to bit-bang if it fails (e.g. trixie + libgpiod 2.x); set `false` to force bit-bang |
| `dht_iio_device` | `auto` | `iio` method only: IIO device (`name` or `iio:deviceN`) to read; `auto` picks the first device whose name starts with `dht` |
| `ambient_interval_sec` | `60` | How often the DHT22 is read and published |

You can also pass an alternate config file as an argument: `python3 ac_bridge.py /path/to/other.json`.

## 6. Low power (optional)

`boot-config.txt` / `boot-config-zero-w.txt` and `apply-low-power.sh` tune a headless Pi Zero for minimal draw: undervolting (`arm_freq=600`, `over_voltage=-3`), disabling BT/I2C/SPI/audio/camera, blacklisting unused kernel modules, and stopping unneeded services (bluetooth, avahi, cups, …).

- **Pi Zero 2W** → `boot-config.txt`
- **Pi Zero W** → `boot-config-zero-w.txt`. Don't use the 2W file: `core_freq` below 250 is unsafe on the Zero W's BCM2835 (the SD card clock derives from it — corruption risk); the Zero 2W's BCM2710 auto-clamps it, which is why the 2W file can use 200. `apply-low-power.sh` works unchanged on both.
- Raspberry Pi OS 13 (trixie) has no build for the Zero W (ARMv6 dropped), so those boards stay on Bookworm — everything in this project works the same there.

1. Copy the config for your board to `/boot/firmware/config.txt` (replacing or merging with the existing file; on pre-Bullseye Raspbian the boot partition is `/boot`):

   ```bash
   sudo cp boot-config.txt /boot/firmware/config.txt           # Zero 2W
   sudo cp boot-config-zero-w.txt /boot/firmware/config.txt    # Zero W
   ```
2. `sudo bash apply-low-power.sh`, then reboot.

## Protocol reference

Tuya-style UART frames at 115200 baud, 8N1:

```
A5 01 01 21 <seq> 00 00 <len> <crc16-hi> <crc16-lo> <payload...>
```

- `<len>` = total frame length in bytes
- CRC-16/XMODEM (poly 0x1021, init 0x0000) over every byte before the CRC field
- `0a 0a …` payloads = module→board commands (what this project sends)
- `0c 0c …` payloads = board→module state reports (what this project parses)
- 12-byte `0x23` frames = ACKs

| DP | Meaning | Values |
|----|---------|--------|
| 0x01 | Power | 0 off, 1 on |
| 0x05 | Fan speed | 0 auto … 7 extra_high |
| 0x0E | Horizontal louver | 1–4 flow, 9–13 fixed positions |
| 0x11 | Vertical louver | 1–3 flow, 9–13 fixed positions |
| 0x12 | Mode | 0 auto, 1 cool, 2 dry, 3 fan, 4 heat |
| 0x22 | Sleep | 0 off, 1 standard, 2 aged, 3 child |
| 0x25 | Beep | 0 off, 1 on |
| 0x27 | Target temperature | Fahrenheit setpoint (61–88) |
| 0x73 | LED/display flag | 0 / 1 |

`protoInfo.txt` contains the exact captured frames for every command, and `info.txt` has the 74AHCT125 wiring details.

## Notes & caveats

- Temperature setpoints are in **Fahrenheit** (61–88 °F on this unit).
- Mode commands replay the full captured multi-DP frames as-is, since the board expects the complete packet; all other commands are generated as single-DP frames with a fresh CRC.
- If your unit's wire colors differ, identify them with a multimeter (5 V / GND first) before wiring anything.
- Opening the indoor unit may affect your warranty.

## Credits

The protocol reverse engineering and bridge code for this project were created with the help of Qwen3.8 27B and DeepSeek V4 Flash, both running locally.
