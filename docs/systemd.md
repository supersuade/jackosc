# jackosc as a systemd user service

jackosc is a JACK client, and JACK runs inside your login session
(PipeWire-JACK with rtkit-granted realtime privileges). The bridge must
therefore run in the same session — so it is installed as a **systemd
user unit**, never a system unit: it inherits the JACK graph, rtkit
privileges, and your XDG paths, starts and stops with your desktop, and
needs no sudo.

## Prerequisites

- Python ≥ 3.11 with jackosc installed (pip, venv, or pipx — the unit's
  `ExecStart` is written to match automatically).
- A running systemd user session: `systemctl --user status` should work.
- PipeWire-JACK (or another JACK server) running in the session. On most
  distros the `pipewire-jack` package plus your login does this.

## Quick install

```console
$ pip install jackosc            # or: pip install --user, or a venv/pipx
$ jackosc systemd install
INFO jackosc: wrote ~/.config/systemd/user/jackosc.service
INFO jackosc: started — see: systemctl --user status jackosc
```

That writes the unit, `daemon-reload`s, and enables + starts it. The
unit's `ExecStart` is generated from the exact interpreter you ran the
command with, so venv/pipx installs need no manual path editing.

Useful flags on `systemd install`:

| Flag | Meaning |
|---|---|
| `--port PORT` | web port (default 8080) |
| `--host HOST` | bind address (default 127.0.0.1) |
| `--lan` | bind all interfaces (0.0.0.0) — see LAN below |
| `--no-enable` / `--no-start` | write the unit only / write + enable only |
| `JACKOSC_AUTH_TOKEN=…` | set in the environment to embed the token in the unit |

## Manual install

If you don't want to use the CLI, install the checked-in template:

```console
$ mkdir -p ~/.config/systemd/user
$ cp packaging/jackosc.service ~/.config/systemd/user/
$ systemctl --user daemon-reload
$ systemctl --user enable --now jackosc.service
```

The template's `ExecStart` assumes `pip install --user`; edit it for a
venv (`/path/to/venv/bin/python -m jackosc`) or pipx
(`%h/.local/pipx/venvs/jackosc/bin/jackosc`).

The config file defaults to `~/.config/jackosc/config.json` (created on
the first save in the UI); point elsewhere with `--config` in
`ExecStart`.

---

## Common configurations

### 1. Default — loopback only

The default bind is `127.0.0.1`: the UI is reachable only from the
machine itself. This is the safe default — no auth token needed because
nobody else can reach the port. Nothing to do.

### 2. LAN access (other machines on your network)

```console
$ jackosc systemd uninstall
$ JACKOSC_AUTH_TOKEN=change-me jackosc systemd install --lan
WARNING jackosc: --lan exposes the web UI to the LAN and config writes are OPEN — …
```

`--lan` binds `0.0.0.0` and prints the LAN URL at startup:

```
INFO jackosc: web UI at http://0.0.0.0:8080 (LAN: http://192.168.1.20:8080, config writes require auth token)
```

Without the CLI: change `--host 127.0.0.1` to `--host 0.0.0.0` in the
unit's `ExecStart` and `systemctl --user daemon-reload` + `restart`.

Security model on the LAN:

- **Reads are open by design** — spectra, config, and the live OSC
  packet stream are visible to anyone who can reach the port. That's the
  point of the analysis UI.
