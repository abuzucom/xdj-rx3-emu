#!/usr/bin/env python3
"""Add virtual USB stick handling to rx3_bridge.py:
  0x32 "insert usbN <host path>"  -> populate ~/rx3/usbN with links to the folder's audio files, then connect+mount
  0x32 "eject usbN"               -> umount+disconnect, clear the folder
Windows paths (C:\\Users\\...) are translated to /mnt/c/Users/... (WSL drvfs).
"""

import pathlib

p = pathlib.Path(__file__).with_name("rx3_bridge.py")
s = p.read_text()
old = """                elif typ == 0x32:
                    line = payload.decode(errors='replace').strip()
                    parts = line.split(' ', 2)
                    if len(parts) >= 2 and parts[0] in ('mount', 'umount', 'connect', 'disconnect'):"""
new = """                elif typ == 0x32:
                    line = payload.decode(errors='replace').strip()
                    parts = line.split(' ', 2)
                    if len(parts) >= 2 and parts[0] in ('insert', 'eject'):
                        threading.Thread(target=usb_stick, args=(root, parts[0], parts[1], parts[2] if len(parts) > 2 else '', client), daemon=True).start()
                    elif len(parts) >= 2 and parts[0] in ('mount', 'umount', 'connect', 'disconnect'):"""
assert old in s
s = s.replace(old, new, 1)
helper = '''

AUDIO_EXT = ('.mp3', '.wav', '.aiff', '.aif', '.flac', '.m4a', '.aac', '.ogg')


def host_path(p):
    """Windows path from the game -> WSL path."""
    p = p.strip().strip('"')
    if len(p) >= 2 and p[1] == ':':
        return '/mnt/' + p[0].lower() + p[2:].replace('\\\\', '/')
    return p.replace('\\\\', '/')


def usb_stick(root, action, slot, folder, client):
    """Present a host folder to the firmware as USB1/USB2 through the bound ~/rx3/usbN directory."""
    n = slot[-1]
    if n not in ('1', '2'):
        return
    base = os.path.expanduser('~/rx3/usb' + n)
    dev = 'sd' + ('a' if n == '1' else 'b') + '1'
    mounts = root + '/proc/mounts'
    mount_line = '/dev/%s /media/usb%s/%s vfat rw,relatime,iocharset=utf8 0 0' % (dev, n, dev)
    path = '/media/usb%s/%s' % (n, dev)
    def fifo(name, msg):
        Fifo(root + '/proc/' + name).write(msg.encode())
    def set_mount(present):
        try:
            lines = [l for l in open(mounts).read().splitlines() if '/media/usb%s/' % n not in l]
        except OSError:
            lines = []
        if present:
            lines.append(mount_line)
        open(mounts, 'w').write('\\n'.join(lines) + '\\n')
    def clear():
        os.makedirs(base, exist_ok=True)
        for e in os.listdir(base):
            fp = os.path.join(base, e)
            try:
                if os.path.islink(fp) or os.path.isfile(fp):
                    os.unlink(fp)
                else:
                    import shutil
                    shutil.rmtree(fp)
            except OSError:
                pass
    if action == 'eject':
        fifo('udev_usb' + n, 'umount ' + path)
        time.sleep(1.0)
        fifo('udev_usbctn' + n, 'disconnect')
        time.sleep(0.5)
        set_mount(False)
        clear()
        client.status('usb%s ejected' % n)
        return
    src = host_path(folder)
    if not os.path.isdir(src):
        client.status('usb%s: folder not found: %s' % (n, src))
        return
    clear()
    count = 0
    music = os.path.join(base, 'Contents')
    os.makedirs(music, exist_ok=True)
    for dirpath, dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        dst = music if rel == '.' else os.path.join(music, rel)
        os.makedirs(dst, exist_ok=True)
        for f in files:
            if f.lower().endswith(AUDIO_EXT):
                try:
                    os.symlink(os.path.join(dirpath, f), os.path.join(dst, f))
                    count += 1
                except OSError:
                    pass
    # rekordbox export on the stick (PIONEER folder) is passed through when the folder carries one
    pio = os.path.join(src, 'PIONEER')
    if os.path.isdir(pio):
        try:
            os.symlink(pio, os.path.join(base, 'PIONEER'))
        except OSError:
            pass
    devnode = root + '/dev/' + dev
    if not os.path.exists(devnode):
        img = os.path.expanduser('~/rx3/usb1.img')
        if os.path.exists(img):
            import shutil
            shutil.copyfile(img, devnode)
    set_mount(True)
    fifo('udev_usbctn' + n, 'connect')
    time.sleep(1.5)
    fifo('udev_usb' + n, 'mount ' + path)
    client.status('usb%s inserted: %d audio files from %s' % (n, count, src))
'''
anchor = "\n\ndef serve(port, root, client):"
assert anchor in s
s = s.replace(anchor, helper + anchor, 1)
p.write_text(s)
print("bridge usb patch applied")
