# Setup guide

This guide takes you from nothing to a running relay and one enrolled Windows machine.
For day-to-day use after that, see the [user guide](USAGE.md).

What runs where:

| Part | macOS | Linux (Ubuntu) | Windows |
|---|---|---|---|
| Relay and tunnel (operator side) | Yes, tested | Yes, steps below, not yet run | Yes, steps below, tests pass |
| Viewer | Any current browser | Any current browser | Any current browser |
| Agent (the machine being viewed) | From source, for trials | Not supported | Yes, as `MakeSureAgent.exe` |

There are two machines:

- **Operator machine**: runs the relay and the tunnel. macOS, Linux or Windows. Needs Python 3.12.
- **Remote machine**: the Windows 10/11 PC whose screen you want to see. Needs nothing but `MakeSureAgent.exe`.

## Part 1: the relay (operator machine)

### 1.1 Install the tools

macOS:

```bash
brew install uv cloudflared
```

Ubuntu and other Debian-based Linux (64-bit Intel/AMD):

```bash
sudo apt update
sudo apt install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
```

On an ARM machine (for example a Raspberry Pi with a 64-bit system) use `cloudflared-linux-arm64.deb` instead.
You do not need to install Python yourself: `uv venv --python 3.12` in the next step downloads it.
Check both tools with `uv --version` and `cloudflared --version`.

The rest of Part 1 is the same on Linux. To keep the relay running after you log out or reboot,
see "Run the relay as a service on Linux" below.

Windows 10 or 11, in PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
winget install --id Cloudflare.cloudflared -e
```

Close PowerShell and open it again so the new commands are found, then check them with
`uv --version` and `cloudflared --version`. As on the other systems, `uv` downloads Python 3.12 for you.

On Windows the commands in the rest of this guide change in two small ways:

| In this guide | On Windows |
|---|---|
| `.venv/bin/python` | `.venv\Scripts\python` |
| `cp config/relay.example.yaml relay.yaml` | `copy config\relay.example.yaml relay.yaml` |

So Part 1 on Windows is, in full:

```powershell
git clone https://github.com/3ct0s/makesure.git
cd makesure
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
copy config\relay.example.yaml relay.yaml
.venv\Scripts\python -m makesure.relay set-password --config relay.yaml
.venv\Scripts\python -m makesure.relay run --config relay.yaml
```

The first time the relay starts, Windows may ask whether Python may use the network. Allow it for private networks;
the relay only listens on this machine and reaches the internet through `cloudflared`.

The relay's test suite passes on Windows, but the relay together with its tunnel has only been run on macOS so far,
so treat the first Windows run as a test.

No Cloudflare account is needed for the default tunnel on any system.

### 1.2 Get the code and install it

```bash
git clone https://github.com/3ct0s/makesure.git
cd makesure
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
```

### 1.3 Create the relay config

```bash
cp config/relay.example.yaml relay.yaml
```

The defaults work as they are: the relay listens on `127.0.0.1:8080` and starts a Cloudflare quick tunnel.
`relay.yaml` is ignored by git.

### 1.4 Set the operator password

```bash
.venv/bin/python -m makesure.relay set-password --config relay.yaml
```

Type a long password when asked. Only its hash is stored. The relay will not start without one.

### 1.5 Start the relay

```bash
.venv/bin/python -m makesure.relay run --config relay.yaml
```

After a few seconds it prints two addresses:

```
Public viewer URL: https://<random-words>.trycloudflare.com
Agent relay_url:   wss://<random-words>.trycloudflare.com/ws/agent
```

Keep this terminal open. The relay and the tunnel run as long as it does.

## Part 2: the agent (remote Windows machine)

### 2.1 Get the exe

Either download the `MakeSureAgent` artifact from the latest successful run of the `windows`
workflow under the repo's Actions tab, or build it yourself on a Windows PC with Python 3.12:

```
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

