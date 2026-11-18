# User guide

This guide covers daily use once the relay is running and at least one machine is enrolled.
If you are not there yet, start with the [setup guide](SETUP.md).

## For the operator

### Start and stop the relay

```bash
.venv/bin/python -m makesure.relay run --config relay.yaml
```

Stop it with Ctrl+C in that terminal. That also stops the tunnel.
If the relay is killed some other way, check that no `cloudflared` process is left running.

With the default quick tunnel the public address changes on every start. See "A fixed address" in the setup guide.

### Watch a screen

1. Open the viewer URL and log in.
2. The device list shows every enrolled machine with its name, device ID, online or offline status and how many people are watching.
3. Press **Connect** on an online device. The live picture appears below the list.
4. Press **Disconnect** when done. When the last viewer leaves, the remote machine stops capturing its screen.

Above the picture:

| Readout | Meaning |
|---|---|
| FPS | Frames shown per second. 15 is the default target. |
| Resolution | Size of the stream. The screen is scaled to fit 1280x720, keeping its shape. |
| Latency | Time from capture on the remote machine to display in your browser. |
| Bitrate | Data rate of the stream. Still screens need very little; busy screens more. |

Several operators can watch the same device at once. If the connection drops, the viewer
reconnects by itself and the picture returns when the device is back.

You can only look. There is no way to click, type, run anything, copy files or read the clipboard
on the remote machine, and nothing is recorded.

### Add a machine

```bash
.venv/bin/python -m makesure.relay enroll --config relay.yaml --name "Shop window screen"
```

Run the exe on that machine once with the printed token, as in the setup guide. Each token works for one machine, once, within one hour.

### List and remove machines

```bash
.venv/bin/python -m makesure.relay devices --config relay.yaml
.venv/bin/python -m makesure.relay revoke --config relay.yaml ms-xxxxxxxxxxxxxxxx
```

A revoked machine is disconnected within seconds and cannot come back without a new enrollment.

### Change the operator password

```bash
.venv/bin/python -m makesure.relay set-password --config relay.yaml
```

Restart the relay afterwards so the new password is certain to be in effect. Operators have to log in again after any relay restart.

After five wrong passwords from one address, that address is locked out for 15 minutes.

### Tune the picture

Set these in `agent.yaml` on the remote machine, then restart the agent:

| Goal | Setting |
|---|---|
| Use less data | Lower `max_bitrate_kbps` (for example 800) or `fps` (for example 5) |
| Sharper text | Raise `width` and `height` to the screen's real size, and raise `max_bitrate_kbps` |
| Another monitor | `monitor: 2` |
| Browser cannot decode H.264 | `codec: jpeg` (uses more data) |

The agent also adjusts by itself: on a slow connection it lowers bitrate, then frame rate, then size, and raises them again when the link recovers.

## For the person at the remote machine

MakeSure never runs hidden. Its window tells you what is happening:

| Window shows | Meaning |
|---|---|
| "MakeSure connected (not viewing)" | The agent is running. Nobody is watching and the screen is not being captured. |
| Red dot, "MakeSure: this screen is being viewed", viewer count | Someone is watching right now. The window stays on top of everything. |
| "MakeSure: viewing paused by the local user" | You blocked viewing. Nobody can watch until you press "Allow". |

Buttons:

- **Stop viewing** (while someone watches): ends the session at once and blocks new ones.
- **Block viewing** (while nobody watches): blocks sessions before they start.
- **Allow**: lets operators watch again.
- **Quit**: closes the agent. The machine shows as offline until the exe is started again.

The pause is not remembered across a restart of the agent.

## What to expect

Measured with the Windows exe through the Cloudflare tunnel (details in [TEST_RESULTS.md](TEST_RESULTS.md)):

- 15 FPS at up to 720p, about 0.15 s behind real time.
- About 700 kbps for a screen with steady movement, far less for a still screen.
- About 100 MB of memory on the remote machine while someone watches, about 35 MB otherwise.
- After a network drop the agent retries on its own, waiting up to 30 s between attempts.

## Good practice

- Tell the people who work at or near a monitored machine that it can be viewed, and why.
- Use a long operator password and share the viewer address only with operators.
- Revoke machines you no longer manage.
- The tunnel provider carries the video and could technically see it. Do not point MakeSure at screens showing data you would not send through that provider.
