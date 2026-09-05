#!/usr/bin/python3
"""
Tuya Admin — FastAPI app for onboarding Tuya devices, managing aliases,
and testing on/off commands.

Run alongside tuya_mqtt.py in the same working directory.
"""
import asyncio
import json
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import paho.mqtt.client as mqtt
import requests
import xml.etree.ElementTree as ET

# ── Paths ─────────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
DEVICES_FILE = os.path.join(HERE, "devices.json")
ALIASES_FILE = os.path.join(HERE, "aliases.json")
MQTT_CONFIG_FILE = os.path.join(HERE, "mqtt_config.json")
ADMIN_CONFIG_FILE = os.path.join(HERE, "admin_config.json")
SNAPSHOT_FILE = os.path.join(HERE, "snapshot.json")
GPIO_CONFIG_FILE = os.path.join(HERE, "gpio_config.json")
PDU_CONFIG_FILE = os.path.join(HERE, "pdu_config.json")
STATIC_DIR = os.path.join(HERE, "static")


# ── Config helpers ────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_devices():
    return load_json(DEVICES_FILE, [])


def load_aliases():
    return load_json(ALIASES_FILE, {})


def load_mqtt_config():
    return load_json(MQTT_CONFIG_FILE, {
        "broker": "192.168.10.1", "port": 1883,
        "username": "", "password": ""
    })


def load_admin_config():
    return load_json(ADMIN_CONFIG_FILE, {
        "systemd_unit": "tuya_mqtt.service",
        "tinytuya_json_path": "/home/pi/tinytuya.json",
        "admin_port": 8088
    })


def load_gpio_config():
    return load_json(GPIO_CONFIG_FILE, {"inputs": [], "outputs": []})


def load_pdu_config():
    # Seeded from the old /var/controlHTML/power/pwr_ws.py so first-run behaviour
    # matches the retired service. User configures tuya_alias via the UI.
    return load_json(PDU_CONFIG_FILE, {
        "thor": {
            "base_url": "http://192.168.10.129",
            "username": "admin",
            "password": "THORRF11",
        },
        "outlets": [
            {"port": "p1", "name": "",                "tuya_alias": "", "tuya_switch": "1", "on_delay_seconds": 0},
            {"port": "p2", "name": "HDMI Extender 1", "tuya_alias": "", "tuya_switch": "1", "on_delay_seconds": 0},
            {"port": "p3", "name": "HDMI Extender 2", "tuya_alias": "", "tuya_switch": "1", "on_delay_seconds": 0},
            {"port": "p4", "name": "ATEM Mini",       "tuya_alias": "", "tuya_switch": "1", "on_delay_seconds": 30},
            {"port": "p5", "name": "Sony Camera 1",   "tuya_alias": "", "tuya_switch": "1", "on_delay_seconds": 0},
            {"port": "p6", "name": "Sony Camera 2",   "tuya_alias": "", "tuya_switch": "1", "on_delay_seconds": 0},
            {"port": "p7", "name": "Clearone USB",    "tuya_alias": "", "tuya_switch": "1", "on_delay_seconds": 0},
            {"port": "p8", "name": "Clearone 880",    "tuya_alias": "", "tuya_switch": "1", "on_delay_seconds": 0},
        ],
    })


def load_tinytuya_creds():
    cfg = load_admin_config()
    path = cfg["tinytuya_json_path"]
    return load_json(path, {
        "apiKey": "", "apiSecret": "", "apiRegion": "us", "apiDeviceID": ""
    })


def save_tinytuya_creds(creds):
    cfg = load_admin_config()
    path = cfg["tinytuya_json_path"]
    save_json(path, creds)


# ── WebSocket manager ─────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, data: dict):
        dead = set()
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        self.active -= dead


manager = ConnectionManager()
loop: Optional[asyncio.AbstractEventLoop] = None


def broadcast_async(data: dict):
    """Thread-safe broadcast from non-async code."""
    if loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(manager.broadcast(data), loop)


