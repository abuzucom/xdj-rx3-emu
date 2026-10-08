#!/usr/bin/env python3
"""rx3_bridge.py - relay between the XDJ-RX3 firmware running in the proot chroot and a client application (screen, touch, keys, audio, USB).

Same wire format as cdj3k_bridge.py: [type u8][len u32 LE][payload]
  bridge -> game  0x01 status text     0x10 screen info (w,h u16)   0x11 screen tile (x,y,w,h u16 + RGBA8888)
                  0x14 audio (rate u32, ch u8, s16le interleaved)   0x16 engine state text (from `query`)
                  0x17 firmware LEDs (complete RXL1 snapshot)
  game -> bridge  0x30 key command  (key i32, op i32, channel i32, value i32, analog f32)   -> /dev/rx3-control
                  0x31 touch        (down u8, x u16, y u16 in 1280x800 screen pixels)       -> /dev/tsc2007_2-0048
                  0x32 usb event text ("mount usb1 </media/usb1/sda1>" ...)                  -> /proc/udev_* FIFOs
                  0x21 request full frame
The screen is the chroot's /dev/fb0 (regular file, 1280x800, B,G,R,X). Audio is /tmp/rx3-audio.raw in the chroot
(S16_LE 44.1 kHz stereo master; the cue mix is /tmp/rx3-cue.raw), written by the ALSA file plugin.
"""

import argparse
import os
import socket
import struct
import threading
import time
import pathlib
import logging

W, H = 1280, 800
TILE = 64
# Paths inside the chroot, relative to --root. Not the host's /tmp.
CHROOT_FB = "dev/fb0"
CHROOT_MASTER_AUDIO = "tmp/rx3-master.raw"


class Client:
    def __init__(self):
        self.lock = threading.Lock()
        self.sock = None
        self.want_full = True

    def set(self, sock):
        with self.lock:
            old = self.sock
            self.sock = sock
            self.want_full = True
        if old:
            try:
                old.close()
            except OSError:
                pass

    def send(self, typ, payload):
        with self.lock:
            s = self.sock
            if s is None:
                return False
            try:
                s.sendall(struct.pack("<BI", typ, len(payload)) + payload)
                return True
            except OSError:
                self.sock = None
                return False

    def status(self, text):
        self.send(0x01, text.encode())


