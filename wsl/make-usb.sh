#!/usr/bin/env bash
# Build the USB1 media the XDJ-RX3 firmware browses: a directory bound into the chroot at /media/usb1/sda1,
# plus a small FAT image at /dev/sda1 so the firmware's libblkid probe sees a vfat volume, and a matching
# line in the fake /proc/mounts. Drop your own MP3/WAV/AIFF/FLAC files into ~/rx3/usb1/Music.
#   make-usb.sh            create the layout with two test tones if the folder is empty
#   make-usb.sh /path/*.mp3  also copy those files in
set -u
B=$HOME/rx3; R=$B/rootfs; U=$B/usb1
mkdir -p "$U/Music" "$R/media/usb1/sda1"
if [ $# -gt 0 ]; then cp -v "$@" "$U/Music/"; fi
if [ -z "$(ls -A "$U/Music")" ]; then
python3 - "$U/Music" <<'EOF'
import math, struct, sys, wave
out = sys.argv[1]
def tone(path, f, secs=60, bpm=128.0):
    rate = 44100
    w = wave.open(path, 'wb'); w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
    beat = rate * 60.0 / bpm
    frames = bytearray()
    for i in range(int(rate * secs)):
        env = 1.0 if (i % beat) < beat * 0.15 else 0.35     # a kick-like pulse on every beat so the grid analyser has something
        v = int(12000 * env * math.sin(2 * math.pi * f * i / rate))
        frames += struct.pack('<hh', v, v)
    w.writeframes(bytes(frames)); w.close()
tone(out + '/Test Tone A 440Hz 128bpm.wav', 440.0)
tone(out + '/Test Tone B 330Hz 128bpm.wav', 330.0, bpm=124.0)
print('wrote two test tones')
EOF
fi
# tiny FAT image for the blkid probe (contents irrelevant; the real files come from the bind mount)
if [ ! -f "$R/dev/sda1" ] || [ "$(stat -c %s "$R/dev/sda1")" -lt 1000000 ]; then
  rm -f "$B/usb1.img"; truncate -s 8M "$B/usb1.img"
  mformat -i "$B/usb1.img" -v USB1 :: 2>/dev/null || dd if=/dev/zero of="$B/usb1.img" bs=1M count=8 status=none
  cp -f "$B/usb1.img" "$R/dev/sda1"
fi
grep -q '/media/usb1/sda1' "$R/proc/mounts" || echo "/dev/sda1 /media/usb1/sda1 vfat rw,relatime,iocharset=utf8 0 0" >> "$R/proc/mounts"
ls -la "$U/Music"; echo USB_READY
