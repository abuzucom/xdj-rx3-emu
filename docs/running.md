# Running the XDJ-RX3 emulator

The emulator boots the real XDJ-RX3 player firmware under WSL 2. A bridge
on TCP 127.0.0.1:4480 exposes the screen, the audio, the controls, and two
virtual USB sticks to client programs.

## Requirements

- Windows 10 version 17063 or newer.
- WSL 2 with an Ubuntu distribution.
- Python 3 on Windows with the `py` launcher.
- Windows `tar.exe` at `C:\Windows\System32\tar.exe`.
- About 2 GB of free disk space in the WSL home.
- WSL packages: `python3 curl rsync qemu-user mtools git tar unzip p7zip-full`.

Install WSL from an elevated PowerShell when missing:

```
wsl --install
```

A reboot follows. Install Ubuntu from the Microsoft Store afterward.

Install the WSL packages inside the distribution:

```
sudo apt-get update && sudo apt-get install -y python3 curl rsync qemu-user mtools git tar unzip p7zip-full
```

## Quick start

1. Double-click `rx3.cmd` in the repository root.
2. Wait for the bootstrap phases to finish.
3. Watch the emulator console window. The bridge listens on 127.0.0.1:4480 after about a minute.

Command-line switches shape the run:

- `rx3.cmd --check-only` runs the environment checks and stops.
- `rx3.cmd --no-run` bootstraps without launching the emulator.
- `rx3.cmd --no-usb` skips the virtual USB seeding.
- `rx3.cmd --skip-download` requires a local `firmware/XDJ-RX3_v120.zip` instead of downloading it.
- `rx3.cmd --usb1-source "PATH"` syncs a Windows music folder into the virtual USB1 stick. Also set via the `RX3_USB1_SOURCE` environment variable.
- `rx3.cmd --no-usb1-sync` skips the USB1 music sync even when `RX3_USB1_SOURCE` is set.

Every phase is idempotent. Re-run `rx3.cmd` after a failure. The plan skips finished phases.

## What the bootstrap does

| Phase | Action |
|---|---|
| Check environment | Verifies WSL, the distribution, Windows tar, and the WSL packages. |
| Stage WSL files | Copies the repository `wsl/` directory into `~/rx3`. |
| Fetch rx3-handoff tooling | Clones `abuzucom/Rx3-flx4` at the pinned commit and copies `rx3-handoff/` into `~/rx3`. |
| Acquire firmware archive | Downloads `XDJ-RX3_v120.zip` and verifies it against `firmware/firmware.sha256`. |
| Fetch recovery inputs | Runs `rx3_fetch.sh` to fetch proot, the armel toolchain, and the firmware recovery. |
| Create armel-gcc wrapper | Writes the cross-compiler wrapper the build expects. |
| Build chroot | Assembles `~/rx3/rootfs` and builds the shims. |
| Seed virtual USB stick | Creates `~/rx3/usb1` with test tones and a fake block device. |
| Sync USB1 music | If `RX3_USB1_SOURCE` or `--usb1-source` is set, rsyncs that folder into the stick. |
| Launch emulator | Starts the player and the bridge in a new console window. |

## Firmware details

Two separate firmware artifacts exist.

- The emulator runtime uses firmware 1.19. `recover-firmware.py` from the rx3-handoff tooling downloads the official update and the GPL source package. Both downloads carry pinned SHA-256 hashes inside that script. No Pioneer files enter this repository.
- The smoke test suite uses `XDJ-RX3_v120.zip`. `scripts/smoke_test.py` verifies it against `firmware/firmware.sha256`. Fetch it with `python scripts/smoke_test.py --action prepare --download`. Run the mock bridge test with `python scripts/smoke_test.py --action e2e`.

The rx3-handoff tooling comes from `abuzucom/Rx3-flx4` at commit
`c5659888aae4766d8e3fdce84f60ec09ceba0212`. The clone lands in
`~/rx3/Rx3-flx4` on the operator machine. That project carries no license
file. Fetch it for personal research use only.

## Manual setup

Run these steps inside WSL when the bootstrap is not an option:

```
mkdir -p ~/rx3
tar -C /mnt/c/path/to/repo/wsl -cf - . | tar -C ~/rx3 -xf -
git clone https://github.com/abuzucom/Rx3-flx4.git ~/rx3/Rx3-flx4
git -C ~/rx3/Rx3-flx4 checkout c5659888aae4766d8e3fdce84f60ec09ceba0212
mkdir -p ~/rx3/rx3-handoff
cp -a ~/rx3/Rx3-flx4/rx3-handoff/. ~/rx3/rx3-handoff/
cd ~/rx3 && bash rx3_fetch.sh
cat > ~/rx3/armel-gcc <<'EOF'
#!/bin/sh
exec "$(ls "$HOME/rx3/tc-armel/bin/"*-gcc | head -n 1)" "$@"
EOF
chmod +x ~/rx3/armel-gcc
bash build-rootfs-wsl.sh
bash make-usb.sh
bash run-rx3.sh
```

