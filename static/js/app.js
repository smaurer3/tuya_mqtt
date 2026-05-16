// ── State ─────────────────────────────────────────────────────────────────
let ws = null;
let wsReconnectTimer = null;
let devices = [];                  // [{name, id, ip, version, alias, state}]
let aliasDraft = {};               // dev_id -> alias (uncommitted edits)
let switchesShown = {};            // dev_id -> Set<switch>

// ── WebSocket ─────────────────────────────────────────────────────────────
function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onopen = () => {
        console.log("WS connected");
        clearTimeout(wsReconnectTimer);
    };
    ws.onmessage = (e) => {
        try {
            const msg = JSON.parse(e.data);
            handleWs(msg);
        } catch (err) {
            console.error("Bad WS message:", e.data);
        }
    };
    ws.onclose = () => {
        console.warn("WS closed, retrying in 1s");
        setMqttDot(false);
        wsReconnectTimer = setTimeout(connectWs, 1000);
    };
    ws.onerror = (e) => { console.error("WS error:", e); };
}

function wsSend(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
        return true;
    }
    return false;
}

function handleWs(msg) {
    switch (msg.type) {
        case "init":
            setMqttDot(msg.mqtt_connected);
            setServiceDot(msg.service);
            // Seed state cache
            for (const [devId, switches] of Object.entries(msg.state_cache || {})) {
                for (const [sw, val] of Object.entries(switches)) {
                    updateSwitchState(devId, sw, val, /*render*/false);
                }
            }
            renderDevices();
            break;
        case "mqtt_status":
            setMqttDot(msg.connected);
            break;
        case "state":
            updateSwitchState(msg.dev_id, msg.switch, msg.value, /*render*/true);
            break;
        case "command_ack":
            // optional UX: subtle flash on the card
            break;
        case "discovery":
            appendDiscoveryLog(msg);
            if (msg.stage === "done" || msg.stage === "error") {
                document.getElementById("discover-btn").disabled = false;
            }
            break;
        case "devices_changed":
            loadDevices();
            break;
    }
}

// ── Status dots ───────────────────────────────────────────────────────────
function setMqttDot(connected) {
    const d = document.getElementById("mqtt-dot");
    d.classList.toggle("connected", !!connected);
    d.classList.toggle("disconnected", !connected);
}
function setServiceDot(status) {
    const d = document.getElementById("svc-dot");
    const lbl = document.getElementById("svc-label");
    d.classList.remove("active", "inactive", "warn");
    if (status === "active") { d.classList.add("active"); lbl.textContent = "SERVICE"; }
    else if (status === "inactive" || status === "failed") { d.classList.add("inactive"); lbl.textContent = status.toUpperCase(); }
    else { d.classList.add("warn"); lbl.textContent = (status || "UNKNOWN").toUpperCase(); }
}

// ── Devices ───────────────────────────────────────────────────────────────
async function loadDevices() {
    try {
        const r = await fetch("/api/devices");
        const j = await r.json();
        devices = j.devices || [];
        setServiceDot(j.service);
        // Initial switch lists from state, plus switch 1 by default
        for (const dev of devices) {
            const set = switchesShown[dev.id] || new Set();
            set.add("1");
            for (const sw of Object.keys(dev.state || {})) set.add(sw);
            switchesShown[dev.id] = set;
        }
        renderDevices();
    } catch (e) {
        console.error("loadDevices:", e);
    }
}

function refreshDevices() { loadDevices(); }

function updateSwitchState(devId, sw, value, doRender) {
    const dev = devices.find(d => d.id === devId);
    if (!dev) return;
    dev.state = dev.state || {};
    dev.state[sw] = value;
    if (!switchesShown[devId]) switchesShown[devId] = new Set(["1"]);
    switchesShown[devId].add(sw);
    if (doRender) renderDevices();
}

