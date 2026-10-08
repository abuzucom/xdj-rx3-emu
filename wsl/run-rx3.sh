#!/usr/bin/env bash
# Start the XDJ-RX3 player (firmware 1.19) under proot + qemu-arm on WSL, plus the game bridge.
#   RX3_NOBRIDGE=1  player only        RX3_TIMEOUT=N  stop after N seconds (smoke test)
set -u
if [ "${1:-}" != "--owned-session" ]; then
  exec python3 "$HOME/rx3/rx3_session.py"
fi
RX3_PORT_VALUE="$RX3_PORT"
B=$HOME/rx3; R=$B/rootfs; LOG=$B/player.log
[ -x $R/root/pdj/rbp-pi ] || { echo "rootfs not built: run build-rootfs-wsl.sh"; exit 1; }
rm -f $R/tmp/rx3-master.raw $R/tmp/rx3-cue.raw $R/tmp/rx3-audio-peaks
echo "1.19" > $R/tmp/smdj.rev; echo "1.19 [1.19:1.19]" > $R/tmp/smdj2.rev
# a zeroed frame so the bridge never streams stale pixels from the last run
python3 -c "open('$R/dev/fb0','r+b').write(bytes(1280*800*4))"
cd $B
mkdir -p $B/usb1 $B/usb2; USBBIND="-b $B/usb1:/media/usb1/sda1 -b $B/usb2:/media/usb2/sdb1"
PROOT="$B/proot -r $R -w /root/pdj -q qemu-arm -b /dev/null -b /dev/zero -b /dev/urandom -b /dev/random -b /dev/full -b $R/dev/printkdrv0:/dev/printkdrv0 $USBBIND"
${RX3_TIMEOUT:+timeout ${RX3_TIMEOUT}} $PROOT /bin/busybox sh -c "cd /root/pdj && exec env LD_PRELOAD=/lib/fbshim.so /root/pdj/rbp-pi -a" > "$LOG" 2>&1 < /dev/null &
PLAYER_PID=$!
echo "player started; log: $LOG"
# announce USB1 to the firmware once it is up (udev FIFO protocol: connect, then mount <path>)
if [ -n "${RX3_AUTOUSB:-}" ]; then ( sleep ${RX3_USB_DELAY:-45}; python3 - "$R" <<'PY'
import os, sys, time
r = sys.argv[1]
def fifo(name, msg):
    try:
        f = os.open(r + '/proc/' + name, os.O_RDWR | os.O_NONBLOCK); os.write(f, msg.encode()); os.close(f)
    except OSError as e:
        print('usb fifo', name, e)
fifo('udev_usbctn1', 'connect'); time.sleep(1.5); fifo('udev_usb1', 'mount /media/usb1/sda1'); print('USB1 announced')
PY
) > $B/usb.log 2>&1 & fi
if [ -z "${RX3_NOBRIDGE:-}" ]; then
  python3 "$B/rx3_bridge.py" --root "$R" --port "$RX3_PORT_VALUE" &
  BRIDGE_PID=$!
  wait -n "$PLAYER_PID" "$BRIDGE_PID"
else
  wait "$PLAYER_PID"
fi