# ── MQTT ──────────────────────────────────────────────────────────────────────
mqtt_client: Optional[mqtt.Client] = None
mqtt_connected = False
# Cache state by dev_id (not alias) — UI keys on dev_id
state_cache: dict = {}  # dev_id -> {switch: "on"|"off"}
# GPIO state caches
gpio_input_topics: Set[str] = set()        # currently-subscribed input topics
gpio_input_state: dict = {}                # input name -> last payload
gpio_output_state: dict = {}               # output name -> "on"/"off" we last commanded
# PDU (THOR RF11) mirror state
pdu_last_state: dict = {}                  # dev_id -> {switch: "on"|"off"} — edge detection
pdu_last_state_lock = threading.Lock()
pdu_outlet_status: dict = {}               # port -> "on"/"off" — last known (from poll or command)
pdu_outlet_current: dict = {}              # port -> amps (float, from /status.xml)
pdu_reachable: Optional[bool] = None       # None=unknown, True/False from last HTTP


def _resolve_topic_to_dev_id(token: str) -> Optional[str]:
    """Topic segment can be either a dev_id or an alias — return dev_id."""
    devs = load_devices()
    known_ids = {d["id"] for d in devs}
    if token in known_ids:
        return token
    aliases = load_aliases()
    return aliases.get(token)


def on_mqtt_connect(client, userdata, flags, rc):
    global mqtt_connected
    mqtt_connected = (rc == 0)
    print(f"[ADMIN-MQTT] {'Connected' if mqtt_connected else f'Failed rc={rc}'}")
    if mqtt_connected:
        client.subscribe("tuya/+/+/state")
        # Resubscribe to all configured GPIO input topics
        global gpio_input_topics
        gpio_input_topics = _current_gpio_input_topics()
        for topic in gpio_input_topics:
            client.subscribe(topic)
    broadcast_async({"type": "mqtt_status", "connected": mqtt_connected})


def _current_gpio_input_topics():
    return {inp.get("topic", "") for inp in load_gpio_config().get("inputs", []) if inp.get("topic")}


def resubscribe_gpio_inputs():
    """Adjust admin's input-topic subs to match current gpio_config.json."""
    global gpio_input_topics
    if mqtt_client is None:
        return
    new_topics = _current_gpio_input_topics()
    for t in (gpio_input_topics - new_topics):
        try: mqtt_client.unsubscribe(t)
        except Exception: pass
    for t in (new_topics - gpio_input_topics):
        try: mqtt_client.subscribe(t)
        except Exception: pass
    gpio_input_topics = new_topics


def on_mqtt_disconnect(client, userdata, rc):
    global mqtt_connected
    mqtt_connected = False
    print(f"[ADMIN-MQTT] Disconnected rc={rc}")
    broadcast_async({"type": "mqtt_status", "connected": False})


def on_mqtt_message(client, userdata, msg):
    payload = msg.payload.decode(errors="ignore").strip()

    # GPIO input topic?
    for inp in load_gpio_config().get("inputs", []):
        if inp.get("topic") == msg.topic:
            name = inp.get("name") or msg.topic
            gpio_input_state[name] = payload
            broadcast_async({
                "type": "gpio_input", "name": name, "topic": msg.topic, "value": payload
            })
            return

    # Tuya state?
    parts = msg.topic.split("/")
    if len(parts) == 4 and parts[0] == "tuya" and parts[3] == "state":
        token, switch = parts[1], parts[2]
        dev_id = _resolve_topic_to_dev_id(token)
        if not dev_id:
            return
        value = payload.lower()
        state_cache.setdefault(dev_id, {})[switch] = value
        broadcast_async({
            "type": "state", "dev_id": dev_id, "switch": switch, "value": value
        })
        pdu_handle_state_change(dev_id, switch, value)


def start_mqtt():
    global mqtt_client
    if mqtt_client is not None:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass
    cfg = load_mqtt_config()
    c = mqtt.Client(client_id="tuya_admin")
    c.on_connect = on_mqtt_connect
    c.on_disconnect = on_mqtt_disconnect
    c.on_message = on_mqtt_message
    if cfg.get("username"):
        c.username_pw_set(cfg["username"], cfg.get("password", ""))
    c.reconnect_delay_set(min_delay=1, max_delay=60)
    c.connect_async(cfg["broker"], int(cfg["port"]), keepalive=60)
    c.loop_start()
    mqtt_client = c


