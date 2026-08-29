#!/usr/bin/env python3
"""DHT22 ambient temperature/humidity MQTT publisher for the Zokop Mini Split.

Runs as a separate service alongside ac_bridge.py, using the same config.json,
MQTT broker, and topic prefix. Publishes Home Assistant discovery for ambient
temperature and humidity sensors that attach to the same HA device as the AC
entities (same device identifiers as ac_bridge.py's discovery).
"""

import time
import json
import re
import sys

import board
import adafruit_dht
import paho.mqtt.client as mqtt

# =============== DEFAULT CONFIG (overridden by config file) ===============
DEFAULTS = {
    "mqtt_host": "192.168.1.100",
    "mqtt_port": 1883,
    "mqtt_user": None,
    "mqtt_pass": None,
    "mqtt_topic_prefix": "Zokop-MiniSplit-SmallBedroom",
    "dht_gpio": 4,
    "dht_use_pulseio": True,
    "ambient_interval_sec": 60,
    "debug": False,
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

MQTT_HOST = CONFIG["mqtt_host"]
MQTT_PORT = CONFIG["mqtt_port"]
MQTT_USER = CONFIG["mqtt_user"]
MQTT_PASS = CONFIG["mqtt_pass"]
MQTT_TOPIC_PREFIX = CONFIG["mqtt_topic_prefix"]
DHT_GPIO = CONFIG["dht_gpio"]
DHT_USE_PULSEIO = CONFIG.get("dht_use_pulseio", True)
AMBIENT_INTERVAL = CONFIG["ambient_interval_sec"]
DEBUG = CONFIG.get("debug", False)

DHT_READ_RETRIES = 3
DHT_RETRY_DELAY = 2.1  # DHT22 needs ~2s between reads

last_reading = {}

# =============== HA DISCOVERY ===============
def _discovery_object_id(prefix: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', prefix.lower())

def publish_discovery(client):
    obj_id = _discovery_object_id(MQTT_TOPIC_PREFIX)

    # Must match ac_bridge.py's device_info exactly so HA merges the
    # sensors into the same device as the AC entities.
    device_info = {
        "identifiers": [f"{obj_id}_device"],
        "name": MQTT_TOPIC_PREFIX,
        "model": "Mini Split AC",
        "manufacturer": "Zokop",
    }

    temp_config = {
        "name": "Ambient Temperature",
        "unique_id": f"{obj_id}_ambient_temp_F",
        "device": device_info,
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/ambient_temp_F",
        "unit_of_measurement": "°F",
        "device_class": "temperature",
        "state_class": "measurement",
        "retain": True,
    }
    client.publish(f"homeassistant/sensor/{obj_id}_ambient_temp/config", json.dumps(temp_config), retain=True)
    print(f"Published ambient temperature discovery for {obj_id}")

    humidity_config = {
        "name": "Ambient Humidity",
        "unique_id": f"{obj_id}_ambient_humidity",
        "device": device_info,
        "state_topic": f"{MQTT_TOPIC_PREFIX}/state/humidity",
        "unit_of_measurement": "%",
        "device_class": "humidity",
        "state_class": "measurement",
        "retain": True,
    }
    client.publish(f"homeassistant/sensor/{obj_id}_ambient_humidity/config", json.dumps(humidity_config), retain=True)
    print(f"Published ambient humidity discovery for {obj_id}")

# =============== SENSOR ===============
def read_dht(sensor):
    for attempt in range(1, DHT_READ_RETRIES + 1):
        try:
            temp_c = sensor.temperature
            humidity = sensor.humidity
            if temp_c is None or humidity is None:
                raise RuntimeError("sensor returned None")
            if not (-40.0 <= temp_c <= 80.0 and 0.0 <= humidity <= 100.0):
                raise RuntimeError(f"out of range: temp_c={temp_c} humidity={humidity}")
            return temp_c, humidity
        except Exception as e:
            if DEBUG:
                print(f"DHT read attempt {attempt}/{DHT_READ_RETRIES} failed: {e}")
            time.sleep(DHT_RETRY_DELAY)
    return None, None

def make_sensor(pin):
    if DHT_USE_PULSEIO:
        try:
            return adafruit_dht.DHT22(pin)
        except Exception as e:
            print(
                f"PulseIn init failed ({e}); using bit-bang (use_pulseio=False). "
                "This is expected on trixie, where libgpiod 2.x (libgpiod.so.3) can't load "
                "blinka's libgpiod 1.x helper. Reads are timing-sensitive but the loop retries."
            )
    return adafruit_dht.DHT22(pin, use_pulseio=False)

def publish_reading(temp_c, humidity):
    temp_f = temp_c * 9.0 / 5.0 + 32.0
    values = {
        "ambient_temp_C": f"{temp_c:.1f}",
        "ambient_temp_F": f"{temp_f:.1f}",
        "humidity": f"{humidity:.1f}",
    }
    last_reading.update(values)
    if not mqtt_client.is_connected():
        if DEBUG:
            print(f"Skipped publish (disconnected): {values}")
        return
    for key, value in values.items():
        topic = f"{MQTT_TOPIC_PREFIX}/state/{key}"
        mqtt_client.publish(topic, value, retain=True)
        print(f"PUBLISH {topic} = {value}")

# =============== MQTT CALLBACKS ===============
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Connected to MQTT")
        publish_discovery(client)
        for key, value in last_reading.items():
            client.publish(f"{MQTT_TOPIC_PREFIX}/state/{key}", value, retain=True)
    else:
        print(f"MQTT connection failed: {reason_code}")

# =============== MAIN ===============
if __name__ == "__main__":
    if len(sys.argv) > 1:
        CONFIG = load_config(sys.argv[1])
        MQTT_HOST = CONFIG["mqtt_host"]
        MQTT_PORT = CONFIG["mqtt_port"]
        MQTT_USER = CONFIG["mqtt_user"]
        MQTT_PASS = CONFIG["mqtt_pass"]
        MQTT_TOPIC_PREFIX = CONFIG["mqtt_topic_prefix"]
        DHT_GPIO = CONFIG["dht_gpio"]
        DHT_USE_PULSEIO = CONFIG.get("dht_use_pulseio", True)
        AMBIENT_INTERVAL = CONFIG["ambient_interval_sec"]
        DEBUG = CONFIG.get("debug", False)

    pin = getattr(board, f"D{DHT_GPIO}")
    sensor = make_sensor(pin)
    print(f"Using DHT22 on GPIO {DHT_GPIO}, publishing every {AMBIENT_INTERVAL}s under {MQTT_TOPIC_PREFIX}")

    mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"{_discovery_object_id(MQTT_TOPIC_PREFIX)}_ambient")
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    mqtt_client.on_connect = on_connect

    while True:
        try:
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            break
        except Exception as e:
            print(f"MQTT connect failed ({e}), retrying in 5s...")
            time.sleep(5)
    mqtt_client.loop_start()

    try:
        while True:
            temp_c, humidity = read_dht(sensor)
            if temp_c is not None:
                publish_reading(temp_c, humidity)
            else:
                print("DHT22 read failed after retries, skipping this interval")
            time.sleep(AMBIENT_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        mqtt_client.loop_stop()
        sensor.exit()