function renderDevices() {
    const root = document.getElementById("devices-list");
    if (!devices.length) {
        root.innerHTML = '<div class="empty-state">No devices in <span class="mono">devices.json</span> — run discovery</div>';
        return;
    }
    root.innerHTML = "";
    for (const dev of devices) {
        const draft = (aliasDraft[dev.id] !== undefined) ? aliasDraft[dev.id] : dev.alias;
        const dirty = draft !== (dev.alias || "");
        const offline = !dev.ip;

        const row = document.createElement("div");
        row.className = "device-row" + (offline ? " offline" : "");

        const switchList = Array.from(switchesShown[dev.id] || new Set(["1"])).sort();
        const switchesHtml = switchList.map(sw => {
            const isOn = (dev.state || {})[sw] === "on";
            return `
                <div class="switch-card ${isOn ? "on" : ""}" data-dev="${dev.id}" data-switch="${sw}">
                    <div class="switch-top">
                        <span class="switch-label">SW ${sw}</span>
                        <span class="switch-indicator"></span>
                    </div>
                    <div class="switch-buttons">
                        <button class="btn-switch btn-on" onclick="sendCommand('${dev.id}', '${sw}', true)">ON</button>
                        <button class="btn-switch btn-off" onclick="sendCommand('${dev.id}', '${sw}', false)">OFF</button>
                    </div>
                </div>
            `;
        }).join("");

        row.innerHTML = `
            <div class="device-body">
                <div class="device-top">
                    <span class="device-name">${escapeHtml(dev.name || dev.id)}</span>
                    <span class="device-id-chip" title="${dev.id}">${dev.id.slice(0, 10)}…</span>
                    ${dev.version ? `<span class="device-version">v${escapeHtml(dev.version)}</span>` : ""}
                    ${dev.ip ? `<span class="device-ip">${escapeHtml(dev.ip)}</span>` : `<span class="device-noip">NO IP — RUN DISCOVERY</span>`}
                </div>
                <div class="alias-row">
                    <label>Alias</label>
                    <input class="alias-input ${dirty ? "alias-dirty" : ""}"
                           type="text"
                           placeholder="e.g. stage_lights"
                           value="${escapeAttr(draft || "")}"
                           data-dev="${dev.id}"
                           oninput="onAliasInput(this)">
                </div>
                <div class="switches-row">
                    ${switchesHtml}
                    <button class="add-switch-btn" onclick="addSwitch('${dev.id}')">+ switch</button>
                </div>
            </div>
        `;
        root.appendChild(row);
    }
}

function onAliasInput(input) {
    const dev = input.dataset.dev;
    const v = input.value.trim();
    const orig = (devices.find(d => d.id === dev) || {}).alias || "";
    if (v === orig) delete aliasDraft[dev]; else aliasDraft[dev] = v;
    input.classList.toggle("alias-dirty", v !== orig);
}

function addSwitch(devId) {
    const set = switchesShown[devId] || new Set();
    let next = 1;
    while (set.has(String(next))) next++;
    set.add(String(next));
    switchesShown[devId] = set;
    renderDevices();
}

async function saveAllAliases() {
    // Build the full alias map: alias -> dev_id
    const out = {};
    for (const dev of devices) {
        const v = (aliasDraft[dev.id] !== undefined) ? aliasDraft[dev.id] : dev.alias;
        if (v) {
            if (out[v]) {
                alert(`Alias "${v}" used by multiple devices — aliases must be unique`);
                return;
            }
            out[v] = dev.id;
        }
    }
    try {
        const r = await fetch("/api/aliases", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({aliases: out})
        });
        if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            alert("Save failed: " + (err.detail || r.statusText));
            return;
        }
        aliasDraft = {};
        await loadDevices();
    } catch (e) { alert("Save failed: " + e); }
}

function sendCommand(devId, sw, on) {
    // Prefer alias if set; tuya_mqtt resolves either way
    const dev = devices.find(d => d.id === devId);
    const token = (dev && dev.alias) || devId;
    wsSend({type: "command", dev: token, switch: sw, on});
}

// ── Discovery ─────────────────────────────────────────────────────────────
async function startDiscovery() {
    const btn = document.getElementById("discover-btn");
    btn.disabled = true;
    appendDiscoveryLog({stage: "starting", message: "Triggering discovery…"});
    try {
        const r = await fetch("/api/discover", {method: "POST"});
        if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            appendDiscoveryLog({stage: "error", message: err.detail || r.statusText});
            btn.disabled = false;
        }
    } catch (e) {
        appendDiscoveryLog({stage: "error", message: String(e)});
        btn.disabled = false;
    }
}

function appendDiscoveryLog(msg) {
    const root = document.getElementById("discovery-log");
    if (root.querySelector(".empty-state")) root.innerHTML = "";
    const cls = msg.stage === "error" ? "error" : (msg.stage === "done" ? "success" : "info");
    const time = new Date().toLocaleTimeString();
    const line = document.createElement("div");
    line.className = "log-line " + cls;
    line.innerHTML = `<span class="log-time">${time}</span><span class="log-text">[${msg.stage}] ${escapeHtml(msg.message || "")}</span>`;
    root.appendChild(line);
    root.scrollTop = root.scrollHeight;
}

