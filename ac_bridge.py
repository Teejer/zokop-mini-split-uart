#!/usr/bin/env python3
"""AC MQTT Bridge for A5 UART protocol - with config file, HA discovery, debug mode, and Pi restart."""

import serial
import time
import json
import re
import sys
import subprocess
import paho.mqtt.client as mqtt

# =============== DEFAULT CONFIG (overridden by config file) ===============
DEFAULTS = {
    "serial_port": "/dev/ttyUSB0",
    "baud_rate": 115200,
    "mqtt_host": "192.168.1.100",
    "mqtt_port": 1883,
    "mqtt_user": None,
    "mqtt_pass": None,
    "mqtt_topic_prefix": "Zokop-MiniSplit-SmallBedroom",
    "debug": False,
    "capture_dps": False,
}

def load_config(path=None):
    if path is None:
        path = "config.json"
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
        config = {**DEFAULTS, **cfg}
        print(f"Loaded config from {path}")
    except Exception as e:
        print(f"Config load failed ({e}), using defaults.")
        config = DEFAULTS.copy()
    return config

CONFIG = load_config()

SERIAL_PORT = CONFIG["serial_port"]
BAUD_RATE = CONFIG["baud_rate"]
MQTT_HOST = CONFIG["mqtt_host"]
MQTT_PORT = CONFIG["mqtt_port"]
MQTT_USER = CONFIG["mqtt_user"]
MQTT_PASS = CONFIG["mqtt_pass"]
MQTT_TOPIC_PREFIX = CONFIG["mqtt_topic_prefix"]
DEBUG = CONFIG.get("debug", False)
CAPTURE_DPS = CONFIG.get("capture_dps", False) or DEBUG

STATUS_PING_INTERVAL = 60

# =============== CRC / FRAME FUNCTIONS ===============
def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc

def make_state_frame(payload: bytes, seq: int = 0) -> bytes:
    header = bytes([0xA5, 0x01, 0x01, 0x21, seq & 0xFF, 0x00, 0x00])
    length = 10 + len(payload)
    pre_checksum = header + bytes([length]) + payload
    crc = crc16_xmodem(pre_checksum)
    return header + bytes([length]) + bytes([(crc >> 8) & 0xFF, crc & 0xFF]) + payload

def make_single_dp_frame(dp_id: int, value: int, type_byte: int = 0x00, seq: int = 0) -> bytes:
    payload = bytes([0x0A, 0x0A, type_byte, dp_id, value & 0xFF])
    return make_state_frame(payload, seq)

