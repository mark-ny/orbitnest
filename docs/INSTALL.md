# Installing OrbitNest Workspaces

> None of the installers below could be run on their target platforms in the development sandbox
> (no Termux, Windows, WSL, Docker or package index there). They were syntax-checked and their
> logic reviewed; treat the first run on each platform as a test and report problems.

Every platform needs Python 3.11 or newer and a working internet connection to download packages
the first time. Where isolation matters (keeping a workspace's files and network separate from
everything else), you also need Docker; without it OrbitNest still runs, but workspaces are
disabled and the dashboard explains why.

After any of the installs below, a single command handles day-to-day use: `orbitnest start`,
`orbitnest stop`, `orbitnest status`, and `orbitnest update` to pull and apply the latest code from
GitHub. The installer puts this `orbitnest` command on your `PATH`, so you can run it from any
directory, in any new terminal, without first activating a virtual environment or remembering
where the server lives. The rest of this page explains what each installer does and why, so a
first-time install goes smoothly; after that, `orbitnest` is the only command most people need.

## Linux, with Docker, for real workspace isolation

This is the path to use on a desktop, laptop or server running Linux directly (not inside WSL —
see the Windows section below for that).

1. Get the code: clone the repository with `git clone <repo> orbitnest`, then move into it with
   `cd orbitnest`.
2. Run the installer: `scripts/install-linux.sh`. It creates a private Python environment under
   `~/.local/share/orbitnest`, installs the dependencies into it, writes a configuration file at
   `~/.config/orbitnest/orbitnest.env` with a freshly generated secret key (back this file up; it
   also holds your password-hashing key), and links the `orbitnest` command into `~/.local/bin`. If
   that directory is not already on your shell's `PATH`, the installer tells you the one line to
   add to your shell's startup file so it is found the next time you open a terminal.
3. Create your administrator account: `orbitnest create-admin <username>`. It prompts for a
   password; nothing is printed back to the terminal while you type it.
4. Start the server: `orbitnest start`. It runs migrations, starts the process in the background,
   writes its log to your data directory, and waits briefly to confirm it answered before printing
   the address — normally `http://127.0.0.1:8765`. Open that address in a browser on the same
   machine and sign in.

Separately from OrbitNest itself, install Docker Engine and add your own user to the `docker`
group so it can be used without `sudo` (on Debian or Ubuntu: `sudo usermod -aG docker $USER`, then
log out and back in for it to take effect). Whoever can use the Docker socket effectively has root
on that machine, because OrbitNest's control plane needs that same power to create and destroy
containers — keep that access as tightly held as you would root itself.

To run OrbitNest as a proper background service instead of a terminal session, see
`deploy/systemd/orbitnest.service`; its header explains the one-time setup. If you use that, prefer
`systemctl --user` to start and stop it, and let `orbitnest update` detect and restart it instead
of the plain background process.

## Windows 10 or 11, through WSL 2 and Docker Desktop

OrbitNest's control plane relies on POSIX file semantics, so on Windows it runs inside the Windows
Subsystem for Linux (WSL 2), not as a native Windows program. Docker Desktop then shares its engine
with that WSL environment.

1. If you have not already, install WSL 2 and a Linux distribution: open PowerShell as
   Administrator and run `wsl --install -d Ubuntu`, then reboot when it asks. Install Docker
   Desktop, and in its Settings enable "Use the WSL 2 based engine" and the WSL integration for the
   distribution you just installed.
2. From an ordinary (non-administrator) PowerShell window, inside the repository folder, run
   `.\scripts\install-windows.ps1`. It copies the project into your WSL home directory (files kept
   under `/mnt/c` are slow and lack Unix permissions, so a copy is faster and more reliable than
   running in place) and then runs the same Linux installer described above inside WSL.
