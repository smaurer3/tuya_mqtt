#!/usr/bin/python3
import tinytuya
import json
import time
import threading
import queue
import paho.mqtt.client as mqtt

# Load devices from devices.json
with open("devices.json") as f:
    devices_info = json.load(f)

# MQTT setup
MQTT_BROKER = "192.168.10.1"  # replace with your broker IP
MQTT_PORT = 1883
client = mqtt.Client()

POLL_INTERVAL = 0.3  # seconds

# Per-device queues and state
device_queues = {}   # dev_id -> queue.Queue
last_states = {}     # dev_id -> dps dict
devices = {}         # dev_id -> OutletDevice


def device_worker(dev_id, device, cmd_queue):
    """
    Single worker thread per device. Serializes all status polls and
    commands so the socket is never accessed from two threads at once.

    Queue items are either:
      - None                          → poll for status
      - ('on'|'off', switch_number)   → send a command, then poll
    """
    while True:
        try:
            item = cmd_queue.get(timeout=POLL_INTERVAL)
        except queue.Empty:
            item = None  # timeout — fall through to a poll

        try:
            if item is not None:
                command, switch = item
                try:
                    if command == 'on':
                        device.turn_on(int(switch))
                    elif command == 'off':
                        device.turn_off(int(switch))
                    print(f"[{dev_id}] switch {switch} -> {command} OK")
                except Exception as e:
                    print(f"[{dev_id}] command error (switch {switch}, {command}): {e}")

            # Always poll after a command (or on the regular timer)
            try:
                data = device.status()
                if data is None:
                    raise Exception("No response from device")
                dps = data.get('dps', {})

                last_dps = last_states.get(dev_id, {})
                for switch, value in dps.items():
                    if last_dps.get(switch) != value:
                        topic = f"tuya/{dev_id}/{switch}/state"
                        client.publish(topic, "on" if value else "off", retain=True)
                last_states[dev_id] = dps

            except Exception as e:
                print(f"[{dev_id}] poll error: {e}")

        except Exception as e:
            print(f"[{dev_id}] worker error: {e}")

        finally:
            if item is not None:
                cmd_queue.task_done()


def on_message(client, userdata, msg):
    topic_parts = msg.topic.split('/')
    if len(topic_parts) != 4 or topic_parts[0] != 'tuya' or topic_parts[3] != 'set':
        return

    dev_id = topic_parts[1]
    switch = topic_parts[2]
    command = msg.payload.decode().strip().lower()

    print(f"[MQTT] {dev_id}/{switch} -> {command}")

    if dev_id not in device_queues:
        print(f"[MQTT] Unknown device: {dev_id}")
        return

    # Drop it on the device's queue — never blocks the MQTT thread
    device_queues[dev_id].put((command, switch))


# ── MQTT setup ────────────────────────────────────────────────────────────────
client.on_message = on_message
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.subscribe("tuya/+/+/set")
client.loop_start()

# ── Device init ───────────────────────────────────────────────────────────────
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

    d = tinytuya.OutletDevice(
        dev_id=dev_id,
        address=ip,
        local_key=dev['key'],
        version=version,
    )
    d.set_socketPersistent(False)
    d.set_socketTimeout(3)
    devices[dev_id] = d

    # One queue + one worker thread per device
    q = queue.Queue()
    device_queues[dev_id] = q
    t = threading.Thread(
        target=device_worker,
        args=(dev_id, d, q),
        daemon=True,
        name=f"worker-{dev_id}",
    )
    t.start()
    print(f"Started worker for {dev.get('name', dev_id)} ({dev_id})")

print("All device workers started.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Exiting...")