def fb_thread(path, client):
    """Poll the framebuffer file, send changed 64x64 tiles as RGBA."""
    last = bytearray(W * H * 4)
    have = False
    while True:
        try:
            with pathlib.Path(path).open("rb") as f:
                cur = f.read(W * H * 4)
        except OSError:
            time.sleep(1.0)
            continue
        if len(cur) < W * H * 4:
            time.sleep(0.5)
            continue
        with client.lock:
            full = client.want_full and client.sock is not None
            if full:
                client.want_full = False
        if full:
            client.send(0x10, struct.pack("<HH", W, H))
        sent = 0
        for ty in range(0, H, TILE):
            th = min(TILE, H - ty)
            for tx in range(0, W, TILE):
                tw = min(TILE, W - tx)
                changed = full or not have
                if not changed:
                    for r in range(th):
                        o = ((ty + r) * W + tx) * 4
                        if cur[o : o + tw * 4] != last[o : o + tw * 4]:
                            changed = True
                            break
                if not changed:
                    continue
                rows = bytearray()
                for r in range(th):
                    o = ((ty + r) * W + tx) * 4
                    px = cur[o : o + tw * 4]
                    # B,G,R,X -> R,G,B,A
                    rgba = bytearray(len(px))
                    rgba[0::4] = px[2::4]
                    rgba[1::4] = px[1::4]
                    rgba[2::4] = px[0::4]
                    rgba[3::4] = b"\xff" * (len(px) // 4)
                    rows += rgba
                client.send(0x11, struct.pack("<HHHH", tx, ty, tw, th) + bytes(rows))
                sent += 1
        last = bytearray(cur)
        have = True
        time.sleep(0.033 if sent else 0.05)


def audio_thread(path, client):
    rate, ch = 44100, 2
    pos = None
    while True:
        try:
            size = pathlib.Path(path).stat().st_size
        except OSError:
            pos = None
            time.sleep(0.5)
            continue
        if pos is None or size < pos:
            pos = size - size % (2 * ch)
            client.status("audio: tailing %s" % path)
        if size - pos >= 2 * ch * 256:
            with pathlib.Path(path).open("rb") as f:
                f.seek(pos)
                d = f.read(min(size - pos, 2 * ch * rate // 25))
            d = d[: len(d) - len(d) % (2 * ch)]
            pos += len(d)
            client.send(0x14, struct.pack("<IB", rate, ch) + d)
        else:
            time.sleep(0.005)


class Fifo:
    def __init__(self, path):
        self.path = path
        self.fd = None
        self.lock = threading.Lock()

    def write(self, data):
        with self.lock:
            try:
                if self.fd is None:
                    self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
                os.write(self.fd, data)
                return True
            except OSError:
                try:
                    if self.fd is not None:
                        os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
                return False


AUDIO_EXT = (".mp3", ".wav", ".aiff", ".aif", ".flac", ".m4a", ".aac", ".ogg")


def host_path(p):
    """Windows path from the game -> WSL path."""
    p = p.strip().strip('"')
    if len(p) >= 2 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")
    return p.replace("\\", "/")


def usb_stick(root, action, slot, folder, client):
    """Present a host folder to the firmware as USB1/USB2 through the bound ~/rx3/usbN directory."""
    n = slot[-1]
    if n not in ("1", "2"):
        return
    base = os.path.expanduser("~/rx3/usb" + n)
    dev = "sd" + ("a" if n == "1" else "b") + "1"
    mounts = root + "/proc/mounts"
    mount_line = "/dev/%s /media/usb%s/%s vfat rw,relatime,iocharset=utf8 0 0" % (dev, n, dev)
    path = "/media/usb%s/%s" % (n, dev)

    def fifo(name, msg):
        Fifo(root + "/proc/" + name).write(msg.encode())

    def set_mount(present):
        try:
            lines = [l for l in pathlib.Path(mounts).read_text().splitlines() if "/media/usb%s/" % n not in l]
        except OSError:
            lines = []
        if present:
            lines.append(mount_line)
        pathlib.Path(mounts).write_text("\n".join(lines) + "\n")

    def clear():
        pathlib.Path(base).mkdir(exist_ok=True, parents=True)
        for e in os.listdir(base):
            fp = os.path.join(base, e)
            try:
                if pathlib.Path(fp).is_symlink() or pathlib.Path(fp).is_file():
                    pathlib.Path(fp).unlink()
                else:
                    import shutil

                    shutil.rmtree(fp)
            except OSError:
                pass

    if action == "eject":
        fifo("udev_usb" + n, "umount " + path)
        time.sleep(1.0)
        fifo("udev_usbctn" + n, "disconnect")
        time.sleep(0.5)
        set_mount(False)
        client.status("usb%s ejected" % n)
        return
    src = host_path(folder)
    if not pathlib.Path(src).is_dir():
        client.status("usb%s: folder not found: %s" % (n, src))
        return
    # The firmware ignores symlinks, so the stick is a synced copy: new / changed files are copied,
    # files that vanished from the source are removed, unchanged ones are kept (fast re-insert).
    import shutil

    pathlib.Path(base).mkdir(exist_ok=True, parents=True)
    wanted = set()
    count = 0
    client.status("usb%s: syncing files ..." % n)
    for dirpath, dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        if rel.split(os.sep)[0] == "PIONEER":
            keep = files
        else:
            keep = [f for f in files if f.lower().endswith(AUDIO_EXT)]
        if not keep and rel != ".":
            continue
        dst = base if rel == "." else os.path.join(base, rel)
        pathlib.Path(dst).mkdir(exist_ok=True, parents=True)
        wanted.add(os.path.normpath(dst))
        for f in keep:
            sp, dp = os.path.join(dirpath, f), os.path.join(dst, f)
            wanted.add(os.path.normpath(dp))
            try:
                st = os.stat(sp)
                if (
                    not pathlib.Path(dp).exists()
                    or pathlib.Path(dp).stat().st_size != st.st_size
                    or abs(pathlib.Path(dp).stat().st_mtime - st.st_mtime) > 2
                ):
                    shutil.copy2(sp, dp)
                count += 1
            except OSError as e:
                client.status("usb%s: copy failed %s: %s" % (n, f, e))
    for dirpath, dirs, files in os.walk(base, topdown=False):
        for f in files:
            fp = os.path.join(dirpath, f)
            if os.path.normpath(fp) not in wanted:
                try:
                    pathlib.Path(fp).unlink()
                except OSError:
                    pass
        if os.path.normpath(dirpath) != os.path.normpath(base) and os.path.normpath(dirpath) not in wanted:
            try:
                pathlib.Path(dirpath).rmdir()
            except OSError:
                pass
    devnode = root + "/dev/" + dev
    if not pathlib.Path(devnode).exists():
        img = os.path.expanduser("~/rx3/usb1.img")
        if pathlib.Path(img).exists():
            import shutil

            shutil.copyfile(img, devnode)
    set_mount(True)
    fifo("udev_usbctn" + n, "connect")
    time.sleep(1.5)
    fifo("udev_usb" + n, "mount " + path)
    client.status("usb%s inserted: %d audio files from %s" % (n, count, src))


class TouchStream:
    """The firmware's tsc2007 driver expects a stream of samples while a finger is down (the Pi touch
    bridge writes one report per 10 ms loop) and a few release reports afterwards.
    """

    def __init__(self, fifo):
        self.fifo = fifo
        self.lock = threading.Lock()
        self.down = False
        self.x = 0
        self.y = 0
        self.release_left = 0
        threading.Thread(target=self.run, daemon=True).start()

    def set(self, down, x, y):
        with self.lock:
            if self.down and not down:
                self.release_left = 10
            self.down, self.x, self.y = down, x, y

    def run(self):
        while True:
            time.sleep(0.01)
            with self.lock:
                down, x, y, rel = self.down, self.x, self.y, self.release_left
                if rel:
                    self.release_left -= 1
            if not down and not rel:
                continue
            # tsc2007 raw coordinates, X mirrored (from the Pi project's touch bridge)
            rx = 37 + (W - x) * 3976 // W
            ry = 72 + y * 3856 // H
            self.fifo.write(struct.pack("<BBHH", 1 if down else 0, 0, rx, ry))


TOUCH = None
import queue

USB_QUEUE = queue.Queue()


def usb_worker():
    """USB plug/unplug events run strictly one after another, with a settle time between them, so an eject
    followed by an insert (a different stick in the same slot) reaches the firmware in order.
    """
    while True:
        root, action, slot, folder, client = USB_QUEUE.get()
        try:
            usb_stick(root, action, slot, folder, client)
        except Exception as e:
            # Keep the worker alive for the next event. Log the traceback, since any error can land here.
            logging.exception("usb %s %s failed", action, slot)
            client.status("usb error: %r" % (e,))
        time.sleep(2.0)


threading.Thread(target=usb_worker, daemon=True).start()


def serve(port, root, client):
    global TOUCH
    control = Fifo(root + "/dev/rx3-control")
    touch = Fifo(root + "/dev/tsc2007_2-0048")
    TOUCH = TouchStream(touch)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # "" is INADDR_ANY, the same as "0.0.0.0": listen on every interface.
    srv.bind(("", port))
    srv.listen(1)
    print("rx3 bridge listening on %d" % port, flush=True)
    while True:
        s, addr = srv.accept()
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print("game connected from", addr, flush=True)
        client.set(s)
        client.status("bridge connected")
        try:
            while True:
                hdr = b""
                while len(hdr) < 5:
                    d = s.recv(5 - len(hdr))
                    if not d:
                        raise OSError("client EOF")
                    hdr += d
                typ, ln = struct.unpack("<BI", hdr)
                payload = b""
                while len(payload) < ln:
                    d = s.recv(min(65536, ln - len(payload)))
                    if not d:
                        raise OSError("client EOF")
                    payload += d
                if typ == 0x30 and len(payload) >= 20:
                    key, op, ch, val, analog = struct.unpack("<iiiif", payload[:20])
                    control.write(struct.pack("<iiiifi", key, op, ch, val, analog, 0))
                elif typ == 0x31 and len(payload) >= 5:
                    down, x, y = struct.unpack("<BHH", payload[:5])
                    TOUCH.set(bool(down), max(0, min(W - 1, x)), max(0, min(H - 1, y)))
                elif typ == 0x32:
                    line = payload.decode(errors="replace").strip()
                    parts = line.split(" ", 2)
                    if len(parts) >= 2 and parts[0] in ("insert", "eject"):
                        USB_QUEUE.put((root, parts[0], parts[1], parts[2] if len(parts) > 2 else "", client))
                    elif len(parts) >= 2 and parts[0] in ("mount", "umount", "connect", "disconnect"):
                        slot = parts[1][-1]
                        if parts[0] in ("connect", "disconnect"):
                            Fifo(root + "/proc/udev_usbctn" + slot).write(parts[0].encode())
                        else:
                            path = parts[2] if len(parts) > 2 else "/media/%s/sda1" % parts[1]
                            Fifo(root + "/proc/udev_" + parts[1]).write(("%s %s" % (parts[0], path)).encode())
                elif typ == 0x21:
                    with client.lock:
                        client.want_full = True
        except OSError as e:
            print("game disconnected:", e, flush=True)
            with client.lock:
                if client.sock is s:
                    client.sock = None
            try:
                s.close()
            except OSError:
                pass


def main():
    from led_relay import LedRelay

    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/rx3/rootfs"))
    ap.add_argument("--port", type=int, default=4480)
    a = ap.parse_args()
    client = Client()
    threading.Thread(target=fb_thread, args=(os.path.join(a.root, CHROOT_FB), client), daemon=True).start()
    threading.Thread(target=audio_thread, args=(os.path.join(a.root, CHROOT_MASTER_AUDIO), client), daemon=True).start()
    stopped = threading.Event()
    relay = LedRelay(os.path.join(a.root, "tmp/rx3-leds"), client)
    led_worker = threading.Thread(target=relay.run, args=(stopped,))
    led_worker.start()
    try:
        serve(a.port, a.root, client)
    finally:
        stopped.set()
        led_worker.join()


if __name__ == "__main__":
    main()