Replace `/mnt/c/path/to/repo` with the real repository location.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `RX3_NOBRIDGE` | unset | Set to `1` to run the player without the TCP bridge. |
| `RX3_TIMEOUT` | unset | Stop the player after N seconds. |
| `RX3_PORT` | `4480` | Bridge listen port. |
| `RX3_AUTOUSB` | unset | Set to announce USB1 to the firmware after boot. |
| `RX3_USB_DELAY` | `45` | Seconds before the automatic USB1 announcement. |
| `RX3_RUN_DIR` | `~/rx3` | Working directory for `wsl/test-e2e.sh`. |
| `RX3_BRIDGE_SRC` | `./rx3_bridge.py` | Bridge source copied by `wsl/test-e2e.sh`. |
| `RX3_USB_ROOT` | `/mnt/c/rx3_usb` | Windows-side root for virtual USB stick folders in the e2e test. |
| `RX3_USB1_SOURCE` | unset | Windows folder to sync into the virtual USB1 stick. Quote paths with spaces. |
| `RX3_USB1_ALLOWED_ROOT` | unset | Optional. If set, the source path must resolve under this folder. |
| `RX3_SHOT_DIR` | `.` | Screenshot output directory for the e2e test. |

## Virtual USB music folder

Set `RX3_USB1_SOURCE` to a Windows folder containing MP3, WAV, AIFF, or FLAC files. The path must use a drive letter (for example, `C:\Music`). The bootstrap rsyncs that folder into the virtual USB1 stick before launch. Quote the value if the path contains spaces.

Example with an environment variable:

```powershell
$env:RX3_USB1_SOURCE = "C:\Music\Techno"
.\rx3.cmd
```

Example with the command-line switch:

```powershell
.\rx3.cmd --usb1-source "C:\Music\Techno"
```

The sync is one-way: your folder is never modified. The firmware's own writes (track analysis, settings) stay inside the WSL copy. Add or remove tracks later by re-running `rx3.cmd`. Copy `.env.example` to `.env` and set `RX3_USB1_SOURCE` there to make the value persistent.

For extra control, set `RX3_USB1_ALLOWED_ROOT` to a parent folder. The source path must then resolve inside that folder. Confinement is enforced in WSL, where rsync runs.

## Connecting a controller

The optional controller client forwards MIDI input to the emulator bridge as firmware key commands.

Important: `controller.cmd` automatically installs missing MIDI dependencies.
The launcher contacts PyPI.
Pip verifies package artifacts against `requirements-controllers.txt` hashes.
Package installation can execute build code.

Install the optional dependencies:

```powershell
python -m pip install --require-hashes -r requirements-controllers.txt
```

Run the direct Python client after the emulator is bridge-ready:

```powershell
python -m controller_client.main --backend midi --midi-port "MIDI Controller"
```

The direct Python client runs headless by default. Add `--view` to show the live
firmware screen.

Double-click `controller.cmd` on Windows to open the live firmware screen while
the MIDI client runs. It checks for both MIDI packages. It installs them with
pip hash verification if either package is missing. Add `--headless` to start
the launcher without the screen window. The dependency check also verifies
Pillow 12.3.0 for antialiased screen resizing. Run the explicit pip command above
before using `controller.cmd` to control when installation occurs.

The screen window renders the bridge's 1280x800 RGBA tiles. The viewer and MIDI
client share one bridge connection. Use a Python installation with Tcl/Tk
support for visual mode. Pillow preserves thin text and lines when resizing
the screen. The controller requirements include its pinned version and hashes.

On Windows, the controller client plays bridge master audio through the default
multimedia output device. Windows applies the system and application mixer
levels. Playback works with `--headless` and visual mode. The bridge currently
does not send the cue capture to the client.

Use `--bridge-host` and `--bridge-port` to target a non-default bridge.
Place custom JSON profiles in `controllers/profiles/` before using `--profile`.
See `controllers/profiles/default.json` for the schema.

## Connecting a client

Connect to TCP 127.0.0.1:4480. Frames use the layout `[type u8][len u32 LE][payload]`. The README protocol table lists every frame type. Run `python scripts/smoke_test.py --action client` to verify a running bridge.

## Troubleshooting

| Symptom | Action |
|---|---|
| `WSL is not available` | Run `wsl --install` in an elevated PowerShell. Reboot. Install Ubuntu. |
| `Missing WSL packages` | Run the printed `apt-get install` line inside WSL. |
| `rx3-handoff clone failed` | Check network access to GitHub. |
| `rx3-handoff commit mismatch` | Delete `~/rx3/Rx3-flx4` inside WSL and re-run `rx3.cmd`. |
| `Firmware acquisition failed` | Check network access or place `XDJ-RX3_v120.zip` into `firmware/`. |
| Bridge port already in use | Set `RX3_PORT` before `run-rx3.sh`. |
| Boot seems stuck | Read `~/rx3/player.log` inside WSL. |