def mqtt_publish_set(token: str, switch: str, on: bool):
    """Publish a set command — token can be a dev_id or alias. tuya_mqtt resolves it."""
    if mqtt_client is None:
        return False
    mqtt_client.publish(f"tuya/{token}/{switch}/set", "on" if on else "off")
    return True


# ── Discovery (tinytuya wizard equivalent) ────────────────────────────────────
discovery_lock = threading.Lock()
discovery_running = False


def _discovery_worker():
    """Run a Tuya scan + cloud-key fetch in a background thread.
    Streams progress over WS. Writes devices.json on success."""
    global discovery_running
    try:
        broadcast_async({"type": "discovery", "stage": "starting"})

        import tinytuya  # local import — only required for discovery

        # 1) Cloud fetch
        creds = load_tinytuya_creds()
        if not creds.get("apiKey") or not creds.get("apiSecret"):
            broadcast_async({"type": "discovery", "stage": "error",
                             "message": "Tuya cloud credentials missing — set them in Settings"})
            return

        broadcast_async({"type": "discovery", "stage": "cloud_fetch",
                         "message": "Fetching device list from Tuya Cloud…"})
        cloud = tinytuya.Cloud(
            apiRegion=creds.get("apiRegion", "us"),
            apiKey=creds["apiKey"],
            apiSecret=creds["apiSecret"],
            apiDeviceID=creds.get("apiDeviceID", "")
        )
        cloud_devs = cloud.getdevices()
        if isinstance(cloud_devs, dict) and "Error" in cloud_devs:
            # tinytuya returns {Error, Err, Payload}. Payload is usually the
            # raw human string from the Tuya cloud (sometimes a dict). We
            # surface everything so the user never has to drop to the wizard
            # to figure out what's wrong.
            short = cloud_devs.get("Error") or "Unknown error"
            err_code = cloud_devs.get("Err")
            payload = cloud_devs.get("Payload")
            if isinstance(payload, dict):
                payload_str = (f"Tuya code {payload.get('code')}: {payload.get('msg')}"
                               if payload.get("code") else json.dumps(payload))
            else:
                payload_str = str(payload) if payload else ""
            parts = [short]
            if payload_str:
                parts.append(payload_str)
            if err_code:
                parts.append(f"(Err={err_code})")
            # Hint for the case we've actually seen
            if payload_str and "28841002" in payload_str:
                parts.append("Your Tuya IoT Cloud Development plan has expired — renew at iot.tuya.com.")
            broadcast_async({"type": "discovery", "stage": "error",
                             "message": "Cloud error: " + " — ".join(parts)})
            return
        broadcast_async({"type": "discovery", "stage": "cloud_done",
                         "message": f"Got {len(cloud_devs)} device(s) from cloud"})

        # 2) Local scan
        broadcast_async({"type": "discovery", "stage": "scanning",
                         "message": "Scanning local network (this takes ~20s)…"})
        scan = tinytuya.deviceScan(False, 20)
        # scan returns {ip: {gwId, productKey, version, ...}}
        scan_by_id = {info.get("gwId"): info for info in scan.values() if info.get("gwId")}

        broadcast_async({"type": "discovery", "stage": "scan_done",
                         "message": f"Found {len(scan_by_id)} device(s) on network"})

        # 3) Merge
        merged = []
        for d in cloud_devs:
            dev_id = d.get("id")
            if not dev_id:
                continue
            local = scan_by_id.get(dev_id, {})
            merged.append({
                "name": d.get("name", dev_id),
                "id": dev_id,
                "key": d.get("key", ""),
                "mac": d.get("mac", ""),
                "ip": local.get("ip", ""),
                "ver": str(local.get("version", "3.3")),
                "version": str(local.get("version", "3.3")),
            })

        # 4) Write devices.json atomically
        save_json(DEVICES_FILE, merged)
        broadcast_async({
            "type": "discovery", "stage": "done",
            "message": f"Wrote {len(merged)} device(s) to devices.json",
            "found": len(merged),
            "on_network": sum(1 for m in merged if m["ip"]),
        })
        broadcast_async({"type": "devices_changed"})
    except Exception as e:
        broadcast_async({"type": "discovery", "stage": "error",
                         "message": f"Discovery failed: {e}"})
    finally:
        global discovery_running
        with discovery_lock:
            discovery_running = False


