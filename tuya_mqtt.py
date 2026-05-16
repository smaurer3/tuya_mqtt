#!/usr/bin/python3
import tinytuya
import json
import os
import time
import threading
import queue
import atexit
import paho.mqtt.client as mqtt

# RPi.GPIO is only present on a Pi — make it optional so the file can be
# read/imported on a dev machine
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    GPIO_AVAILABLE = False
    print("[GPIO] RPi.GPIO not available — GPIO features disabled")

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
GPIO_CONFIG_FILE = "gpio_config.json"
POLL_INTERVAL = 0.3  # seconds
GPIO_POLL_INTERVAL = 0.5  # seconds — how often to check input pins

# Per-device queues and state
device_queues = {}   # dev_id -> queue.Queue
last_states = {}     # dev_id -> dps dict
devices = {}         # dev_id -> OutletDevice

# Alias state (alias name <-> device id), hot-reloaded from aliases.json
aliases = {}          # alias -> dev_id
reverse_aliases = {}  # dev_id -> alias
aliases_mtime = 0
aliases_lock = threading.Lock()

# GPIO state, hot-reloaded from gpio_config.json
gpio_inputs = {}      # pin -> {config, last_published}
gpio_outputs = {}     # topic -> {config, pin}
gpio_pins_setup = set()  # pins we've initialised — used for cleanup
gpio_config_mtime = 0
gpio_lock = threading.Lock()


# ── Aliases ───────────────────────────────────────────────────────────────────
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


# ── GPIO ──────────────────────────────────────────────────────────────────────
def _pull_const(pull):
    if not GPIO_AVAILABLE:
        return None
    return {"up": GPIO.PUD_UP, "down": GPIO.PUD_DOWN}.get(str(pull).lower(), GPIO.PUD_OFF)


def _apply_gpio_config(cfg):
    """Diff cfg against current state and (de)init pins. Subscribes / unsubscribes
    output topics on the MQTT client. Caller holds gpio_lock."""
    global gpio_inputs, gpio_outputs

    new_inputs = {}
    for inp in cfg.get("inputs", []):
        pin = int(inp.get("pin"))
        new_inputs[pin] = {"config": inp, "last_published": None}

    new_outputs = {}
    for out in cfg.get("outputs", []):
        topic = out.get("topic", "").strip()
        if not topic:
            continue
        new_outputs[topic] = {"config": out, "pin": int(out.get("pin"))}

    # Determine pin churn: which pins go away, which are new
    old_pins = {p for p in gpio_inputs} | {o["pin"] for o in gpio_outputs.values()}
    new_pins = set(new_inputs) | {o["pin"] for o in new_outputs.values()}

    pins_removed = old_pins - new_pins
    pins_added = new_pins - old_pins

    if GPIO_AVAILABLE:
        for pin in pins_removed:
            try:
                GPIO.cleanup(pin)
            except Exception as e:
                print(f"[GPIO] cleanup pin {pin} error: {e}")
            gpio_pins_setup.discard(pin)

        for pin in pins_added:
            # Decide direction: prefer input declaration if both somehow conflict
            if pin in new_inputs:
                pull = _pull_const(new_inputs[pin]["config"].get("pull"))
                GPIO.setup(pin, GPIO.IN, pull_up_down=pull)
                print(f"[GPIO] pin {pin} = INPUT pull={new_inputs[pin]['config'].get('pull','none')}")
            else:
                # find the output binding for this pin
                out = next(o for o in new_outputs.values() if o["pin"] == pin)
                initial = str(out["config"].get("initial", "low")).lower()
                GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH if initial == "high" else GPIO.LOW)
                print(f"[GPIO] pin {pin} = OUTPUT initial={initial}")
            gpio_pins_setup.add(pin)

    # MQTT subscription churn for outputs (subscribe new, unsubscribe gone)
    topics_old = set(gpio_outputs.keys())
    topics_new = set(new_outputs.keys())
    if client is not None:
        for t in (topics_old - topics_new):
            try: client.unsubscribe(t)
            except Exception: pass
        for t in (topics_new - topics_old):
            try: client.subscribe(t)
            except Exception: pass

    gpio_inputs = new_inputs
    gpio_outputs = new_outputs


