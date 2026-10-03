#!/usr/bin/env bash
# Fresh boot, wait for the control adapter, then: insert stick, play track A, measure pitch; swap stick; eject.
cd ~/rx3
BRIDGE_SRC="${RX3_BRIDGE_SRC:-./rx3_bridge.py}"
if [ -f "$BRIDGE_SRC" ] && [ "$BRIDGE_SRC" != "rx3_bridge.py" ]; then
  tr -d '\r' < "$BRIDGE_SRC" > rx3_bridge.py
fi
pkill -f rx3_bridge.py; pkill -f 'rbp-pi -a'; sleep 1
(RX3_TIMEOUT=600 ./run-rx3.sh > bridge.log 2>&1 &)
for i in $(seq 1 120); do grep -q 'RX3 control adapter ready' player.log 2>/dev/null && break; sleep 2; done
grep -q 'RX3 control adapter ready' player.log && echo "control adapter ready after ~$((i*2)) s" || { echo "control adapter never ready"; exit 1; }
sleep 3
python3 - <<'PY'
import os, socket, struct, time
from PIL import Image
s = socket.create_connection(('127.0.0.1', 4480), timeout=30)
def rd(n):
    b=b''
    while len(b)<n:
        d=s.recv(n-len(b))
        if not d: raise EOFError
        b+=d
    return b
def send(t,p): s.sendall(struct.pack('<BI',t,len(p))+p)
def key(k,op,ch=0,val=0,a=0.0): send(0x30,struct.pack('<iiiif',k,op,ch,val,a))
def tap(k,ch=0): key(k,0,ch); time.sleep(0.2); key(k,2,ch)
def touch(x,y,hold=0.25): send(0x31,struct.pack('<BHH',1,x,y)); time.sleep(hold); send(0x31,struct.pack('<BHH',0,x,y))
frame=bytearray(1280*800*4)
def pump(sec):
    end=time.time()+sec; s.settimeout(0.5)
    while time.time()<end:
        try: t,ln=struct.unpack('<BI',rd(5)); p=rd(ln)
        except socket.timeout: continue
        if t==0x11:
            x,y,w,h=struct.unpack_from('<HHHH',p)
            for r in range(h):
                o=((y+r)*1280+x)*4; frame[o:o+w*4]=p[8+r*w*4:8+(r+1)*w*4]
        elif t==0x01: print('STATUS',p.decode(errors='replace'))
def shot(n):
    out = os.environ.get('RX3_SHOT_DIR', '.')
    Image.frombuffer('RGBA',(1280,800),bytes(frame),'raw','RGBA',0,1).convert('RGB').save(os.path.join(out, f'f_{n}.png'))
base=os.environ.get('RX3_USB_ROOT', '/mnt/c/rx3_usb').encode()   # folder holding the virtual USB stick folders
send(0x21,b''); pump(3)
send(0x32, b'insert usb1 ' + base + br'\USB'); pump(8)
tap(0x201); pump(3); touch(400,144); pump(3); touch(400,124); pump(3); touch(908,624); pump(5)
tap(0x4101,1); pump(15); shot('playing')
print('--- swap: eject, insert Second')
send(0x32, b'eject usb1'); send(0x32, b'insert usb1 ' + base + br'\Second'); pump(14)
tap(0x201); pump(3); shot('source_after_swap'); touch(400,144); pump(3); shot('list_after_swap')
send(0x32, b'eject usb1'); pump(6); tap(0x201); pump(3); shot('source_after_eject')
PY
echo "--- audio device:"; grep -i 'audioDevice\|OVER_SAMPLING' player.log | head -3
echo "--- peaks:"; tail -2 rootfs/tmp/rx3-audio-peaks
python3 ~/rx3/pitch.py
grep -i 'usb' bridge.log | tail -5