# =============== EXACT CAPTURED FRAMES ===============
PAYLOADS = {
    "power_on": bytes.fromhex("a5 01 01 21 09 00 00 12 be b6 0a 0a 00 01 01 00 13 00"),
    "power_off": bytes.fromhex("a5 01 01 21 0a 00 00 0f 62 dd 0a 0a 00 01 00"),
    "mode_auto": bytes.fromhex("a5 01 01 21 0e 00 00 24 a7 eb 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 00 00 05 02 00 73 00 00 13 00"),
    "mode_cool": bytes.fromhex("a5 01 01 21 0f 00 00 24 d7 e6 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 01 00 05 00 00 73 01 00 13 00"),
    "mode_dry": bytes.fromhex("a5 01 01 21 10 00 00 24 96 2b 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 02 00 05 02 00 73 00 00 13 00"),
    "mode_fan": bytes.fromhex("a5 01 01 21 11 00 00 24 f0 71 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 03 00 05 02 00 73 00 00 13 00"),
    "mode_heat": bytes.fromhex("a5 01 01 21 12 00 00 24 ee ee 0a 0a 00 02 00 00 0a 28 02 27 00 00 00 4f 00 12 04 00 05 05 00 73 00 00 13 00"),
    "fan_auto": bytes.fromhex("a5 01 01 21 16 00 00 12 7e bc 0a 0a 00 05 00 00 73 01"),
    "fan_mute": bytes.fromhex("a5 01 01 21 17 00 00 12 1b 5c 0a 0a 00 05 01 00 73 00"),
    "fan_low": bytes.fromhex("a5 01 01 21 18 00 00 12 93 63 0a 0a 00 05 02 00 73 00"),
    "fan_mid_low": bytes.fromhex("a5 01 01 21 19 00 00 12 e6 a2 0a 0a 00 05 03 00 73 00"),
    "fan_mid": bytes.fromhex("a5 01 01 21 1a 00 00 12 b2 10 0a 0a 00 05 04 00 73 00"),
    "fan_mid_high": bytes.fromhex("a5 01 01 21 1b 00 00 12 c7 d1 0a 0a 00 05 05 00 73 00"),
    "fan_high": bytes.fromhex("a5 01 01 21 1c 00 00 12 54 46 0a 0a 00 05 06 00 73 00"),
    "fan_extra_high": bytes.fromhex("a5 01 01 21 1d 00 00 15 4d cf 0a 0a 00 26 00 00 05 07 00 73 00"),
    "louver_h_left_right_flow": bytes.fromhex("a5 01 01 21 38 00 00 0f 50 a7 0a 0a 00 0e 01"),
    "louver_h_left_flow": bytes.fromhex("a5 01 01 21 39 00 00 0f 8b e7 0a 0a 00 0e 02"),
    "louver_h_middle_flow": bytes.fromhex("a5 01 01 21 3a 00 00 0f b6 82 0a 0a 00 0e 03"),
    "louver_h_right_flow": bytes.fromhex("a5 01 01 21 3b 00 00 0f 2d 46 0a 0a 00 0e 04"),
    "louver_h_left_fix": bytes.fromhex("a5 01 01 21 3c 00 00 0f 4d 40 0a 0a 00 0e 09"),
    "louver_h_bit_left_fix": bytes.fromhex("a5 01 01 21 3d 00 00 0f 96 00 0a 0a 00 0e 0a"),
    "louver_h_middle_fix": bytes.fromhex("a5 01 01 21 3e 00 00 0f ab 65 0a 0a 00 0e 0b"),
    "louver_h_bit_right_fix": bytes.fromhex("a5 01 01 21 3f 00 00 0f 30 a1 0a 0a 00 0e 0c"),
    "louver_h_right_fix": bytes.fromhex("a5 01 01 21 40 00 00 0f 13 0f 0a 0a 00 0e 0d"),
    "louver_v_up_down_flow": bytes.fromhex("a5 01 01 21 43 00 00 0f ec 8a 0a 0a 00 11 01"),
    "louver_v_up_flow": bytes.fromhex("a5 01 01 21 44 00 00 0f 6d 42 0a 0a 00 11 02"),
    "louver_v_down_flow": bytes.fromhex("a5 01 01 21 45 00 00 0f 96 40 0a 0a 00 11 03"),
    "louver_v_up_fix": bytes.fromhex("a5 01 01 21 46 00 00 0f 1a 4e 0a 0a 00 11 09"),
    "louver_v_above_up_fix": bytes.fromhex("a5 01 01 21 47 00 00 0f c1 0e 0a 0a 00 11 0a"),
    "louver_v_middle_fix": bytes.fromhex("a5 01 01 21 48 00 00 0f 49 7b 0a 0a 00 11 0b"),
    "louver_v_above_down_fix": bytes.fromhex("a5 01 01 21 49 00 00 0f d2 bf 0a 0a 00 11 0c"),
    "louver_v_down_fix": bytes.fromhex("a5 01 01 21 4a 00 00 0f ef da 0a 0a 00 11 0d"),
    "sleep_standard": bytes.fromhex("a5 01 01 21 50 00 00 0f c2 f6 0a 0a 00 22 01"),
    "sleep_aged": bytes.fromhex("a5 01 01 21 51 00 00 0f 19 b6 0a 0a 00 22 02"),
    "sleep_child": bytes.fromhex("a5 01 01 21 52 00 00 0f 24 d3 0a 0a 00 22 03"),
    "beep_off": bytes.fromhex("a5 01 01 21 21 00 00 0f 0b b8 0a 0a 00 25 00"),
    "beep_on": bytes.fromhex("a5 01 01 21 22 00 00 0f 36 dd 0a 0a 00 25 01"),
    "temp_80": bytes.fromhex("a5 01 01 21 23 00 00 12 f9 44 0a 0a 02 27 00 00 00 50"),
    "temp_79": bytes.fromhex("a5 01 01 21 24 00 00 12 12 d1 0a 0a 02 27 00 00 00 4f"),
    "temp_78": bytes.fromhex("a5 01 01 21 25 00 00 12 01 85 0a 0a 02 27 00 00 00 4e"),
}

