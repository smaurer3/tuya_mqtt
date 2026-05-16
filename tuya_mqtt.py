#!/usr/bin/python3
import tinytuya
import json
import os
import time
import threading
import queue
import paho.mqtt.client as mqtt

# Load devices from devices.json
with open("devices.json") as f:
    devices_info = json.load(f)

# MQTT broker config (shared with tuya_admin)
try:
    with open("mqtt_config.json") as f:
        _cfg = json.load(f)
    MQTT_BROKER = _cfg.get("broker", "192.168.10.1")
    MQTT_PORT = int(_cfg.get("port", 1883))
    MQTT_USER = _cfg.get("username", "")
    MQTT_PASS = _cfg.get("password", "")
except (FileNotFoundError, json.JSONDecodeError):
    MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS = "192.168.10.1", 1883, "", ""

ALIASES_FILE = "aliases.json"
POLL_INTERVAL = 0.3  # seconds

# Per-device queues and state
device_queues = {}   # dev_id -> queue.Queue
last_states = {}     # dev_id -> dps dict
devices = {}         # dev_id -> OutletDevice

# Alias state (alias name <-> device id), hot-reloaded from aliases.json
aliases = {}          # alias -> dev_id
reverse_aliases = {}  # dev_id -> alias
aliases_mtime = 0
aliases_lock = threading.Lock()


def load_aliases():
    """Reload aliases.json if its mtime changed. Safe to call repeatedly."""
    global aliases, reverse_aliases, aliases_mtime
    try:
        st = os.stat(ALIASES_FILE)
    except FileNotFoundError:
        if aliases:
            with aliases_lock:
                aliases, reverse_aliases, aliases_mtime = {}, {}, 0
            print("[ALIAS] aliases.json missing — cleared")
        return

    if st.st_mtime == aliases_mtime:
        return
    try:
        with open(ALIASES_FILE) as f:
            new = json.load(f)
    except Exception as e:
        print(f"[ALIAS] Load error: {e}")
        return

    new_map = {k: v for k, v in new.items() if isinstance(v, str) and v}
    with aliases_lock:
        aliases = new_map
        reverse_aliases = {v: k for k, v in new_map.items()}
        aliases_mtime = st.st_mtime
    print(f"[ALIAS] Loaded {len(new_map)} aliases")


def alias_watch_thread():
    while True:
        load_aliases()
        time.sleep(2)


def resolve_dev_id(token):
    with aliases_lock:
        return aliases.get(token, token)


def alias_for(dev_id):
    with aliases_lock:
        return reverse_aliases.get(dev_id)


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
                alias = alias_for(dev_id)
                for switch, value in dps.items():
                    if last_dps.get(switch) != value:
                        payload = "on" if value else "off"
                        client.publish(f"tuya/{dev_id}/{switch}/state", payload, retain=True)
                        if alias:
                            client.publish(f"tuya/{alias}/{switch}/state", payload, retain=True)
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

    token = topic_parts[1]
    switch = topic_parts[2]
    command = msg.payload.decode().strip().lower()

    # Resolve alias → dev_id (falls through to raw id if not aliased)
    dev_id = resolve_dev_id(token)
    print(f"[MQTT] {token} -> {dev_id}/{switch} -> {command}")

    if dev_id not in device_queues:
        print(f"[MQTT] Unknown device: {token}")
        return

    # Drop it on the device's queue — never blocks the MQTT thread
    device_queues[dev_id].put((command, switch))


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Connected — (re)subscribing")
        # Re-subscribe on every connect so broker restarts don't strand us
        client.subscribe("tuya/+/+/set")
    else:
        print(f"[MQTT] Connect failed: rc={rc}")


def on_disconnect(client, userdata, rc):
    print(f"[MQTT] Disconnected: rc={rc} — paho will auto-reconnect")


# ── Aliases + MQTT setup ──────────────────────────────────────────────────────
load_aliases()
threading.Thread(target=alias_watch_thread, daemon=True, name="alias-watch").start()

client = mqtt.Client()
client.on_message = on_message
client.on_connect = on_connect
client.on_disconnect = on_disconnect
if MQTT_USER:
    client.username_pw_set(MQTT_USER, MQTT_PASS)
client.reconnect_delay_set(min_delay=1, max_delay=60)
client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
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