The result is `dist\MakeSureAgent.exe`, one file of about 54 MB. Copy it to the remote machine, for example to `C:\MakeSure\`.

Windows SmartScreen may warn about the exe because it is not code-signed. That is expected for now.

### 2.2 Create an enrollment token

On the operator machine, in a second terminal:

```bash
.venv/bin/python -m makesure.relay enroll --config relay.yaml --name "Lobby kiosk"
```

It prints a token. The token works once and expires after one hour.

### 2.3 Enroll the machine

On the remote machine, open a command prompt in the folder with the exe:

```
MakeSureAgent.exe --relay-url wss://<random-words>.trycloudflare.com/ws/agent --enroll <token>
```

A small MakeSure window appears and the device shows up in the viewer as online.
The machine now has its own key, stored in `%LOCALAPPDATA%\MakeSure`. The token is no longer needed.

### 2.4 Save the settings

So the agent starts without any flags, create `agent.yaml` next to the exe
(copy `config/agent.example.yaml` as a starting point) with at least:

```yaml
relay_url: wss://<random-words>.trycloudflare.com/ws/agent
```

From now on, start the agent by double-clicking `MakeSureAgent.exe`.

### 2.5 Start with Windows (optional)

Press Win+R, type `shell:startup`, and put a shortcut to `MakeSureAgent.exe` in the folder that opens.
The agent then starts when that user logs in. It must run inside a logged-in desktop session, not as a Windows service,
because it needs to see the screen and show its indicator window.

## Part 3: check that it works

1. Open the public viewer URL in Chrome, Edge or Safari.
2. Log in with the operator password.
3. Press Connect next to the device.

You should see the remote screen within a second, and the remote machine should show
"MakeSure: this screen is being viewed".

## A fixed address (recommended for real use)

The quick tunnel gets a new random address every time the relay restarts, and every agent then
needs the new address in its `agent.yaml`. To get an address that never changes, use a Cloudflare
named tunnel. It is free, but you need a Cloudflare account and a domain managed by Cloudflare.

These are Cloudflare's standard steps. This setup has not been tested with MakeSure yet.

```bash
cloudflared tunnel login
cloudflared tunnel create makesure
cloudflared tunnel route dns makesure view.yourdomain.com
cloudflared tunnel run --url http://127.0.0.1:8080 makesure
```

Then tell the relay not to start its own tunnel, in `relay.yaml`:

```yaml
tunnel:
  provider: none
  public_url: https://view.yourdomain.com
```

Agents use `relay_url: wss://view.yourdomain.com/ws/agent`.

## Run the relay as a service on Linux

This keeps the relay (and its tunnel) running in the background and restarts it after a crash or reboot.
The example assumes the code is in `/home/youruser/makesure` and that `relay.yaml` is there too. Adjust the user name and paths.

Create `/etc/systemd/system/makesure-relay.service`:

```ini
[Unit]
Description=MakeSure relay
After=network-online.target
Wants=network-online.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/makesure
ExecStart=/home/youruser/makesure/.venv/bin/python -m makesure.relay run --config relay.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now makesure-relay
journalctl -u makesure-relay -f
```

The last command shows the log, including the public viewer URL. Stop the service with
`sudo systemctl stop makesure-relay`; systemd also stops the tunnel process it started.

With the quick tunnel, every service restart gives a new address, so on a server a fixed address
(previous section) is strongly recommended.

The relay has been tested on macOS, and its test suite also passes on Windows. It has not been run on Linux yet;
it uses nothing platform-specific, but treat the first Linux run as a test.

The agent is for Windows. Running it on a Linux desktop is not supported.

## Trying the agent on a Mac

For a quick trial you can run the agent from source on macOS:

```bash
.venv/bin/python -m makesure.agent --relay-url wss://<relay>/ws/agent --enroll <token>
```

macOS only lets a program capture other windows after you allow it under
System Settings, Privacy & Security, Screen & System Audio Recording (for the terminal or app that started the agent).
Without that permission the stream shows only the MakeSure window on a blank background.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Relay says no password is set | Run `set-password` with the same `--config` file. |
| Relay cannot start the tunnel | `cloudflared` is not installed or not on `PATH`. Set `tunnel.cloudflared_path` in `relay.yaml` if it lives elsewhere. |
| Agent logs "cannot reach relay" and keeps retrying | The relay or tunnel is down, or the relay address changed after a restart. Update `relay_url`. |
| Enrollment refused | The token was already used or is older than one hour. Create a new one. |
| Device shows offline | The agent is not running, or the user pressed Quit. Start the exe again. |
| Viewer shows a device as paused | Someone at the remote machine blocked viewing. They must press "Allow". |
| Picture is blank on a Mac agent | Screen Recording permission is missing, see above. |
| Connected but no picture | The browser may lack WebCodecs. Use a current Chrome, Edge or Safari, or set `codec: jpeg` on the agent. |

Logs: agent in `%LOCALAPPDATA%\MakeSure`, relay in `~/.makesure-relay` (`%LOCALAPPDATA%\MakeSureRelay` on Windows).
For a build that prints its log in a console window, build with `-Console`.