STATUS_PING_FRAME = bytes.fromhex("a5 01 00 21 00 00 00 0c d3 7d 0c 0c")

# =============== STATE PARSING ===============
DP_NAMES = {
    0x01: "power",
    0x05: "fan",
    0x0E: "h_louver",
    0x11: "v_louver",
    0x12: "mode",
    0x22: "sleep",
    0x25: "beep",
    0x27: "temp_F",
    0x73: "led_display",   # placeholder – likely fan auto flag
}

def format_dp_value(dp, val):
    if dp == 0x01: return "on" if val else "off"
    if dp == 0x05:
        return {0:"auto",1:"mute",2:"low",3:"mid_low",4:"mid",5:"mid_high",6:"high",7:"extra_high"}.get(val, f"unknown({val})")
    if dp == 0x0E:
        return {8:"off",1:"left_right_flow",2:"left_flow",3:"middle_flow",4:"right_flow",
                9:"left_fix",10:"bit_left_fix",11:"middle_fix",12:"bit_right_fix",13:"right_fix"}.get(val, f"unknown({val})")
    if dp == 0x11:
        return {8:"off",1:"up_down_flow",2:"up_flow",3:"down_flow",
                9:"up_fix",10:"above_up_fix",11:"middle_fix",12:"above_down_fix",13:"down_fix"}.get(val, f"unknown({val})")
    if dp == 0x12:
        return {0:"auto",1:"cool",2:"dry",3:"fan",4:"heat"}.get(val, f"unknown({val})")
    if dp == 0x22:
        return {0:"off",1:"standard",2:"aged",3:"child"}.get(val, f"unknown({val})")
    if dp == 0x25: return "on" if val else "off"
    if dp == 0x27:
        if 61 <= val <= 88: return str(val)
        return None
    if dp == 0x73: return "on" if val else "off"   # placeholder
    return str(val)

def parse_state_frame(frame, debug=False):
    if len(frame) < 12: return None, None
    payload = frame[10:]
    if not payload.startswith(b"\x0c\x0c"): return None, None
    data = payload[2:]
    i = 0
    state = {}
    raw_dps = {}
    while i < len(data):
        typ = data[i]
        if typ not in (0x00,0x01,0x02):
            if debug:
                print(f"  STOP parsing at offset {i}: unknown type byte 0x{typ:02X}, {len(data)-i} bytes left")
            break
        if i+1 >= len(data): break
        dp = data[i+1]
        vlen = 1 if typ == 0x00 else (2 if typ == 0x01 else 4)
        if i+2+vlen > len(data):
            if debug:
                print(f"  TRUNCATED dp 0x{dp:02X}")
            break
        val = int.from_bytes(data[i+2:i+2+vlen], "big")
        name = DP_NAMES.get(dp, "UNKNOWN")
        if debug:
            print(f"  DP 0x{dp:02X} ({name}) type={typ} val={val}")
        raw_dps[f"0x{dp:02X}"] = val
        if dp in DP_NAMES:
            formatted = format_dp_value(dp, val)
            if formatted is not None:
                state[DP_NAMES[dp]] = formatted
        i += 2+vlen
    return (state if state else None), (raw_dps if raw_dps else None)