async function restartService() {
    const status = document.getElementById("restart-status");
    status.classList.remove("error");
    status.textContent = "Restarting…";
    try {
        const r = await fetch("/api/restart-service", {method: "POST"});
        const j = await r.json();
        if (j.ok) {
            status.textContent = "Service restarted (" + (j.status || "active") + ")";
        } else {
            status.classList.add("error");
            status.textContent = "Restart failed: " + (j.message || "unknown");
        }
        setServiceDot(j.status);
        setTimeout(() => { status.textContent = ""; status.classList.remove("error"); }, 6000);
    } catch (e) {
        status.classList.add("error");
        status.textContent = "Restart failed: " + e;
    }
}

// ── Settings ──────────────────────────────────────────────────────────────
async function loadSettings() {
    try {
        const [mq, ad, tt] = await Promise.all([
            fetch("/api/mqtt-config").then(r => r.json()),
            fetch("/api/admin-config").then(r => r.json()),
            fetch("/api/tuya-credentials").then(r => r.json()),
        ]);
        document.getElementById("mqtt-broker").value = mq.broker || "";
        document.getElementById("mqtt-port").value = mq.port || 1883;
        document.getElementById("mqtt-user").value = mq.username || "";
        document.getElementById("mqtt-pass").value = "";

        document.getElementById("adm-unit").value = ad.systemd_unit || "";
        document.getElementById("adm-tinytuya").value = ad.tinytuya_json_path || "";
        document.getElementById("adm-port").value = ad.admin_port || 8088;
        document.getElementById("tinytuya-path-display").textContent = ad.tinytuya_json_path || "tinytuya.json";

        document.getElementById("tuya-key").value = tt.apiKey || "";
        document.getElementById("tuya-region").value = tt.apiRegion || "us";
        document.getElementById("tuya-devid").value = tt.apiDeviceID || "";
        document.getElementById("tuya-secret").value = "";
        document.getElementById("tuya-secret-hint").textContent =
            tt.apiSecretSet ? "Secret is set — leave blank to keep, type a new value to replace" : "Secret not yet set";
    } catch (e) { console.error("loadSettings:", e); }
}

async function saveMqttConfig() {
    const body = {
        broker: document.getElementById("mqtt-broker").value.trim(),
        port: parseInt(document.getElementById("mqtt-port").value || "1883", 10),
        username: document.getElementById("mqtt-user").value,
        password: document.getElementById("mqtt-pass").value,
    };
    await postSettings("/api/mqtt-config", body, "mqtt-save-status", "Saved — restart tuya_mqtt service to apply");
}

async function saveTuyaCreds() {
    const body = {
        apiKey: document.getElementById("tuya-key").value.trim(),
        apiSecret: document.getElementById("tuya-secret").value, // may be blank → keep existing
        apiRegion: document.getElementById("tuya-region").value,
        apiDeviceID: document.getElementById("tuya-devid").value.trim(),
    };
    await postSettings("/api/tuya-credentials", body, "tuya-save-status", "Saved");
    loadSettings();
}

async function saveAdminConfig() {
    const body = {
        systemd_unit: document.getElementById("adm-unit").value.trim(),
        tinytuya_json_path: document.getElementById("adm-tinytuya").value.trim(),
        admin_port: parseInt(document.getElementById("adm-port").value || "8088", 10),
    };
    await postSettings("/api/admin-config", body, "adm-save-status", "Saved");
}

async function postSettings(url, body, statusEl, okMsg) {
    const status = document.getElementById(statusEl);
    status.classList.remove("error");
    status.textContent = "Saving…";
    try {
        const r = await fetch(url, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body),
        });
        if (r.ok) {
            status.textContent = okMsg;
        } else {
            const err = await r.json().catch(() => ({}));
            status.classList.add("error");
            status.textContent = "Failed: " + (err.detail || r.statusText);
        }
    } catch (e) {
        status.classList.add("error");
        status.textContent = "Failed: " + e;
    }
    setTimeout(() => { status.textContent = ""; status.classList.remove("error"); }, 4000);
}

// ── Tabs ──────────────────────────────────────────────────────────────────
function setupTabs() {
    document.querySelectorAll(".nav-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const tab = btn.dataset.tab;
            document.querySelectorAll(".nav-btn").forEach(b => b.classList.toggle("active", b === btn));
            document.querySelectorAll(".tab-content").forEach(c =>
                c.classList.toggle("active", c.id === "tab-" + tab));
            if (tab === "settings") loadSettings();
            if (tab === "devices") loadDevices();
        });
    });
}

// ── Utils ─────────────────────────────────────────────────────────────────
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
}
function escapeAttr(s) { return escapeHtml(s); }

// ── Bootstrap ─────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
    setupTabs();
    connectWs();
    loadDevices();
});