# ── Systemd helpers ───────────────────────────────────────────────────────────
def restart_tuya_mqtt():
    cfg = load_admin_config()
    unit = cfg.get("systemd_unit", "tuya_mqtt.service")
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", unit],
            capture_output=True, text=True, timeout=15
        )
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except subprocess.TimeoutExpired:
        return False, "systemctl restart timed out"
    except FileNotFoundError:
        return False, "systemctl not found"


def service_status():
    cfg = load_admin_config()
    unit = cfg.get("systemd_unit", "tuya_mqtt.service")
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


# ── FastAPI lifespan ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_running_loop()
    start_mqtt()
    threading.Thread(target=pdu_poll_thread, daemon=True, name="pdu-poll").start()
    yield
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


app = FastAPI(lifespan=lifespan)


# Cloudflare (which fronts this app via tunnel) caches /static/* by default at
# its edge. Force no-cache on every static response so Cloudflare treats the
# assets as uncacheable and users always get the latest UI code after a deploy.
@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ── Routes: static ────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Routes: API ───────────────────────────────────────────────────────────────
def _classify_dps(mapping: dict):
    """Split a device's DPS mapping into switches vs informational DPS.
    Switches = Boolean + code starts with "switch_" (excludes switch_inching etc.
    only if code doesn't start with switch — they do, so be stricter: type==Boolean).
    """
    switches = []
    info = []
    for dp, meta in mapping.items():
        code = str(meta.get("code", ""))
        typ = str(meta.get("type", ""))
        entry = {"dp": str(dp), "code": code, "type": typ}
        if typ == "Boolean" and code.startswith("switch_"):
            switches.append(entry)
        else:
            info.append(entry)
    switches.sort(key=lambda e: int(e["dp"]) if e["dp"].isdigit() else 9999)
    info.sort(key=lambda e: int(e["dp"]) if e["dp"].isdigit() else 9999)
    return switches, info


@app.get("/api/devices")
async def api_devices():
    devices = load_devices()
    aliases = load_aliases()
    reverse = {v: k for k, v in aliases.items()}
    out = []
    for d in devices:
        dev_id = d["id"]
        mapping = d.get("mapping", {}) or {}
        switches, info = _classify_dps(mapping)
        out.append({
            "name": d.get("name", dev_id),
            "id": dev_id,
            "ip": d.get("ip", ""),
            "version": d.get("version", ""),
            "alias": reverse.get(dev_id, ""),
            "state": state_cache.get(dev_id, {}),
            "switches": switches,           # [{dp, code, type}, ...] — render as on/off
            "info_dps": info,               # [{dp, code, type}, ...] — read-only metadata
            "has_mapping": bool(mapping),   # false → no cloud mapping, fall back to seen DPS
        })
    return {"devices": out, "service": service_status()}


class AliasesPayload(BaseModel):
    aliases: dict  # alias -> dev_id


@app.get("/api/aliases")
async def api_get_aliases():
    return {"aliases": load_aliases()}


@app.post("/api/aliases")
async def api_save_aliases(payload: AliasesPayload):
    cleaned = {}
    seen_ids = set()
    for alias, dev_id in payload.aliases.items():
        alias = alias.strip()
        dev_id = str(dev_id).strip()
        if not alias or not dev_id:
            continue
        if not alias.replace("_", "").replace("-", "").isalnum():
            raise HTTPException(400, f"Invalid alias '{alias}' — letters/numbers/_/- only")
        if dev_id in seen_ids:
            raise HTTPException(400, f"Device {dev_id} mapped to multiple aliases")
        seen_ids.add(dev_id)
        cleaned[alias] = dev_id
    save_json(ALIASES_FILE, cleaned)
    return {"ok": True, "aliases": cleaned}


class MqttConfigPayload(BaseModel):
    broker: str
    port: int = 1883
    username: str = ""
    password: str = ""


@app.get("/api/mqtt-config")
async def api_get_mqtt_config():
    return load_mqtt_config()


@app.post("/api/mqtt-config")
async def api_save_mqtt_config(payload: MqttConfigPayload):
    save_json(MQTT_CONFIG_FILE, payload.dict())
    # Reconnect admin's own MQTT client
    start_mqtt()
    return {"ok": True}