# =============== BRIDGE CLASS ===============
class ACBridge:
    def __init__(self):
        self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        self.seq = 0
        self.rx_buffer = b""
        self.state = {}
        self.debug = DEBUG   # or CONFIG.get("debug", False)
        self.seen_dps = set()

    def extract_frames(self, data):
        frames = []
        self.rx_buffer += data
        while True:
            idx = self.rx_buffer.find(b'\xA5')
            if idx == -1:
                self.rx_buffer = b""
                break
            if idx > 0:
                self.rx_buffer = self.rx_buffer[idx:]
            if len(self.rx_buffer) < 10:
                break
            length = self.rx_buffer[7]
            if len(self.rx_buffer) < length:
                break
            frame = self.rx_buffer[:length]
            self.rx_buffer = self.rx_buffer[length:]
            frames.append(frame)
        return frames

    def handle_serial(self):
        data = self.ser.read(256)
        if not data:
            return
        for frame in self.extract_frames(data):
            if len(frame) < 10: continue
            length = frame[7]
            if len(frame) != length: continue
            expected = (frame[8] << 8) | frame[9]
            payload = frame[10:]
            calc = crc16_xmodem(frame[:8] + payload)
            if calc != expected:
                if self.debug:
                    print(f"CRC MISMATCH: expected 0x{expected:04X} calc 0x{calc:04X} frame: {frame.hex(' ').upper()}")
                continue

            if self.debug:
                print(f"RX frame ({len(frame)}): {frame.hex(' ').upper()}")

            if frame[3] in (0x21, 0x23):
                state, raw_dps = parse_state_frame(frame, self.debug)
                if raw_dps and CAPTURE_DPS:
                    self.report_raw_dps(raw_dps, frame)
                if state:
                    self.state.update(state)
                    self.publish_state()
                if frame[3] == 0x23:
                    self.publish_state()
            elif self.debug:
                print(f"RX unhandled frame cmd=0x{frame[3]:02X}: {frame.hex(' ').upper()}")

    def report_raw_dps(self, raw_dps, frame):
        mqtt_client.publish(
            f"{MQTT_TOPIC_PREFIX}/debug/dps",
            json.dumps(raw_dps), retain=True,
        )
        for key, val in raw_dps.items():
            if key not in self.seen_dps:
                self.seen_dps.add(key)
                print(f"NEW DP {key} = {val}   frame: {frame.hex(' ').upper()}")

    def publish_state(self):
        for key, value in self.state.items():
            topic = f"{MQTT_TOPIC_PREFIX}/state/{key}"
            mqtt_client.publish(topic, str(value), retain=True)
            print(f"PUBLISH {topic} = {value}")

    def send_command(self, name):
        if name in PAYLOADS:
            frame = PAYLOADS[name]
            print(f"TX {name}: {frame.hex(' ').upper()}")
            self.ser.write(frame)
            self.seq = (self.seq + 1) & 0xFF
            return True

        parts = name.split("_")

        if parts[0] == "mode":
            mode_map = {"auto":0, "cool":1, "dry":2, "fan":3, "heat":4}
            if parts[1] in mode_map:
                self.send_dynamic_dp(0x12, mode_map[parts[1]])
                return True

        if parts[0] == "fan":
            fan_map = {"auto":0, "mute":1, "low":2, "mid_low":3, "mid":4, "mid_high":5, "high":6, "extra_high":7}
            fan_name = "_".join(parts[1:])
            if fan_name in fan_map:
                self.send_dynamic_dp(0x05, fan_map[fan_name])
                return True

        if parts[0] == "louver" and parts[1] == "h":
            h_map = {"off":8, "left_right_flow":1, "left_flow":2, "middle_flow":3, "right_flow":4,
                     "left_fix":9, "bit_left_fix":10, "middle_fix":11, "bit_right_fix":12, "right_fix":13}
            h_name = "_".join(parts[2:])
            if h_name in h_map:
                self.send_dynamic_dp(0x0E, h_map[h_name])
                return True

        if parts[0] == "louver" and parts[1] == "v":
            v_map = {"off":8, "up_down_flow":1, "up_flow":2, "down_flow":3,
                     "up_fix":9, "above_up_fix":10, "middle_fix":11, "above_down_fix":12, "down_fix":13}
            v_name = "_".join(parts[2:])
            if v_name in v_map:
                self.send_dynamic_dp(0x11, v_map[v_name])
                return True

        if parts[0] == "sleep":
            sleep_map = {"off":0, "standard":1, "aged":2, "child":3}
            if parts[1] in sleep_map:
                self.send_dynamic_dp(0x22, sleep_map[parts[1]])
                return True

        if parts[0] == "beep":
            self.send_dynamic_dp(0x25, 1 if parts[1] == "on" else 0)
            return True

        if parts[0] == "led" and parts[1] == "display":
            # Not confirmed – use dynamic DP 0x73 for now
            self.send_dynamic_dp(0x73, 1 if parts[2] == "on" else 0)
            return True

        print(f"Unknown command: {name}")
        return False

    def send_dynamic_dp(self, dp, value):
        frame = make_single_dp_frame(dp, value, 0x00, self.seq)
        print(f"TX dynamic dp={dp} value={value}: {frame.hex(' ').upper()}")
        self.ser.write(frame)
        self.seq = (self.seq + 1) & 0xFF

    def send_temperature(self, temp):
        if 61 <= temp <= 88:
            payload = bytes([0x0A,0x0A,0x02,0x27,0x00,0x00,0x00,temp])
            frame = make_state_frame(payload, self.seq)
            print(f"TX temp_{temp}: {frame.hex(' ').upper()}")
            self.ser.write(frame)
            self.seq = (self.seq + 1) & 0xFF
            return True
        return False

    def send_status_ping(self):
        print(f"TX status_ping: {STATUS_PING_FRAME.hex(' ').upper()}")
        self.ser.write(STATUS_PING_FRAME)

