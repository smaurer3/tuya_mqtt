#!/usr/bin/python3
import tinytuya
import json
import time
import threading
import paho.mqtt.client as mqtt
from pprint import pprint

# Load devices from devices.json
with open("devices.json") as f:
    devices_info = json.load(f)

# MQTT setup
MQTT_BROKER = "192.168.10.1"  # replace with your broker IP
MQTT_PORT = 1883
client = mqtt.Client()

def on_message(client, userdata, msg):
    topic_parts = msg.topic.split('/')
    # Expect topic: tuya/{dev_id}/{switch}/set
    if len(topic_parts) != 4 or topic_parts[0] != 'tuya' or topic_parts[3] != 'set':
        return
    dev_id = topic_parts[1]
    switch = topic_parts[2]
    command = msg.payload.decode().lower()
    print (f"MESSAGE: {command}")
    if dev_id not in devices:
        print(f"Unknown device {dev_id}")
        return

    device = devices[dev_id]
    try:
        if command == 'on':
            device.turn_on(int(switch))
        elif command == 'off':
            device.turn_off(int(switch))
        else:
            print(f"Unknown command: {command}")
    except Exception as e:
        print(f"Error controlling {dev_id} switch {switch}: {e}")

client.on_message = on_message
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.subscribe("tuya/+/+/set")
client.loop_start()

# Initialize Tinytuya devices
devices = {}
for dev in devices_info:
    dev_id = dev['id']
    ip = dev.get('ip', '')
    if not ip:
        print(f"Skipping {dev.get('name', dev_id)} (no IP address)")
        continue
    version_str = dev.get('version', '')
    try:
        version = float(version_str) if version_str else 3.3
    except ValueError:
        version = 3.3

    devices[dev_id] = tinytuya.OutletDevice(
        dev_id=dev_id,
        address=ip,
        local_key=dev['key'],
        version=version
    )

# Keep track of last known DPS state to detect changes
last_states = {}

POLL_INTERVAL = .3  # seconds

# Set up each device with a short timeout
for device in devices.values():
    device.set_socketPersistent(False)
    device.set_socketTimeout(3)

def poll_device_thread(dev_id, device):
    while True:
        try:
            data = device.status()
            dps = data.get('dps', {})
            last_dps = last_states.get(dev_id, {})

            # Publish only changes
            for switch, value in dps.items():
                if last_dps.get(switch) != value:
                    topic = f"tuya/{dev_id}/{switch}/state"
                    client.publish(topic, "on" if value else "off", retain=True)

            last_states[dev_id] = dps
        except Exception as e:
            print(f"{dev_id} offline or error: {e}")
        time.sleep(POLL_INTERVAL)

# Start one thread per device
for dev_id, device in devices.items():
    t = threading.Thread(target=poll_device_thread, args=(dev_id, device), daemon=True)
    t.start()

print("Polling started for all devices.")


try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Exiting...")
