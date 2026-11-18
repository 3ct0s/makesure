# MakeSure

View-only screen streaming for unattended Windows machines (signage, kiosks, monitoring terminals).
An operator can see what a remote screen shows. Nothing else: no mouse, no keyboard, no commands,
no files, no clipboard, no recording.

The remote machine only makes outbound HTTPS connections, so it works behind NAT and firewalls
with no port forwarding.

Guides: [setup](docs/SETUP.md), [daily use](docs/USAGE.md), [test results](docs/TEST_RESULTS.md).

## How it fits together

```
Windows machine                       Operator side                        Operator
+----------------+   wss (outbound)   +---------------------------+  https  +---------+
| MakeSureAgent  | -----------------> | tunnel -> relay (FastAPI) | <------ | browser |
| capture+H.264  |                    | auth, routing, web viewer |         | viewer  |
+----------------+                    +---------------------------+         +---------+
```

| Part | Where | What it does |
|---|---|---|
| Agent (`makesure/agent`) | Remote Windows machine, as `MakeSureAgent.exe` | Captures the primary screen with mss, encodes H.264 with PyAV, sends frames over one outbound WebSocket. Captures nothing while nobody is watching. |
| Relay (`makesure/relay`) | Operator side, bound to `127.0.0.1` | Authenticates devices and operators, routes one device's frames to its viewers, serves the viewer page. |
| Tunnel | Next to the relay | Gives the relay a public HTTPS address. Started by the relay from config. |
| Viewer (`makesure/viewer`) | Any browser with WebCodecs (Chrome, Edge, Safari) | Login, device list, live picture, FPS / resolution / latency / bitrate, Connect and Disconnect. |

Capture, encoder, transport and tunnel provider are each behind a small interface
(`CaptureSource`, `Encoder`, `Transport`, `TunnelProvider`), so one can be replaced without touching the others.

## Why WebSocket and not WebRTC

Free HTTP tunnels carry HTTP and WebSocket only. None of them passes UDP, so WebRTC media cannot
flow through them without a TURN server, which would be extra paid infrastructure. MakeSure sends
H.264 access units as binary WebSocket messages over HTTPS and decodes them in the browser with WebCodecs.
This was tested through a real Cloudflare tunnel, see [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md).

## Choosing a tunnel

Checked against the providers' own pages on 2026-11-18.

| Provider | Address | Limits that matter | Verdict |
|---|---|---|---|
| Cloudflare quick tunnel (`cloudflare_quick`) | Random, new on every start | No account. Meant for testing, no uptime guarantee, 200 requests in flight. | Use for trials and demos. This is what the tests used. |
| Cloudflare named tunnel (`none` + your own `cloudflared` service) | Fixed, on a domain you own | Needs a Cloudflare account and a domain on Cloudflare. No stated data cap. | Use for real deployments. |
| ngrok free (`ngrok`) | One fixed `ngrok-free.app` name | 1 GB outbound per month, about 1 to 4 hours of video. | Too small for regular use. |
| localhost.run free | Random, changes every few hours | Unpublished speed limit, HTTP only. | Not supported. |

Things to know:

- A quick tunnel gets a new address every time the relay restarts. Agents store the relay address,
  so after a restart they need the new one. A named tunnel avoids this.
- Cloudflare's terms say serving video through their CDN needs a paid video product. Whether that
  applies to Tunnel traffic is not clear. Keep the bitrate modest and keep the provider swappable.
- The tunnel provider terminates TLS, so it can technically see frames. There is no end-to-end
  encryption between agent and viewer yet.
- The `ngrok` provider's command line has not been tested.

## Quick start

### 1. Relay (operator machine: macOS, Linux or Windows)

```bash
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
cp config/relay.example.yaml relay.yaml
.venv/bin/python -m makesure.relay set-password --config relay.yaml
.venv/bin/python -m makesure.relay run --config relay.yaml
```

`cloudflared` must be installed for the default tunnel (`brew install cloudflared`). The relay prints:

```
Public viewer URL: https://<random>.trycloudflare.com
Agent relay_url:   wss://<random>.trycloudflare.com/ws/agent
```

The relay refuses to start until an operator password is set.

### 2. Enroll a device

