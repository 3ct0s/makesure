// MakeSure viewer page: login, device list and the live (view-only) stream.

import { Backoff, ClockSync, CODEC_H264, CODEC_JPEG, codecFromAnnexB, parseFrame } from "./protocol.js";

const $ = (id) => document.getElementById(id);
const DEVICE_REFRESH_MS = 5000;
const PING_INTERVAL_MS = 2000;
const MAX_DECODE_QUEUE = 4;
const KEYFRAME_REQUEST_MIN_MS = 1000;

let deviceTimer = null;
let stream = null;

function show(id, visible) {
  $(id).hidden = !visible;
}

function localNowMs() {
  return performance.timeOrigin + performance.now();
}

// ---------- session and device list ----------

async function refreshDevices() {
  let response;
  try {
    response = await fetch("/api/devices", { cache: "no-store", credentials: "same-origin" });
  } catch {
    return;
  }
  if (response.status === 401) {
    showLogin();
    return;
  }
  if (!response.ok) return;
  const data = await response.json();
  showApp();
  renderDevices(data.devices || []);
}

function showLogin() {
  if (stream) stream.close();
  stream = null;
  clearInterval(deviceTimer);
  deviceTimer = null;
  show("app-view", false);
  show("logout", false);
  show("login-view", true);
  $("password").focus();
}

function showApp() {
  show("login-view", false);
  show("app-view", true);
  show("logout", true);
  if (deviceTimer === null) deviceTimer = setInterval(refreshDevices, DEVICE_REFRESH_MS);
}

function renderDevices(devices) {
  const body = $("devices").tBodies[0];
  body.replaceChildren();
  show("devices-empty", devices.length === 0);
  for (const device of devices) {
    const row = body.insertRow();
    const status = row.insertCell();
    const badge = document.createElement("span");
    badge.className = "badge " + (device.online ? (device.paused ? "paused" : "online") : "offline");
    badge.textContent = device.online ? (device.paused ? "paused" : "online") : "offline";
    status.append(badge);
    row.insertCell().textContent = device.name;
    const idCell = row.insertCell();
    idCell.textContent = device.device_id;
    idCell.className = "mono";
    row.insertCell().textContent = device.viewers ? String(device.viewers) : "";
    const action = row.insertCell();
    const button = document.createElement("button");
    const active = stream && stream.deviceId === device.device_id;
    button.textContent = active ? "Viewing" : "Connect";
    button.disabled = Boolean(active);
    button.addEventListener("click", () => openStream(device.device_id, device.name));
    action.append(button);
  }
}

async function login(event) {
  event.preventDefault();
  $("login-error").textContent = "";
  const response = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ password: $("password").value }),
  });
  $("password").value = "";
  if (response.ok) {
    await refreshDevices();
    return;
  }
  if (response.status === 429) {
    const data = await response.json().catch(() => ({}));
    $("login-error").textContent = `Too many failed attempts. Try again in ${data.retry_after || "a few"} seconds.`;
  } else {
    $("login-error").textContent = "Wrong password.";
  }
}

async function logout() {
  if (stream) stream.close();
  stream = null;
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  showLogin();
}

function openStream(deviceId, name) {
  if (stream) stream.close();
  stream = new StreamView(deviceId, name);
  stream.open();
  refreshDevices();
}

// ---------- live view ----------

class StreamView {
  constructor(deviceId, name) {
    this.deviceId = deviceId;
    this.name = name;
    this.ws = null;
    this.closedByUser = false;
    this.backoff = new Backoff();
    this.clock = new ClockSync();
    this.canvas = $("screen");
    this.ctx = this.canvas.getContext("2d");
    this.decoder = null;
    this.decoderCodec = null;
    this.decoderSize = "";
    this.waitKeyframe = true;
    this.lastKeyframeRequest = 0;
    this.jpegBusy = false;
    this.jpegPending = null;
    this.timers = [];
    this.resetStats();
  }

  resetStats() {
    this.statFrames = 0;
    this.statBytes = 0;
    this.statLatency = [];
    this.lastSize = "-";
  }

  open() {
    $("view-name").textContent = this.name;
    $("view-id").textContent = this.deviceId;
    show("view-panel", true);
    this.timers.push(setInterval(() => this.updateStats(), 1000));
    this.connect();
  }

