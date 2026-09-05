// ── State ─────────────────────────────────────────────────────────────────
let ws = null;
let wsReconnectTimer = null;
let devices = [];                  // [{name, id, ip, version, alias, state}]
let aliasDraft = {};               // dev_id -> alias (uncommitted edits)
let switchesShown = {};            // dev_id -> Set<switch>
let pduConfig = null;              // {thor, outlets} — staged locally, saved on demand
let pduOutletStatus = {};          // port -> "on"/"off"
let pduReachable = null;           // last known health of the PDU HTTP endpoint
let gpioConfigDraft = null;        // {inputs, outputs} — staged edits
let gpioInputState = {};           // name -> last payload (live)
let gpioOutputState = {};          // name -> "on"/"off" we last commanded

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
            gpioInputState = msg.gpio_input_state || {};
            gpioOutputState = msg.gpio_output_state || {};
            pduOutletStatus = msg.pdu_outlet_status || {};
            pduReachable = msg.pdu_reachable;
            renderDevices();
            renderGpio();
            renderPdu();
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
        case "gpio_input":
            gpioInputState[msg.name] = msg.value;
            renderGpio();
            break;
        case "gpio_command_ack":
            if (msg.ok) gpioOutputState[msg.name] = msg.cmd;
            renderGpio();
            break;
        case "pdu_outlet":
            if (msg.value) pduOutletStatus[msg.port] = msg.value;
            if (msg.reachable !== undefined) pduReachable = msg.reachable;
            renderPdu();
            break;
        case "pdu_command_ack":
            // outlet_status is already updated via the pdu_outlet broadcast
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

        // Pick switches from cloud mapping if available; fall back to seen DPS
        let switchList;
        if (dev.has_mapping) {
            switchList = (dev.switches || []).map(s => ({dp: s.dp, label: prettyCode(s.code)}));
        } else {
            switchList = Array.from(switchesShown[dev.id] || new Set(["1"]))
                .sort((a, b) => (parseInt(a) || 999) - (parseInt(b) || 999))
                .map(sw => ({dp: sw, label: `SW ${sw}`}));
        }

        const switchesHtml = switchList.map(({dp, label}) => {
            const isOn = (dev.state || {})[dp] === "on";
            return `
                <div class="switch-card ${isOn ? "on" : ""}" data-dev="${dev.id}" data-switch="${dp}">
                    <div class="switch-top">
                        <span class="switch-label">${escapeHtml(label)}</span>
                        <span class="switch-indicator"></span>
                    </div>
                    <div class="switch-buttons">
                        <button class="btn-switch btn-on" onclick="sendCommand('${dev.id}', '${dp}', true)">ON</button>
                        <button class="btn-switch btn-off" onclick="sendCommand('${dev.id}', '${dp}', false)">OFF</button>
                    </div>
                </div>
            `;
        }).join("");

        const addBtnHtml = dev.has_mapping ? "" :
            `<button class="add-switch-btn" onclick="addSwitch('${dev.id}')">+ switch</button>`;

        const infoDps = dev.info_dps || [];
        const infoHtml = infoDps.length ? `
            <details class="info-dps-block">
                <summary>Other DPS <span class="info-dps-count">${infoDps.length}</span></summary>
                <div class="info-dps-grid">
                    ${infoDps.map(i => `
                        <div class="info-dp">
                            <div class="info-dp-top">
                                <span class="info-dp-dp">DP ${i.dp}</span>
                                <span class="info-dp-type">${escapeHtml(i.type)}</span>
                            </div>
                            <div class="info-dp-code">${escapeHtml(i.code)}</div>
                        </div>
                    `).join("")}
                </div>
            </details>
        ` : "";

        row.innerHTML = `
            <div class="device-body">
                <div class="device-top">
                    <span class="device-name">${escapeHtml(dev.name || dev.id)}</span>
                    ${dev.version ? `<span class="device-version">v${escapeHtml(dev.version)}</span>` : ""}
                    ${dev.ip ? `<span class="device-ip">${escapeHtml(dev.ip)}</span>` : `<span class="device-noip">NO IP — RUN DISCOVERY</span>`}
                </div>
                <div class="device-id-row">
                    <span class="device-id-label">ID</span>
                    <span class="device-id-full">${escapeHtml(dev.id)}</span>
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
                    ${switchesHtml || '<div class="empty-state" style="padding:8px 12px">No on/off switches on this device</div>'}
                    ${addBtnHtml}
                </div>
                ${infoHtml}
            </div>
        `;
        root.appendChild(row);
    }
}