class AdminConfigPayload(BaseModel):
    systemd_unit: str
    tinytuya_json_path: str
    admin_port: int = 8088


@app.get("/api/admin-config")
async def api_get_admin_config():
    return load_admin_config()


@app.post("/api/admin-config")
async def api_save_admin_config(payload: AdminConfigPayload):
    save_json(ADMIN_CONFIG_FILE, payload.dict())
    return {"ok": True}


class TuyaCredsPayload(BaseModel):
    apiKey: str
    apiSecret: str
    apiRegion: str = "us"
    apiDeviceID: str = ""


@app.get("/api/tuya-credentials")
async def api_get_tuya_creds():
    creds = load_tinytuya_creds()
    # Redact the secret in the response (UI shows placeholder)
    out = dict(creds)
    if out.get("apiSecret"):
        out["apiSecretSet"] = True
        out["apiSecret"] = ""
    else:
        out["apiSecretSet"] = False
    return out


@app.post("/api/tuya-credentials")
async def api_save_tuya_creds(payload: TuyaCredsPayload):
    existing = load_tinytuya_creds()
    out = {
        "apiKey": payload.apiKey,
        "apiSecret": payload.apiSecret or existing.get("apiSecret", ""),
        "apiRegion": payload.apiRegion,
        "apiDeviceID": payload.apiDeviceID,
    }
    save_tinytuya_creds(out)
    return {"ok": True}


@app.post("/api/discover")
async def api_discover():
    global discovery_running
    with discovery_lock:
        if discovery_running:
            raise HTTPException(409, "Discovery already running")
        discovery_running = True
    threading.Thread(target=_discovery_worker, daemon=True, name="discovery").start()
    return {"ok": True}


@app.post("/api/restart-service")
async def api_restart_service():
    ok, msg = restart_tuya_mqtt()
    return {"ok": ok, "message": msg, "status": service_status()}


@app.get("/api/service-status")
async def api_service_status():
    return {"status": service_status()}


# ── GPIO API ──────────────────────────────────────────────────────────────────
class GpioInput(BaseModel):
    name: str
    pin: int
    pull: str = "up"
    topic: str
    payload_high: str = "high"
    payload_low: str = "low"
    qos: int = 1
    retain: bool = True


class GpioOutput(BaseModel):
    name: str
    pin: int
    topic: str
    payload_on: str = "high"
    payload_off: str = "low"
    active_high: bool = True
    initial: str = "low"


class GpioConfigPayload(BaseModel):
    inputs: list[GpioInput]
    outputs: list[GpioOutput]


@app.get("/api/gpio-config")
async def api_get_gpio_config():
    return {**load_gpio_config(),
            "input_state": gpio_input_state,
            "output_state": gpio_output_state}


@app.post("/api/gpio-config")
async def api_save_gpio_config(payload: GpioConfigPayload):
    cfg = payload.dict()

    # Validate uniqueness — duplicate names or pins are surprises waiting to happen
    names = [i["name"] for i in cfg["inputs"]] + [o["name"] for o in cfg["outputs"]]
    if len(names) != len(set(names)):
        raise HTTPException(400, "GPIO names must be unique across inputs and outputs")
    pins = [i["pin"] for i in cfg["inputs"]] + [o["pin"] for o in cfg["outputs"]]
    if len(pins) != len(set(pins)):
        raise HTTPException(400, "GPIO pin numbers must be unique across inputs and outputs")

    save_json(GPIO_CONFIG_FILE, cfg)
    # Update admin's own input subs immediately (tuya_mqtt will hot-reload its own
    # within ~2s via its file watcher)
    resubscribe_gpio_inputs()
    return {"ok": True}