# =============== HA DISCOVERY ===============
def _discovery_object_id(prefix: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', prefix.lower())

def publish_discovery(client):
    obj_id = _discovery_object_id(MQTT_TOPIC_PREFIX)

    device_info = {
        "identifiers": [f"{obj_id}_device"],
        "name": MQTT_TOPIC_PREFIX,
        "model": "Mini Split AC",
        "manufacturer": "Zokop",
    }

    # Climate
    climate_config = {
        "name": "Mini Split AC",
        "unique_id": f"{obj_id}_climate",
        "device": device_info,
        "mode_command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/mode",
        "mode_state_topic": f"{MQTT_TOPIC_PREFIX}/state/mode",
        "temperature_command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/temp",
        "temperature_state_topic": f"{MQTT_TOPIC_PREFIX}/state/temp_F",
        "temperature_unit": "F",
        "fan_mode_command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/fan",
        "fan_mode_state_topic": f"{MQTT_TOPIC_PREFIX}/state/fan",
        "modes": ["auto", "cool", "dry", "fan", "heat"],
        "fan_modes": ["auto", "mute", "low", "mid_low", "mid", "mid_high", "high", "extra_high"],
        "min_temp": 61,
        "max_temp": 88,
        "temp_step": 1,
        "retain": True,
    }
    client.publish(f"homeassistant/climate/{obj_id}/config", json.dumps(climate_config), retain=True)
    print(f"Published climate discovery for {obj_id}")

    # Power switch
    switch_config = {
        "name": "Power",
        "unique_id": f"{obj_id}_power",
        "device": device_info,
        "command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/power",
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/power",
        "payload_on": "on",
        "payload_off": "off",
        "retain": True,
    }
    client.publish(f"homeassistant/switch/{obj_id}_power/config", json.dumps(switch_config), retain=True)
    print(f"Published power switch discovery for {obj_id}")

    # Fan speed select
    fan_select = {
        "name": "Fan Speed",
        "unique_id": f"{obj_id}_fan_select",
        "device": device_info,
        "command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/fan_select",
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/fan",
        "options": ["auto","mute","low","mid_low","mid","mid_high","high","extra_high"],
        "retain": True,
    }
    client.publish(f"homeassistant/select/{obj_id}_fan/config", json.dumps(fan_select), retain=True)
    print(f"Published fan select discovery for {obj_id}")

    # Horizontal louver select
    h_louver_select = {
        "name": "Horizontal Louver",
        "unique_id": f"{obj_id}_h_louver",
        "device": device_info,
        "command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/h_louver",
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/h_louver",
        "options": ["off","left_right_flow","left_flow","middle_flow","right_flow",
                    "left_fix","bit_left_fix","middle_fix","bit_right_fix","right_fix"],
        "retain": True,
    }
    client.publish(f"homeassistant/select/{obj_id}_h_louver/config", json.dumps(h_louver_select), retain=True)
    print(f"Published h_louver select discovery for {obj_id}")

    # Vertical louver select
    v_louver_select = {
        "name": "Vertical Louver",
        "unique_id": f"{obj_id}_v_louver",
        "device": device_info,
        "command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/v_louver",
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/v_louver",
        "options": ["off","up_down_flow","up_flow","down_flow",
                    "up_fix","above_up_fix","middle_fix","above_down_fix","down_fix"],
        "retain": True,
    }
    client.publish(f"homeassistant/select/{obj_id}_v_louver/config", json.dumps(v_louver_select), retain=True)
    print(f"Published v_louver select discovery for {obj_id}")

    # Sleep select
    sleep_select = {
        "name": "Sleep Mode",
        "unique_id": f"{obj_id}_sleep",
        "device": device_info,
        "command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/sleep",
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/sleep",
        "options": ["off","standard","aged","child"],
        "retain": True,
    }
    client.publish(f"homeassistant/select/{obj_id}_sleep/config", json.dumps(sleep_select), retain=True)
    print(f"Published sleep select discovery for {obj_id}")

    # Beep switch
    beep_switch = {
        "name": "Beep",
        "unique_id": f"{obj_id}_beep",
        "device": device_info,
        "command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/beep",
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/beep",
        "payload_on": "on",
        "payload_off": "off",
        "retain": True,
    }
    client.publish(f"homeassistant/switch/{obj_id}_beep/config", json.dumps(beep_switch), retain=True)
    print(f"Published beep switch discovery for {obj_id}")

    # LED Display switch (placeholder - might not work until correct DP is found)
    led_display_switch_config = {
        "name": "LED Display",
        "unique_id": f"{obj_id}_led_display",
        "device": device_info,
        "command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/led_display",
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/led_display",
        "payload_on": "on",
        "payload_off": "off",
        "retain": True,
    }
    client.publish(f"homeassistant/switch/{obj_id}_led_display/config", json.dumps(led_display_switch_config), retain=True)
    print(f"Published LED Display switch discovery for {obj_id}")

    # Restart Pi switch
    restart_switch_config = {
        "name": "Restart Pi",
        "unique_id": f"{obj_id}_restart_pi",
        "device": device_info,
        "command_topic": f"{MQTT_TOPIC_PREFIX}/cmd/restart_pi",
        "payload_on": "ON",
        "payload_off": "OFF",
        "retain": True,
    }
    client.publish(f"homeassistant/switch/{obj_id}_restart_pi/config", json.dumps(restart_switch_config), retain=True)
    print(f"Published restart Pi switch discovery for {obj_id}")

# =============== MQTT CALLBACKS ===============
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Connected to MQTT")
        client.subscribe(f"{MQTT_TOPIC_PREFIX}/cmd/#")
        publish_discovery(client)
        bridge.send_status_ping()
    else:
        print(f"MQTT connection failed: {reason_code}")

def on_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode().strip()
    parts = topic.split("/")[-1]

    # Restart Pi command
    if parts == "restart_pi":
        if payload == "ON":
            print("Restarting Raspberry Pi...")
            subprocess.call(["sudo", "systemctl", "reboot", "-i"])
        return

    # Temperature
    if parts == "temp" or parts.startswith("temp_"):
        try:
            if parts == "temp":
                temp = int(float(payload))
            else:
                temp = int(float(parts.split("_")[1]))
            bridge.send_temperature(temp)
        except (ValueError, TypeError):
            print(f"Invalid temperature: {payload}")
        return

    # LED Display command
    if parts == "led_display":
        if payload in ("on", "off"):
            bridge.send_dynamic_dp(0x73, 1 if payload == "on" else 0)
        return

    # Direct command
    if parts in PAYLOADS:
        bridge.send_command(parts)
        return

    # Common mappings
    if parts == "power":
        bridge.send_command("power_on" if payload == "on" else "power_off")
    elif parts == "mode":
        mapping = {"auto":"mode_auto", "cool":"mode_cool", "dry":"mode_dry", "fan":"mode_fan", "heat":"mode_heat"}
        if payload in mapping:
            bridge.send_command(mapping[payload])
    elif parts == "fan" or parts == "fan_select":
        mapping = {"auto":"fan_auto", "mute":"fan_mute", "low":"fan_low", "mid_low":"fan_mid_low",
                   "mid":"fan_mid", "mid_high":"fan_mid_high", "high":"fan_high", "extra_high":"fan_extra_high"}
        if payload in mapping:
            bridge.send_command(mapping[payload])
    elif parts == "h_louver":
        mapping = {"off":"louver_h_off", "left_right_flow":"louver_h_left_right_flow", "left_flow":"louver_h_left_flow",
                   "middle_flow":"louver_h_middle_flow", "right_flow":"louver_h_right_flow",
                   "left_fix":"louver_h_left_fix", "bit_left_fix":"louver_h_bit_left_fix",
                   "middle_fix":"louver_h_middle_fix", "bit_right_fix":"louver_h_bit_right_fix",
                   "right_fix":"louver_h_right_fix"}
        if payload in mapping:
            bridge.send_command(mapping[payload])
    elif parts == "v_louver":
        mapping = {"off":"louver_v_off", "up_down_flow":"louver_v_up_down_flow", "up_flow":"louver_v_up_flow",
                   "down_flow":"louver_v_down_flow", "up_fix":"louver_v_up_fix", "above_up_fix":"louver_v_above_up_fix",
                   "middle_fix":"louver_v_middle_fix", "above_down_fix":"louver_v_above_down_fix",
                   "down_fix":"louver_v_down_fix"}
        if payload in mapping:
            bridge.send_command(mapping[payload])
    elif parts == "sleep":
        mapping = {"off":"sleep_off", "standard":"sleep_standard", "aged":"sleep_aged", "child":"sleep_child"}
        if payload in mapping:
            bridge.send_command(mapping[payload])
    elif parts == "beep":
        bridge.send_command("beep_on" if payload == "on" else "beep_off")

# =============== MAIN ===============
if __name__ == "__main__":
    if len(sys.argv) > 1:
        CONFIG = load_config(sys.argv[1])
        SERIAL_PORT = CONFIG["serial_port"]
        BAUD_RATE = CONFIG["baud_rate"]
        MQTT_HOST = CONFIG["mqtt_host"]
        MQTT_PORT = CONFIG["mqtt_port"]
        MQTT_USER = CONFIG["mqtt_user"]
        MQTT_PASS = CONFIG["mqtt_pass"]
        MQTT_TOPIC_PREFIX = CONFIG["mqtt_topic_prefix"]
        DEBUG = CONFIG.get("debug", False)
        CAPTURE_DPS = CONFIG.get("capture_dps", False) or DEBUG

    bridge = ACBridge()

    mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
    mqtt_client.loop_start()

    last_ping_time = time.time()
    try:
        while True:
            bridge.handle_serial()
            now = time.time()
            if now - last_ping_time >= STATUS_PING_INTERVAL:
                bridge.send_status_ping()
                last_ping_time = now
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        mqtt_client.loop_stop()
        bridge.ser.close()