function prettyCode(code) {
    // switch_1 → SWITCH 1, switch_main → SWITCH MAIN, etc.
    if (!code) return "SW";
    return code.toUpperCase().replace(/_/g, " ");
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

// ── GPIO ──────────────────────────────────────────────────────────────────
async function loadGpioConfig() {
    try {
        const r = await fetch("/api/gpio-config");
        const j = await r.json();
        gpioConfigDraft = {
            inputs: (j.inputs || []).map(o => ({...o})),
            outputs: (j.outputs || []).map(o => ({...o})),
        };
        gpioInputState = j.input_state || gpioInputState;
        gpioOutputState = j.output_state || gpioOutputState;
        renderGpio();
    } catch (e) {
        console.error("loadGpioConfig:", e);
    }
}

function ensureGpioDraft() {
    if (!gpioConfigDraft) gpioConfigDraft = {inputs: [], outputs: []};
    return gpioConfigDraft;
}

function addGpioInput() {
    const d = ensureGpioDraft();
    d.inputs.push({
        name: "", pin: 0, pull: "up", topic: "",
        payload_high: "high", payload_low: "low", qos: 1, retain: true,
    });
    renderGpio();
}

function addGpioOutput() {
    const d = ensureGpioDraft();
    d.outputs.push({
        name: "", pin: 0, topic: "",
        payload_on: "high", payload_off: "low",
        active_high: true, initial: "low",
    });
    renderGpio();
}

function deleteGpioInput(i) {
    gpioConfigDraft.inputs.splice(i, 1);
    renderGpio();
}

function deleteGpioOutput(i) {
    gpioConfigDraft.outputs.splice(i, 1);
    renderGpio();
}

function onGpioFieldChange(kind, idx, field, value) {
    const arr = kind === "input" ? gpioConfigDraft.inputs : gpioConfigDraft.outputs;
    let v = value;
    if (field === "pin" || field === "qos") v = parseInt(value, 10) || 0;
    if (field === "retain" || field === "active_high") v = !!value;
    arr[idx][field] = v;
}

function renderGpio() {
    const inRoot = document.getElementById("gpio-inputs-list");
    const outRoot = document.getElementById("gpio-outputs-list");
    if (!inRoot || !outRoot) return;
    const d = ensureGpioDraft();

    inRoot.innerHTML = d.inputs.length ? "" : '<div class="empty-state">No inputs configured</div>';
    d.inputs.forEach((inp, i) => {
        const liveVal = gpioInputState[inp.name];
        const liveOn = liveVal && liveVal === inp.payload_high;
        const card = document.createElement("div");
        card.className = "gpio-card";
        card.innerHTML = `
            <div class="gpio-card-top">
                <div class="gpio-card-state ${liveVal ? (liveOn ? "on" : "off-state") : ""}">
                    <span class="gpio-indicator"></span>
                    <span class="gpio-live-value">${liveVal ? escapeHtml(liveVal) : "—"}</span>
                </div>
                <button class="btn-danger btn-sm" onclick="deleteGpioInput(${i})">Delete</button>
            </div>
            <div class="form-grid gpio-grid">
                <div class="form-group"><label>Name</label>
                    <input type="text" value="${escapeAttr(inp.name)}" placeholder="alarm"
                        oninput="onGpioFieldChange('input', ${i}, 'name', this.value)">
                </div>
                <div class="form-group"><label>Pin (BCM)</label>
                    <input type="number" value="${inp.pin}" min="0" max="40"
                        oninput="onGpioFieldChange('input', ${i}, 'pin', this.value)">
                </div>
                <div class="form-group"><label>Pull</label>
                    <select onchange="onGpioFieldChange('input', ${i}, 'pull', this.value)">
                        <option value="up" ${inp.pull==="up"?"selected":""}>Up</option>
                        <option value="down" ${inp.pull==="down"?"selected":""}>Down</option>
                        <option value="none" ${inp.pull==="none"?"selected":""}>None</option>
                    </select>
                </div>
                <div class="form-group form-group-wide"><label>MQTT Topic</label>
                    <input type="text" value="${escapeAttr(inp.topic)}" placeholder="alarm/state"
                        oninput="onGpioFieldChange('input', ${i}, 'topic', this.value)">
                </div>
                <div class="form-group"><label>Payload when HIGH</label>
                    <input type="text" value="${escapeAttr(inp.payload_high)}" placeholder="high"
                        oninput="onGpioFieldChange('input', ${i}, 'payload_high', this.value)">
                </div>
                <div class="form-group"><label>Payload when LOW</label>
                    <input type="text" value="${escapeAttr(inp.payload_low)}" placeholder="low"
                        oninput="onGpioFieldChange('input', ${i}, 'payload_low', this.value)">
                </div>
                <div class="form-group"><label>QoS</label>
                    <select onchange="onGpioFieldChange('input', ${i}, 'qos', this.value)">
                        <option value="0" ${inp.qos===0?"selected":""}>0</option>
                        <option value="1" ${inp.qos===1?"selected":""}>1</option>
                        <option value="2" ${inp.qos===2?"selected":""}>2</option>
                    </select>
                </div>
                <div class="form-group"><label>Retain</label>
                    <select onchange="onGpioFieldChange('input', ${i}, 'retain', this.value === 'true')">
                        <option value="true" ${inp.retain?"selected":""}>Yes</option>
                        <option value="false" ${!inp.retain?"selected":""}>No</option>
                    </select>
                </div>
            </div>
        `;
        inRoot.appendChild(card);
    });

    outRoot.innerHTML = d.outputs.length ? "" : '<div class="empty-state">No outputs configured</div>';
    d.outputs.forEach((out, i) => {
        const cmd = gpioOutputState[out.name];
        const card = document.createElement("div");
        card.className = "gpio-card";
        card.innerHTML = `
            <div class="gpio-card-top">
                <div class="gpio-card-state ${cmd === "on" ? "on" : (cmd === "off" ? "off-state" : "")}">
                    <span class="gpio-indicator"></span>
                    <span class="gpio-live-value">${cmd ? cmd.toUpperCase() : "—"}</span>
                </div>
                <div class="gpio-test-buttons">
                    <button class="btn-switch btn-on" onclick="sendGpioCommand('${escapeAttr(out.name)}', 'on')">${escapeHtml(out.payload_on || "HIGH").toUpperCase()}</button>
                    <button class="btn-switch btn-off" onclick="sendGpioCommand('${escapeAttr(out.name)}', 'off')">${escapeHtml(out.payload_off || "LOW").toUpperCase()}</button>
                </div>
                <button class="btn-danger btn-sm" onclick="deleteGpioOutput(${i})">Delete</button>
            </div>
            <div class="form-grid gpio-grid">
                <div class="form-group"><label>Name</label>
                    <input type="text" value="${escapeAttr(out.name)}" placeholder="relay_1"
                        oninput="onGpioFieldChange('output', ${i}, 'name', this.value)">
                </div>
                <div class="form-group"><label>Pin (BCM)</label>
                    <input type="number" value="${out.pin}" min="0" max="40"
                        oninput="onGpioFieldChange('output', ${i}, 'pin', this.value)">
                </div>
                <div class="form-group"><label>Active</label>
                    <select onchange="onGpioFieldChange('output', ${i}, 'active_high', this.value === 'true')">
                        <option value="true" ${out.active_high?"selected":""}>Active HIGH</option>
                        <option value="false" ${!out.active_high?"selected":""}>Active LOW (relays etc.)</option>
                    </select>
                </div>
                <div class="form-group form-group-wide"><label>MQTT Topic</label>
                    <input type="text" value="${escapeAttr(out.topic)}" placeholder="gpio/relay_1/set"
                        oninput="onGpioFieldChange('output', ${i}, 'topic', this.value)">
                </div>
                <div class="form-group"><label>Payload to turn ON</label>
                    <input type="text" value="${escapeAttr(out.payload_on)}" placeholder="high"
                        oninput="onGpioFieldChange('output', ${i}, 'payload_on', this.value)">
                </div>
                <div class="form-group"><label>Payload to turn OFF</label>
                    <input type="text" value="${escapeAttr(out.payload_off)}" placeholder="low"
                        oninput="onGpioFieldChange('output', ${i}, 'payload_off', this.value)">
                </div>
                <div class="form-group"><label>Initial state at boot</label>
                    <select onchange="onGpioFieldChange('output', ${i}, 'initial', this.value)">
                        <option value="low" ${out.initial==="low"?"selected":""}>LOW</option>
                        <option value="high" ${out.initial==="high"?"selected":""}>HIGH</option>
                    </select>
                </div>
            </div>
        `;
        outRoot.appendChild(card);
    });
}

function sendGpioCommand(name, cmd) {
    wsSend({type: "gpio_command", name, cmd});
}

async function saveGpioConfig() {
    const status = document.getElementById("gpio-save-status");
    status.classList.remove("error");
    status.textContent = "Saving…";
    try {
        const r = await fetch("/api/gpio-config", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(gpioConfigDraft || {inputs: [], outputs: []}),
        });
        if (r.ok) {
            status.textContent = "Saved";
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

// ── PDU (THOR RF11) ───────────────────────────────────────────────────────
async function loadPduConfig() {
    try {
        const r = await fetch("/api/pdu-config");
        const j = await r.json();
        pduConfig = {
            thor: {
                base_url: j.thor?.base_url || "",
                username: j.thor?.username || "",
                password: "",
                password_set: !!j.thor?.password_set,
            },
            outlets: (j.outlets || []).map(o => ({...o})),
        };
        pduOutletStatus = j.outlet_status || pduOutletStatus;
        pduReachable = j.reachable != null ? j.reachable : pduReachable;
        // Populate the settings form
        document.getElementById("pdu-base-url").value = pduConfig.thor.base_url;
        document.getElementById("pdu-user").value = pduConfig.thor.username;
        document.getElementById("pdu-pass").value = "";
        document.getElementById("pdu-pass-hint").textContent =
            pduConfig.thor.password_set ? "Password is set — leave blank to keep" : "Password not yet set";
        renderPdu();
    } catch (e) { console.error("loadPduConfig:", e); }
}

function _pduAliasOptions(selected) {
    // Aliases come from the devices already loaded on the Devices tab. If the
    // user hasn't visited that tab yet, we still have them from the initial
    // /api/devices call in loadDevices.
    const aliases = devices.map(d => d.alias).filter(Boolean).sort();
    const opts = ['<option value="">— none —</option>'];
    for (const a of aliases) {
        const sel = a === selected ? " selected" : "";
        opts.push(`<option value="${escapeAttr(a)}"${sel}>${escapeHtml(a)}</option>`);
    }
    // If the config points at something not in the alias list, keep it visible
    if (selected && !aliases.includes(selected)) {
        opts.push(`<option value="${escapeAttr(selected)}" selected>${escapeHtml(selected)} (missing)</option>`);
    }
    return opts.join("");
}

function renderPdu() {
    const tbody = document.getElementById("pdu-tbody");
    if (!tbody) return;
    if (!pduConfig) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-state">Loading…</td></tr>';
        return;
    }
    if (!pduConfig.outlets.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No outlets configured</td></tr>';
        return;
    }
    tbody.innerHTML = pduConfig.outlets.map((o, i) => {
        const state = pduOutletStatus[o.port];
        const stateCls = state === "on" ? "pdu-on" : (state === "off" ? "pdu-off" : "");
        return `
            <tr>
                <td><span class="mono">${escapeHtml(o.port)}</span></td>
                <td><input type="text" class="cam-label-input" value="${escapeAttr(o.name || "")}"
                    placeholder="e.g. HDMI Extender 1"
                    oninput="onPduFieldChange(${i}, 'name', this.value)"></td>
                <td>
                    <select class="pdu-alias-select" onchange="onPduFieldChange(${i}, 'tuya_alias', this.value)">
                        ${_pduAliasOptions(o.tuya_alias)}
                    </select>
                </td>
                <td><input type="number" class="pdu-num-input" min="1" max="99"
                    value="${o.tuya_switch || 1}"
                    oninput="onPduFieldChange(${i}, 'tuya_switch', this.value)"></td>
                <td><input type="number" class="pdu-num-input" min="0" max="300"
                    value="${o.on_delay_seconds || 0}"
                    oninput="onPduFieldChange(${i}, 'on_delay_seconds', this.value)"></td>
                <td><span class="pdu-state-chip ${stateCls}">${state ? state.toUpperCase() : "—"}</span></td>
                <td>
                    <div class="pdu-test-buttons">
                        <button class="btn-switch btn-on"  onclick="sendPduCommand('${escapeAttr(o.port)}', true)">ON</button>
                        <button class="btn-switch btn-off" onclick="sendPduCommand('${escapeAttr(o.port)}', false)">OFF</button>
                    </div>
                </td>
            </tr>
        `;
    }).join("");
}

function onPduFieldChange(i, field, value) {
    if (!pduConfig) return;
    const o = pduConfig.outlets[i];
    if (!o) return;
    if (field === "on_delay_seconds") o[field] = Math.max(0, parseInt(value, 10) || 0);
    else if (field === "tuya_switch") o[field] = String(value || "1");
    else o[field] = value;
}

function sendPduCommand(port, on) {
    wsSend({type: "pdu_command", port, on});
}

async function savePduConfig() {
    if (!pduConfig) return;
    const body = {
        thor: {
            base_url: document.getElementById("pdu-base-url").value.trim(),
            username: document.getElementById("pdu-user").value,
            password: document.getElementById("pdu-pass").value,
        },
        outlets: pduConfig.outlets.map(o => ({
            port: o.port,
            name: o.name || "",
            tuya_alias: o.tuya_alias || "",
            tuya_switch: o.tuya_switch || "1",
            on_delay_seconds: parseInt(o.on_delay_seconds, 10) || 0,
        })),
    };
    await postJson("/api/pdu-config", body, "pdu-save-status");
    // Reload so the password_set indicator refreshes
    loadPduConfig();
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
            if (tab === "gpio") loadGpioConfig();
            if (tab === "pdu") loadPduConfig();
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