# ── PDU (THOR RF11) ───────────────────────────────────────────────────────────
# Watches the tuya/*/*/state MQTT stream and mirrors selected switches onto a
# THOR RF11 PDU's outlets via its /cpan.cgi HTTP API. Per-outlet on-delay lets
# you stagger power-up (e.g. wait 30 s before the ATEM Mini boots). Replaces
# the standalone pwr_ws.py service.
def thor_set_outlet(port, value):
    """Set one PDU outlet on the THOR RF11 web interface. value is "1" or "0"."""
    global pdu_reachable
    cfg = load_pdu_config().get("thor", {})
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base or not port:
        return False
    url = f"{base}/cpan.cgi?param={port}&value={value}"
    try:
        r = requests.get(url, auth=(cfg.get("username", ""), cfg.get("password", "")),
                         timeout=5)
        ok = r.ok
        pdu_reachable = ok
        # Optimistic UI update — the next poll cycle will confirm
        pdu_outlet_status[port] = "on" if value == "1" else "off"
        broadcast_async({"type": "pdu_outlet", "port": port,
                         "value": pdu_outlet_status[port],
                         "current": pdu_outlet_current.get(port),
                         "ok": ok, "reachable": pdu_reachable})
        return ok
    except Exception as e:
        pdu_reachable = False
        broadcast_async({"type": "pdu_outlet", "port": port, "value": None,
                         "ok": False, "reachable": False, "error": str(e)})
        print(f"[PDU] {port}={value} error: {e}")
        return False


def pdu_poll_thread():
    """Poll /status.xml every 5 s so the UI shows the PDU's real state and
    per-outlet current draw, not just what we last commanded."""
    global pdu_reachable
    while True:
        cfg = load_pdu_config().get("thor", {})
        base = (cfg.get("base_url") or "").rstrip("/")
        if not base:
            time.sleep(5)
            continue
        try:
            r = requests.get(
                f"{base}/status.xml",
                auth=(cfg.get("username", ""), cfg.get("password", "")),
                timeout=4,
            )
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}")
            root = ET.fromstring(r.text)
            was_reachable = pdu_reachable
            pdu_reachable = True
            for i in range(1, 9):
                port = f"p{i}"
                state_el = root.find(port)
                curr_el = root.find(f"c{i}")
                new_state = None
                if state_el is not None and state_el.text is not None:
                    new_state = "on" if state_el.text.strip() == "1" else "off"
                new_current = None
                if curr_el is not None and curr_el.text is not None:
                    try:
                        new_current = float(curr_el.text.strip())
                    except ValueError:
                        pass
                changed = (
                    (new_state is not None and pdu_outlet_status.get(port) != new_state)
                    or (new_current is not None and pdu_outlet_current.get(port) != new_current)
                )
                if new_state is not None:
                    pdu_outlet_status[port] = new_state
                if new_current is not None:
                    pdu_outlet_current[port] = new_current
                if changed:
                    broadcast_async({
                        "type": "pdu_outlet",
                        "port": port,
                        "value": pdu_outlet_status.get(port),
                        "current": pdu_outlet_current.get(port),
                        "reachable": True,
                    })
            if not was_reachable:
                broadcast_async({"type": "pdu_reachable", "reachable": True})
        except Exception as e:
            if pdu_reachable is not False:
                print(f"[PDU] poll error: {e}")
                broadcast_async({"type": "pdu_reachable", "reachable": False,
                                 "error": str(e)})
            pdu_reachable = False
        time.sleep(5)


def _pdu_apply_with_delay(port, value_str, delay, name):
    if delay > 0 and value_str == "1":
        print(f"[PDU] {name} ({port}) → ON in {delay}s")
        time.sleep(delay)
    ok = thor_set_outlet(port, value_str)
    print(f"[PDU] {name} ({port}) → {value_str} {'OK' if ok else 'FAIL'}")


def _pdu_resolve_source(source):
    """Given an alias (or a raw dev_id) return the canonical dev_id."""
    if not source:
        return None
    return load_aliases().get(source, source)


def pdu_handle_state_change(dev_id, switch, value):
    """Called from on_mqtt_message. On a rising/falling edge for any outlet
    configured to mirror (dev_id, switch), fire the PDU command (with on-delay
    applied only on the rising edge)."""
    switch = str(switch)
    with pdu_last_state_lock:
        prev = pdu_last_state.setdefault(dev_id, {}).get(switch)
        pdu_last_state[dev_id][switch] = value
    if prev == value:
        return  # repeated same state — nothing to do

    for outlet in load_pdu_config().get("outlets", []):
        if _pdu_resolve_source(outlet.get("tuya_alias")) != dev_id:
            continue
        if str(outlet.get("tuya_switch", "1")) != switch:
            continue
        port = outlet.get("port")
        if not port:
            continue
        delay = int(outlet.get("on_delay_seconds") or 0)
        value_str = "1" if value == "on" else "0"
        threading.Thread(
            target=_pdu_apply_with_delay,
            args=(port, value_str, delay, outlet.get("name") or port),
            daemon=True,
        ).start()