def load_gpio_config(force=False):
    """Reload gpio_config.json if its mtime changed."""
    global gpio_config_mtime
    try:
        st = os.stat(GPIO_CONFIG_FILE)
    except FileNotFoundError:
        if gpio_inputs or gpio_outputs:
            with gpio_lock:
                _apply_gpio_config({"inputs": [], "outputs": []})
                gpio_config_mtime = 0
            print("[GPIO] gpio_config.json missing — cleared")
        return

    if not force and st.st_mtime == gpio_config_mtime:
        return
    try:
        with open(GPIO_CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[GPIO] Load error: {e}")
        return

    with gpio_lock:
        _apply_gpio_config(cfg)
        gpio_config_mtime = st.st_mtime
    print(f"[GPIO] Loaded {len(gpio_inputs)} inputs, {len(gpio_outputs)} outputs")


def gpio_watch_thread():
    while True:
        load_gpio_config()
        time.sleep(2)


def gpio_input_poll_thread():
    """Poll all configured input pins and publish on state changes."""
    if not GPIO_AVAILABLE:
        return
    while True:
        try:
            with gpio_lock:
                items = list(gpio_inputs.items())
            for pin, entry in items:
                cfg = entry["config"]
                try:
                    val = GPIO.input(pin)
                except Exception as e:
                    print(f"[GPIO] read pin {pin} error: {e}")
                    continue
                payload = cfg.get("payload_high", "high") if val else cfg.get("payload_low", "low")
                if entry["last_published"] != payload:
                    client.publish(
                        cfg.get("topic", ""),
                        payload,
                        qos=int(cfg.get("qos", 1)),
                        retain=bool(cfg.get("retain", True)),
                    )
                    entry["last_published"] = payload
                    print(f"[GPIO] {cfg.get('name','?')} pin {pin} -> {payload}")
        except Exception as e:
            print(f"[GPIO] poll error: {e}")
        time.sleep(GPIO_POLL_INTERVAL)


def gpio_handle_output_message(topic, payload):
    with gpio_lock:
        entry = gpio_outputs.get(topic)
    if not entry:
        return False
    cfg = entry["config"]
    payload_on = cfg.get("payload_on", "high")
    payload_off = cfg.get("payload_off", "low")
    active_high = bool(cfg.get("active_high", True))
    pin = entry["pin"]

    if payload == payload_on:
        target = GPIO.HIGH if active_high else GPIO.LOW
        label = "ON"
    elif payload == payload_off:
        target = GPIO.LOW if active_high else GPIO.HIGH
        label = "OFF"
    else:
        print(f"[GPIO] {cfg.get('name','?')} unknown payload '{payload}' on {topic}")
        return True

    if GPIO_AVAILABLE:
        try:
            GPIO.output(pin, target)
            print(f"[GPIO] {cfg.get('name','?')} pin {pin} -> {label}")
        except Exception as e:
            print(f"[GPIO] write pin {pin} error: {e}")
    return True


def cleanup_gpio():
    if GPIO_AVAILABLE and gpio_pins_setup:
        try:
            GPIO.cleanup()
            print("[GPIO] cleanup done")
        except Exception:
            pass


atexit.register(cleanup_gpio)


# ── Tuya worker ───────────────────────────────────────────────────────────────
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


# ── MQTT callbacks ────────────────────────────────────────────────────────────
def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="ignore").strip()

    # GPIO output topic?
    if gpio_handle_output_message(msg.topic, payload):
        return

    # Tuya switch set?
    parts = msg.topic.split('/')
    if len(parts) == 4 and parts[0] == 'tuya' and parts[3] == 'set':
        token = parts[1]
        switch = parts[2]
        command = payload.lower()
        dev_id = resolve_dev_id(token)
        print(f"[MQTT] {token} -> {dev_id}/{switch} -> {command}")
        if dev_id in device_queues:
            device_queues[dev_id].put((command, switch))
        else:
            print(f"[MQTT] Unknown device: {token}")


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("[MQTT] Connected — (re)subscribing")
        # Re-subscribe on every connect so broker restarts don't strand us
        client.subscribe("tuya/+/+/set")
        with gpio_lock:
            for topic in gpio_outputs:
                client.subscribe(topic)
    else:
        print(f"[MQTT] Connect failed: rc={rc}")


def on_disconnect(client, userdata, rc):
    print(f"[MQTT] Disconnected: rc={rc} — paho will auto-reconnect")


# ── Init ──────────────────────────────────────────────────────────────────────
if GPIO_AVAILABLE:
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

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

# GPIO comes up after MQTT client exists so output subs can attach
load_gpio_config(force=True)
threading.Thread(target=gpio_watch_thread, daemon=True, name="gpio-watch").start()
threading.Thread(target=gpio_input_poll_thread, daemon=True, name="gpio-poll").start()

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
