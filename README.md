# Zokop Mini Split UART → MQTT Bridge

Replace the Tuya WiFi module in a Zokop 12000 BTU mini split with a Raspberry Pi Zero 2W and a USB-to-TTL serial adapter, giving you full control of the AC over MQTT with native Home Assistant integration.

## How it works

The indoor unit of the Zokop mini split contains a small Tuya WiFi module that talks to the AC's main control board over a 4-wire harness: 5 V, GND, TX, and RX. The data is a Tuya-style UART protocol at 115200 baud, made up of frames containing data points (DPs) for power, temperature, mode, fan, and louvers.

Instead of letting the Tuya module handle the radio, this project:

- **`ac_bridge.py`** — the main bridge. Opens the serial port, publishes AC state to MQTT, accepts commands from MQTT, and auto-publishes Home Assistant MQTT discovery.
- **`ac_control.py`** — a standalone CLI tool for sending/receiving frames without MQTT (handy for testing wiring).
- **`config.example.json`** — sample configuration (copy to `config.json` and edit).
- **`ac_bridge.service`** — systemd unit to run the bridge on boot.
- **`apply-low-power.sh`** + **`boot-config.txt`** — optional low-power tuning for the Pi Zero 2W.
- **`protoInfo.txt`** / **`info.txt`** — reverse-engineered protocol notes and wiring references.

## Parts

| Part | Notes |
|------|-------|
| Raspberry Pi Zero 2W | Any small Pi works; the Zero 2W tucks easily into the indoor unit |
| USB-to-TTL serial adapter | FTDI, CP2102, or CH340 — 115200 baud, 8N1 |
| 5 V ↔ 3.3 V level shifter | The AC board speaks 5 V UART; the adapter is 3.3 V. A bidirectional MOSFET level-shifter module (e.g. BSS138-based) works for both lines, or a 74AHCT125 for TX only — see `info.txt` |
| Dupont wires | |
| 5 V PSU for the Pi | ~2.5 A wall wart; the AC's 5 V rail usually can't supply the Pi |

## 1. Replacing the Tuya WiFi module

> **Warning:** cut power to the AC at the breaker before opening the indoor unit. The unit is a 208/240 V appliance — stay clear of the refrigerant lines, capacitors, and high-voltage wiring while working.

1. **Open the indoor unit.** Slide the front panel up and off. The main control board is visible inside; the Tuya module is a small PCB (often with a visible antenna) plugged into the main board via a short 4-wire harness.
2. **Unplug the harness from the Tuya module.** Don't cut anything — the harness stays attached to the AC board.
3. **Identify the wires** (colors from this project's unit — verify with a multimeter on yours):

   | Wire | Signal | Purpose |
   |------|--------|---------|
   | Yellow | 5 V | Powered the Tuya module; also VCC for the level shifter |
   | White | GND | Common ground |
   | Black | Module → Board | Board's RX input — this is where you send commands |
   | Red | Board → Module | Board's TX output — this is where you receive state |

   Sanity check: yellow/white should measure ~5 V DC between them, and the remaining two wires are the data lines.
4. **Wire through the level shifter.** 3.3 V side to the USB-TTL adapter, 5 V side to the harness:

   | Adapter pin | Via | Harness wire |
   |-------------|-----|--------------|
   | TX | level shifter | Black (board RX) |
   | RX | level shifter | Red (board TX) |
   | GND | direct | White (GND) |
   | VCC (5 V) | direct | Yellow (5 V) |

   Common ground is mandatory. Never try to power the AC board from the USB adapter.
5. **Mount the Pi.** Tuck the Pi Zero 2W and the USB-TTL adapter into the cavity behind the panel, or in a small project box nearby. Power the Pi from its own 5 V PSU; only share GND with the AC wiring.
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

   No RX data or no response from the AC? Re-check TX/RX direction (adapter TX → black wire), the common ground, and the level shifter.
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

## 3. Home Assistant

The bridge publishes MQTT discovery on every connect. With an MQTT integration in Home Assistant pointing at the same broker (same user/password), these entities appear automatically:

- **Climate** (Mini Split AC) — modes auto/cool/dry/fan/heat, temperature 61–88 °F, fan speeds
- **Power** switch
- **Fan Speed** select (auto, mute, low, mid_low, mid, mid_high, high, extra_high)
- **Horizontal Louver** select
- **Vertical Louver** select
- **Sleep Mode** select (off, standard, aged, child)
- **Beep** switch
- **LED Display** switch
- **Restart Pi** switch (sends `ON` to reboot the Pi)

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

## 4. Configuration

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

You can also pass an alternate config file as an argument: `python3 ac_bridge.py /path/to/other.json`.

## 5. Low power (optional)

`boot-config.txt` and `apply-low-power.sh` tune a headless Pi Zero 2W for minimal draw: undervolting (`arm_freq=600`, `over_voltage=-1`), disabling BT/I2C/SPI/audio/camera, blacklisting unused kernel modules, and stopping unneeded services (bluetooth, avahi, cups, …).

1. Replace or merge `boot-config.txt` into `/boot/firmware/config.txt`.
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