```bash
.venv/bin/python -m makesure.relay enroll --config relay.yaml --name "Lobby kiosk"
```

This prints a single-use token that expires after one hour.

### 3. Agent (Windows machine, no Python needed)

Copy `MakeSureAgent.exe` to the machine and run it once with the token:

```
MakeSureAgent.exe --relay-url wss://<relay>/ws/agent --enroll <token>
```

After that, start it without `--enroll`. Put `relay_url` in `agent.yaml` next to the exe
(see `config/agent.example.yaml`) to avoid the flag.

### 4. Watch

Open the public viewer URL, log in, press Connect next to the device.

## What the person at the remote machine sees

- While someone is watching, a window stays on top of the screen: a red dot,
  "MakeSure: this screen is being viewed", the number of viewers and the device ID.
- "Stop viewing" ends the session and refuses new ones until "Allow" is pressed.
- "Quit" closes the agent.
- While nobody is watching, a small "connected (not viewing)" window remains so the agent can always be found and closed.

The indicator cannot be turned off by configuration or flags.

## Configuration

Agent (`agent.yaml`, every key can also be given as a flag, see `MakeSureAgent.exe --help`):

| Key | Default | Meaning |
|---|---|---|
| `relay_url` | none | `wss://<relay>/ws/agent` |
| `width`, `height` | 1280, 720 | Largest stream size. The screen is scaled down to fit, aspect ratio kept. |
| `fps` | 15 | Frames per second |
| `codec` | `h264` | `jpeg` is the fallback |
| `bitrate_kbps`, `min_bitrate_kbps`, `max_bitrate_kbps` | 1500, 300, 4000 | Start value and the range adaptive quality may use |
| `keyframe_interval_s` | 2.0 | |
| `monitor` | 1 | 1 is the primary monitor |
| `reconnect_max_s` | 30 | Longest wait between reconnect attempts |

Relay (`relay.yaml`): `host`, `port`, `data_dir`, `session_hours`, login lockout settings, and
`tunnel.provider` (`cloudflare_quick`, `ngrok`, `none`). See `config/relay.example.yaml`.

Data and logs: agent in `%LOCALAPPDATA%\MakeSure`, relay in `~/.makesure-relay`
(`%LOCALAPPDATA%\MakeSureRelay` on Windows).

Relay commands: `run`, `set-password`, `enroll`, `devices`, `revoke`.

## Security

- Each device creates its own Ed25519 key on first run, stored in the user's data folder, never in the exe.
  The device ID is derived from the public key.
- Enrollment needs a single-use, expiring token from the operator. Later connections are a signed challenge.
  `revoke` removes a device.
- Operators log in with a password (stored as a scrypt hash). Sessions use an HttpOnly, SameSite=Strict cookie.
  Repeated bad logins lock out the address. Viewer sockets need the cookie and a matching Origin.
- No endpoint shows devices or frames without a session.
- The agent accepts only: start, stop, keyframe request, quality hint, ping. Everything else is ignored.
  A test checks that the agent imports no input, subprocess or clipboard library.
- Nothing writes frames to disk.

## Building the Windows exe

PyInstaller cannot build a Windows exe on another system. Either:

- On Windows: `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1`
  (result in `dist\MakeSureAgent.exe`; add `-Console` for a build that shows logs in a console), or
- Push to GitHub: `.github/workflows/windows.yml` runs the tests on Windows, builds the exe and
  uploads it as the `MakeSureAgent` artifact.

It is a single file of about 54 MB. FFmpeg comes inside the PyAV wheel, so there are no separate binaries.
If single-file mode ever causes trouble, `packaging/agent.spec` documents the one-folder fallback.

## Tests

```bash
uv pip install -r requirements-dev.txt
MAKESURE_TEST_HEADLESS_INDICATOR=1 .venv/bin/python -m pytest -q
```

Real measurements, including the exe on Windows through the tunnel: [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md).

## Known limits

- Primary monitor only per stream; no audio.
- The Windows lock screen and UAC prompts are expected to show as black frames (not tested).
- The "Stop viewing" pause and operator logins do not survive a restart of the agent or relay.
- Latency figures depend on clock estimates between agent, relay and viewer.