  connect() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    this.setStatus("connecting", "Connecting");
    const ws = new WebSocket(`${scheme}://${location.host}/ws/view/${encodeURIComponent(this.deviceId)}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.backoff.reset();
      this.setStatus("connecting", "Connected, waiting for the device");
      for (let i = 0; i < 5; i++) setTimeout(() => this.ping(), i * 200);
      this.pingTimer = setInterval(() => this.ping(), PING_INTERVAL_MS);
    };
    ws.onmessage = (event) => {
      if (typeof event.data === "string") this.onControl(event.data);
      else this.onVideo(event.data);
    };
    ws.onclose = () => {
      clearInterval(this.pingTimer);
      this.resetDecoder();
      if (this.closedByUser || ws !== this.ws) return;
      this.reconnectLater();
    };
  }

  async reconnectLater() {
    const delay = this.backoff.next();
    this.setStatus("offline", `Connection lost, retrying in ${Math.round(delay / 1000)} s`);
    // A rejected handshake is invisible to the page; check whether the session ended.
    const response = await fetch("/api/devices", { cache: "no-store" }).catch(() => null);
    if (response && response.status === 401) {
      showLogin();
      return;
    }
    setTimeout(() => {
      if (!this.closedByUser) this.connect();
    }, delay);
  }

  close() {
    this.closedByUser = true;
    clearInterval(this.pingTimer);
    this.timers.forEach(clearInterval);
    if (this.ws) this.ws.close(1000);
    this.resetDecoder();
    show("view-panel", false);
  }

  send(message) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message));
  }

  ping() {
    this.send({ type: "ping", t0: localNowMs() });
  }

  requestKeyframe() {
    const now = performance.now();
    if (now - this.lastKeyframeRequest < KEYFRAME_REQUEST_MIN_MS) return;
    this.lastKeyframeRequest = now;
    this.send({ type: "keyframe_request" });
  }

  relayNowMs() {
    return localNowMs() + this.clock.offsetMs;
  }

  onControl(text) {
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.type === "pong") {
      this.clock.addSample(msg.t0, msg.t1, msg.t2, localNowMs());
    } else if (msg.type === "state") {
      if (!msg.online) this.setStatus("offline", "Device offline, waiting for it to come back");
      else if (msg.paused) this.setStatus("paused", "Viewing paused by the local user at the device");
      else if (msg.streaming) this.setStatus("live", "Live (view only)");
      else this.setStatus("connecting", "Device online, starting");
    }
  }

  onVideo(buffer) {
    const frame = parseFrame(buffer);
    if (!frame) return;
    this.statBytes += buffer.byteLength;
    if (frame.codec === CODEC_H264) this.decodeH264(frame);
    else if (frame.codec === CODEC_JPEG) this.decodeJpeg(frame);
  }

  decodeH264(frame) {
    if (typeof VideoDecoder === "undefined") {
      this.setStatus("offline", "This browser has no WebCodecs (it needs https or localhost)");
      return;
    }
    if (frame.keyframe) {
      const codec = codecFromAnnexB(frame.payload) || "avc1.42E01F";
      const size = `${frame.width}x${frame.height}`;
      if (!this.decoder || this.decoder.state === "closed" || codec !== this.decoderCodec || size !== this.decoderSize) {
        this.createDecoder(codec, frame.width, frame.height);
      }
      this.waitKeyframe = false;
    } else if (this.waitKeyframe || !this.decoder) {
      this.requestKeyframe();
      return;
    } else if (this.decoder.decodeQueueSize > MAX_DECODE_QUEUE) {
      this.waitKeyframe = true;
      this.requestKeyframe();
      return;
    }
    try {
      this.decoder.decode(
        new EncodedVideoChunk({
          type: frame.keyframe ? "key" : "delta",
          timestamp: frame.captureTsMs * 1000,
          data: frame.payload,
        }),
      );
    } catch (err) {
      console.warn("decode failed", err);
      this.resetDecoder();
      this.requestKeyframe();
    }
  }

  createDecoder(codec, width, height) {
    this.resetDecoder();
    const decoder = new VideoDecoder({
      output: (videoFrame) => this.drawVideoFrame(videoFrame),
      error: (err) => {
        console.warn("decoder error", err);
        if (this.decoder === decoder) {
          this.decoder = null;
          this.waitKeyframe = true;
          this.requestKeyframe();
        }
      },
    });
    decoder.configure({ codec, codedWidth: width, codedHeight: height, optimizeForLatency: true });
    this.decoder = decoder;
    this.decoderCodec = codec;
    this.decoderSize = `${width}x${height}`;
  }

  resetDecoder() {
    if (this.decoder && this.decoder.state !== "closed") this.decoder.close();
    this.decoder = null;
    this.waitKeyframe = true;
  }

  drawVideoFrame(videoFrame) {
    this.fitCanvas(videoFrame.displayWidth, videoFrame.displayHeight);
    this.ctx.drawImage(videoFrame, 0, 0, this.canvas.width, this.canvas.height);
    this.recordDisplayed(videoFrame.timestamp / 1000);
    videoFrame.close();
  }

  decodeJpeg(frame) {
    if (this.jpegBusy) {
      this.jpegPending = frame; // keep only the newest
      return;
    }
    this.jpegBusy = true;
    createImageBitmap(new Blob([frame.payload], { type: "image/jpeg" }))
      .then((bitmap) => {
        this.fitCanvas(bitmap.width, bitmap.height);
        this.ctx.drawImage(bitmap, 0, 0, this.canvas.width, this.canvas.height);
        bitmap.close();
        this.recordDisplayed(frame.captureTsMs);
      })
      .catch((err) => console.warn("jpeg decode failed", err))
      .finally(() => {
        this.jpegBusy = false;
        const next = this.jpegPending;
        this.jpegPending = null;
        if (next && !this.closedByUser) this.decodeJpeg(next);
      });
  }

  fitCanvas(width, height) {
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.lastSize = `${width}x${height}`;
  }

  recordDisplayed(captureTsMs) {
    this.statFrames += 1;
    if (this.clock.ready) this.statLatency.push(this.relayNowMs() - captureTsMs);
  }

  updateStats() {
    const latency = this.statLatency.length
      ? Math.round(this.statLatency.reduce((a, b) => a + b, 0) / this.statLatency.length) + " ms"
      : "-";
    $("stat-fps").textContent = String(this.statFrames);
    $("stat-res").textContent = this.lastSize;
    $("stat-lat").textContent = latency;
    $("stat-kbps").textContent = Math.round((this.statBytes * 8) / 1000) + " kbps";
    this.statFrames = 0;
    this.statBytes = 0;
    this.statLatency = [];
  }

  setStatus(kind, text) {
    const el = $("conn-status");
    el.className = "status " + kind;
    el.textContent = text;
  }
}

// ---------- wiring ----------

$("login-form").addEventListener("submit", login);
$("logout").addEventListener("click", logout);
$("disconnect").addEventListener("click", () => {
  if (stream) stream.close();
  stream = null;
  refreshDevices();
});
refreshDevices();