- **Writes (config changes) require the bearer token.** With no token,
  any LAN host can change config — including adding OSC targets. On a
  trusted LAN that may be fine; otherwise set `JACKOSC_AUTH_TOKEN`
  (see #4). The startup log says `config writes OPEN` or `require auth
  token` so you can't miss which mode you're in.
- A web page you visit cannot write to it (no CORS middleware; JSON
  writes are preflighted and blocked). The practical threat is other LAN
  hosts, not websites.

Firewall (if you run one — ufw):

```console
$ sudo ufw allow 8080/tcp comment 'jackosc web UI'
```

firewalld:

```console
$ sudo firewall-cmd --permanent --add-port=8080/tcp
$ sudo firewall-cmd --reload
```

### 3. Different port

```console
$ jackosc systemd install --port 9000
```

or a drop-in to override an existing unit:

```console
$ systemctl --user edit jackosc.service
```

```ini
[Service]
ExecStart=
ExecStart=/home/me/.local/bin/jackosc --host 127.0.0.1 --port 9000
```

(`ExecStart=` must be cleared first — systemd drop-ins append.)

### 4. Auth token — three ways

Precedence: `JACKOSC_AUTH_TOKEN` env > `--auth-token` flag > config
`auth_token`.

a) **At install time** — embedded into the unit:

```console
$ JACKOSC_AUTH_TOKEN=hunter2 jackosc systemd install --lan
```

b) **In the unit** — edit `~/.config/systemd/user/jackosc.service`:

```ini
[Service]
Environment=JACKOSC_AUTH_TOKEN=hunter2
```

c) **From a file** (keeps the token out of the unit):

```ini
[Service]
EnvironmentFile=%h/.config/jackosc/env
```

```console
$ echo 'JACKOSC_AUTH_TOKEN=hunter2' > ~/.config/jackosc/env
$ chmod 600 ~/.config/jackosc/env
```

Then use the token in the UI: the first write prompts for it, or set it
as a browser password for the site.

### 5. venv / pipx installs

`jackosc systemd install` bakes the interpreter that ran it into
`ExecStart`, so nothing else changes:

```console
$ python3 -m venv ~/jackosc-venv
$ ~/jackosc-venv/bin/pip install jackosc
$ ~/jackosc-venv/bin/jackosc systemd install        # ExecStart=/home/me/jackosc-venv/bin/python3 -m jackosc …
```

pipx:

```console
$ pipx install jackosc
$ ~/.local/pipx/venvs/jackosc/bin/jackosc systemd install
```

### 6. Second instance (another JACK name / port / config)

Each instance needs its own unit, JACK client name, config file, and
port. Copy the installed unit and edit:

```console
$ cp ~/.config/systemd/user/jackosc.service ~/.config/systemd/user/jackosc-lights.service
$ systemctl --user edit --full jackosc-lights.service
```

```ini
[Unit]
Description=jackosc (lights) — JACK audio to OSC bridge
After=pipewire.service jackosc.service

[Service]
ExecStart=/home/me/.local/bin/jackosc --jack-name lights --config /home/me/.config/jackosc/lights.json --port 8081

[Install]
WantedBy=default.target
```

```console
$ systemctl --user daemon-reload
$ systemctl --user enable --now jackosc-lights.service
```

### 7. JACK→OSC only, no web UI

The bridge itself (analysis + OSC send) runs fine without the web
server; the `--no-web` flag skips it entirely:

```ini
ExecStart=/home/me/.local/bin/jackosc --no-web
```

### 8. Live tweaks without reinstalling

`systemctl --user edit jackosc.service` opens a drop-in for ad-hoc
changes (ports, tokens, extra flags); `--full` edits the whole unit.
Writes are applied on save for user units, then `systemctl --user
restart jackosc`.

---

## Day to day

```console
$ systemctl --user status jackosc          # state, PID, recent log lines
$ journalctl --user -u jackosc -f          # follow the logs
$ systemctl --user restart jackosc
$ systemctl --user stop jackosc
$ systemctl --user disable --now jackosc   # stop + don't start at login
```

`WantedBy=default.target` means the service starts when your desktop
session starts and stops when it ends.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Log says `audio unavailable` | PipeWire-JACK not up: `systemctl --user status pipewire`; on some distros `pipewire-jack` isn't installed. The reconnect monitor retries every 2 s, so starting JACK later heals it. |
| `port already in use` | Another jackosc (or anything) on the port: `ss -ltnp \| grep :8080`; change port or stop the other instance. |
| UI reachable from this machine, not from the LAN | Unit still binds `127.0.0.1` — reinstall with `--lan`, or a firewall is blocking the port. |
| Startup log says `config writes OPEN` on a LAN bind | No token — set `JACKOSC_AUTH_TOKEN` and restart. |
| `systemctl` says `Failed to connect to bus` | No systemd user session in this context (e.g. ssh without lingering): `loginctl enable-linger $USER` and re-login. |