3. The script finishes by printing the next commands. Open a WSL shell (`wsl -d Ubuntu`, or your
   distribution's name) and run `orbitnest create-admin <username>`, then `orbitnest start`. WSL 2
   forwards `localhost`, so `http://127.0.0.1:8765` opens directly in your normal Windows browser —
   no separate WSL-side browser is needed.

From then on, update and restart with one command from inside WSL: `orbitnest update`. From
PowerShell without opening a WSL shell yourself, the equivalent is `wsl orbitnest update`.

## Termux (Android, no root required)

Termux gives you a Linux-like command line on an Android phone or tablet, but it is not a
container host: there is no Docker here, so workspaces created this way are ordinary folders and
processes running as your Termux user, not isolated containers. Treat this as a thin, portable
client and control plane — for real isolation, either install on a remote Linux machine and use
Termux only as a browser (see the next section), or wait for the sandboxed local-browser feature
described in `BUILD_STATUS.md`, once it ships.

1. Install `git` first, since Termux does not ship it by default: `pkg install git`.
2. Clone the repository and move into it: `git clone <repo> orbitnest && cd orbitnest`.
3. Run `bash scripts/install-termux.sh`. It creates a Python environment under `~/.orbitnest`,
   installs dependencies, and links the `orbitnest` command. One dependency, `pydantic-core`, is
   written in Rust; Termux usually has no prebuilt package for it, so the installer also installs a
   Rust toolchain and compiles it on first run. That compile step is the slow part of this install —
   it can take many minutes and use a few hundred megabytes of temporary storage — everything after
   it is quick.
4. Create your administrator with `orbitnest create-admin <username>`, then start the server with
   `orbitnest start`. Open `http://127.0.0.1:8765` in the phone's own browser.

Two things are worth knowing about running a server on a phone: Android aggressively kills
background processes to save battery, so run `termux-wake-lock` while OrbitNest is running and
exempt Termux from battery optimization in Android's settings, or expect the server to stop when
the screen is off for a while; and the server listens only on the phone's own loopback address, by
design, so it is not reachable from other devices on the network.

## Remote Linux host or VPS (recommended if you want real isolation from a phone)

If your own device cannot run Docker — Termux being the main case — put OrbitNest on a small Linux
server instead and reach it as a client from any device's browser, including Termux's.

1. Install it on the server exactly as in the Linux section above, but once it is confirmed
   working on `127.0.0.1`, put a TLS reverse proxy in front of it rather than exposing the raw
   HTTP port: `deploy/caddy/Caddyfile` gets you automatic HTTPS certificates with the least setup,
   or use `deploy/nginx/orbitnest.conf` if you already run nginx.
2. In the server's configuration file, set `ORBITNEST_PUBLIC_URL` to the HTTPS address you will
   use, and `ORBITNEST_TRUSTED_PROXY=true` so the application trusts the `X-Forwarded-*` headers
   the proxy adds.
3. At the firewall, allow only ports 80 and 443 (plus SSH for your own access) from the internet.
   Separately, make sure containers created by OrbitNest cannot reach the cloud metadata service at
   `169.254.169.254` or the control-plane's own port — OrbitNest does not configure your host
   firewall for you, so this is a step you take once, yourself, outside the application.
4. From then on, any device's browser — including a phone running only Termux, with no Docker at
   all — can reach the server over HTTPS and use fully isolated, containerized workspaces.

A non-loopback bind is refused by default; OrbitNest only listens on a public address at all when
`ORBITNEST_ALLOW_NON_LOOPBACK=true` and the matching environment secret are both set, specifically
to make an accidental, unencrypted exposure hard to do by mistake.

## Docker Compose

A compose file exists at `deploy/compose/docker-compose.yml`, but it mounts the Docker socket into
a container, which hands that container the same host-root power described above — read the
warning at the top of that file before using it. It has not been exercised in development.

## Building the browser, desktop and Tor gateway images

The streaming templates — Chromium, Firefox, Tor Browser, the Ubuntu and Kali desktops, and the
Security Lab — are not downloaded from a registry; they are built locally from the `docker/`
folder the first time you use them, so you know exactly what is in them. The same is true of the
Tor gateway, the small helper container that gives other templates private, Tor-routed networking
(see "Staying anonymous" below) — it is not a template itself, so it is listed separately.

The easiest way to build any of these is from the web interface, as an administrator: open
**Admin → Templates**, and click **Install image** next to a template, or **Install / rebuild Tor
gateway image** in the row above the table. The same button appears automatically if you try to
create a workspace from a template whose image is not built yet. A build can take anywhere from a
few minutes to over an hour depending on your connection — Chromium's dependencies in particular
are large — and its log streams live in the dialog; you can close the dialog without stopping the
build and check back later from the same place.

The same thing is available from the command line, for scripting or for a server with no browser
handy: `orbitnest build-images` builds everything buildable, or name specific ones, for example
`orbitnest build-images stream-chromium tor-gateway`. The Security Lab and Kali desktop images are
particularly large (several gigabytes) because they carry a real toolset; expect their first build
to take longest.

Whichever way you build an image, no restart is needed afterward — just reload the Templates page.
Each workspace needs roughly 1–2 GB of RAM on top of whatever you've given it, so size your host
accordingly if you expect several running at once. If an administrator's authenticator app is lost,
recover the account with `orbitnest disable-mfa <username>` from the server itself.

## Staying anonymous: what the Tor routing does and does not do

Several templates — the browsers, the desktops and the Security Lab — can route all of their
network traffic through Tor instead of your own internet connection. Each such workspace gets its
own small gateway container that owns the network connection and firewall rules; the workspace
itself is given no networking privileges of its own and can reach the outside world only through
that gateway, which is configured to fail closed: if the gateway cannot come up and prove it is
ready, the workspace is never started, rather than quietly leaking traffic outside Tor. You choose
"Through Tor" or "Direct" when creating a workspace that offers the choice, and the page tells you
plainly which one is currently in effect.

This is meaningfully different from running a browser that simply happens to connect through Tor.
It is also meaningfully different from Tails, the dedicated anonymity operating system this
project is sometimes compared to, and from the Tor Browser, Tor's own hardened browser (also
available here as its own template, running its own separate Tor process). An ordinary Chromium or
Firefox routed through this Tor gateway still presents an ordinary Chromium or Firefox fingerprint
to every site it visits, and anything you sign in to on that site still identifies you to that
site, same as it would anywhere else. Your own internet provider can still see that you are
connecting to Tor, even though it cannot see what you do once inside it. For genuine anonymity
against a motivated adversary, the Tor Browser template — or real Tails, on real hardware — remains
the stronger choice; the Tor-routed browsers here are for keeping casual browsing off your own IP
address, not for operating under serious threat.

## Operations

Besides `start`, `stop`, `restart`, `status` and `update`, the `orbitnest` command also accepts the
administrative subcommands below — run any of them the same way, for example `orbitnest backup`:

- `backup [--dest DIR] [--include-workspaces]` — writes a consistent snapshot you can take while
  the server keeps running.
- `restore --archive FILE` — restores a backup, but only into an empty data directory, so it never
  overwrites a live install by accident.
- `cleanup [--apply]` — reports orphaned runtime resources and purged data directories; it is a dry
  run unless you add `--apply`.
- `revoke-sessions` — signs every user out everywhere, for example after a suspected compromise.
- `reset-password <username>` — resets one user's password from the command line.
- `migrate` — applies any pending database migrations; `orbitnest update` already runs this for
  you, so you rarely need it directly.

To update by hand instead of with `orbitnest update` — for instance on a host where you'd rather
review a `git diff` before applying it — the equivalent steps are: stop the service, take a backup,
`git pull`, re-run the installer script for your platform, run `orbitnest migrate`, then start the
service again. Either way, keep backup archives private: they contain the server's signing key and
users' password hashes.