# ── PDU API ───────────────────────────────────────────────────────────────────
class ThorSettings(BaseModel):
    base_url: str
    username: str
    password: str = ""


class PduOutlet(BaseModel):
    port: str
    name: str = ""
    tuya_alias: str = ""
    tuya_switch: str = "1"
    on_delay_seconds: int = 0


class PduConfigPayload(BaseModel):
    thor: ThorSettings
    outlets: list[PduOutlet]


@app.get("/api/pdu-config")
async def api_get_pdu_config():
    cfg = load_pdu_config()
    # Redact password on GET — UI leaves blank to keep existing
    thor = cfg.get("thor", {}) or {}
    if thor.get("password"):
        thor = dict(thor)
        thor["password"] = ""
        thor["password_set"] = True
    else:
        thor = dict(thor); thor["password_set"] = False
    return {
        "thor": thor,
        "outlets": cfg.get("outlets", []),
        "outlet_status": pdu_outlet_status,
        "outlet_current": pdu_outlet_current,
        "reachable": pdu_reachable,
    }


@app.post("/api/pdu-config")
async def api_save_pdu_config(payload: PduConfigPayload):
    d = payload.dict()
    # Preserve existing password if new is blank (matches other config endpoints)
    existing = load_pdu_config().get("thor", {}) or {}
    if not d["thor"].get("password") and existing.get("password"):
        d["thor"]["password"] = existing["password"]
    # Sanitise: on_delay non-negative
    for o in d["outlets"]:
        o["on_delay_seconds"] = max(0, int(o.get("on_delay_seconds") or 0))
        o["tuya_switch"] = str(o.get("tuya_switch") or "1")
    save_json(PDU_CONFIG_FILE, d)
    return {"ok": True}


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send initial snapshot
        await ws.send_json({
            "type": "init",
            "mqtt_connected": mqtt_connected,
            "service": service_status(),
            "state_cache": state_cache,
            "gpio_input_state": gpio_input_state,
            "gpio_output_state": gpio_output_state,
            "pdu_outlet_status": pdu_outlet_status,
            "pdu_outlet_current": pdu_outlet_current,
            "pdu_reachable": pdu_reachable,
        })
        while True:
            data = await ws.receive_json()
            await handle_ws(ws, data)
    except WebSocketDisconnect:
        manager.disconnect(ws)


async def handle_ws(ws: WebSocket, data: dict):
    t = data.get("type")
    if t == "command":
        token = data.get("dev")        # dev_id or alias
        switch = str(data.get("switch", "1"))
        on = bool(data.get("on"))
        ok = mqtt_publish_set(token, switch, on)
        await ws.send_json({
            "type": "command_ack", "dev": token, "switch": switch, "on": on, "ok": ok
        })
    elif t == "gpio_command":
        name = data.get("name")
        cmd = data.get("cmd")  # "on" or "off"
        ok = False
        for out in load_gpio_config().get("outputs", []):
            if out.get("name") == name:
                payload = out.get("payload_on" if cmd == "on" else "payload_off",
                                  "high" if cmd == "on" else "low")
                if mqtt_client is not None:
                    mqtt_client.publish(out["topic"], payload)
                    gpio_output_state[name] = cmd
                    ok = True
                break
        await ws.send_json({"type": "gpio_command_ack", "name": name, "cmd": cmd, "ok": ok})
    elif t == "pdu_command":
        port = data.get("port")
        on = bool(data.get("on"))
        ok = thor_set_outlet(port, "1" if on else "0")
        await ws.send_json({"type": "pdu_command_ack", "port": port, "on": on, "ok": ok})
    elif t == "ping":
        await ws.send_json({"type": "pong", "t": time.time()})


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    cfg = load_admin_config()
    uvicorn.run(app, host="0.0.0.0", port=int(cfg.get("admin_port", 8088)))
