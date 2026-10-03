# xdj-rx3-emu

Run the Pioneer XDJ-RX3's real player firmware (the ARM32 rekordbox player application, v1.19) on a Windows PC
under WSL, without root, and expose it to any client program: the 1280x800 touch screen as a tile stream, the
master and cue audio as PCM, the deck's keys / jog / faders / knobs as the firmware's native control messages,
and two virtual USB sticks whose folders live on the Windows side.

**No Pioneer software is in this repository.** The firmware is downloaded from Pioneer's public update
server by the user and unpacked locally with the Rx3-flx4 project's `recover-firmware.py`; this repo only
holds the emulation harness, shims and bridge written for it.

```
 WSL (Ubuntu, no sudo)                                              Windows / client
  proot + qemu-arm (binfmt) chroot of the firmware rootfs
    rbp-pi (player)  --fb0 file--> rx3_bridge.py  --TCP 4480-->  screen tiles, audio, keys, touch, USB events
    shims: wsl-shim.c (RT sched, paced S16 audio capture), g2d-shim.c (software i.MX G2D blitter),
           patched fbshim (framebuffer file), asound-wsl.conf (ALSA plug over null)
```

## Bridge protocol (TCP 127.0.0.1:4480, frames `[type u8][len u32 LE][payload]`)

| dir | type | payload |
|-----|------|---------|
| bridge -> client | `0x01` | status text |
| | `0x10` | screen info: w, h (u16) |
| | `0x11` | screen tile: x, y, w, h (u16) + RGBA8888 pixels |
| | `0x14` | audio: rate u32, channels u8, S16LE interleaved |
| | `0x16` | engine state text (answer to `query`) |
| client -> bridge | `0x30` | key command: key i32, op i32, channel i32, value i32, analog f32 (firmware's control device) |
| | `0x31` | touch: down u8, x u16, y u16 (screen pixels; streamed while down) |
| | `0x32` | USB event text: `insert usb1 <windows path>` / `eject usb1` |
| | `0x21` | request a full frame |

Known key ids (from the firmware's control device): SOURCE `0x201`, browse rotary `0x420c`, LOAD `0x4311`
(channel = deck), PLAY `0x4101`, tempo op 5 analog -1..1, jog `0x4305` op 4 ticks (+ zero report).

## Setup (once)

1. `wsl/rx3_fetch.sh` - fetches proot, an armv7 toolchain, Pioneer's public XDJ-RX3 update and GPL source
   drop, and runs the Rx3-flx4 recovery to unpack the firmware (all into `~/rx3`).
2. `wsl/build-rootfs-wsl.sh` - assembles the chroot from the recovered firmware and builds the shims
   (`wsl-shim.c`, `g2d-shim.c`, `patch_fbshim.py`).
3. `wsl/make-usb.sh` - optional: seeds `~/rx3/usb1` with test tones.

## Run

```
wsl ~/rx3/run-rx3.sh          # boots the player (about a minute) and starts rx3_bridge.py on 4480
```
Environment: `RX3_NOBRIDGE=1` (player only), `RX3_TIMEOUT=N` (auto-stop after N s).
`wsl/test-e2e.sh` is an end-to-end check: boot, insert a stick, load and play a track, measure the output
pitch (`measure-pitch.py`), swap and eject sticks.

## Virtual USB sticks

A client sends `insert usb1 <folder>`; the bridge copies the audio files into the firmware's
`/media/usb1/sda1` (the firmware ignores symlinks; unchanged files are kept), creates the fake block device
node and `/proc/mounts` entry, and replays the firmware's own udev connect / mount events. A `PIONEER`
folder (rekordbox export) is passed through for library mode. `eject usb1` reverses it. Insert/eject are
queued with a 2 s settle so rapid swaps don't race the firmware.

## Screenshots

`docs/screenshots/` - source page, browse list, a loaded deck and playback, captured through the bridge.

## Notes

- Audio: the firmware outputs S24_LE 6 channels at 44.1 kHz; `wsl_capture` converts to S16 stereo master
  (`/tmp/rx3-master.raw`) and cue (`/tmp/rx3-cue.raw`) with real-time pacing, since the null ALSA device
  does not pace.
- The centre waveform is drawn by the firmware through the i.MX G2D; `g2d-shim.c` implements the calls in
  software on the framebuffer file.
- Jog scaling is approximate (40 ticks per revolution, DDJ-400 style).

## Development

`main` is the primary branch. `master` mirrors upstream and receives no other
commits. Merge `master` into `main` through a pull request.

See `docs/development.md` for commands, `CONTRIBUTING.md` for conventions, and
`AGENTS.md` for the agent policy. Pull requests run the Ruff baseline, the
agent policy checks, the foucault security review, and the euler quality review.

Handoffs use `plan/HANDOFF.md.example`. Inspect a handoff only after an
active-user request. Do not run Git commands before consent. After consent,
read Git state through `scripts/read_git_state.py`.

## Licence

This repository carries a split license.

Files inherited from the upstream fork stay under the MIT license in [`LICENSE`](LICENSE):
`LICENSE`, `README.md`, `.gitignore`, everything under `wsl/`, and everything under `docs/screenshots/`.
Later edits to those files keep them under MIT.

Every other file carries the BSD 3-Clause license in [`LICENSE.BSD-3-Clause`](LICENSE.BSD-3-Clause),
copyright ABUZUCOM LLC. That includes the material copied from `abuzucom/agents`, `abuzucom/rough`,
`abuzucom/foucault`, and `abuzucom/euler`, which keeps its BSD 3-Clause notice and conditions.

The firmware and Pioneer's GPL source drop are subject to their own licences and are not distributed.
