import os, struct, math, array
p = os.path.expanduser('~/rx3/rootfs/tmp/rx3-master.raw')
d = open(p, 'rb').read()
print('raw bytes', len(d))
a = array.array('h'); a.frombytes(d[:len(d) - len(d) % 4])
# find a loud 1-second window (the tone), mono mix
n = len(a) // 2
best = 0; bi = 0
step = 44100
for i in range(0, n - step, step):
    s = a[2 * i:2 * (i + step):2]
    m = max(abs(x) for x in s[::10])
    if m > best:
        best, bi = m, i
print('loudest second at', bi / 44100.0, 's peak', best)
seg = [ (a[2 * k] + a[2 * k + 1]) * 0.5 for k in range(bi, bi + 32768) ]
# zero crossings -> frequency assuming 44100
zc = sum(1 for k in range(1, len(seg)) if seg[k - 1] < 0 <= seg[k])
print('zero-crossing estimate (assuming 44.1k):', zc * 44100.0 / len(seg), 'Hz')
# DFT peak
N = 32768
mag = []
for f in range(300, 600, 1):
    re = im = 0.0
    for k in range(0, N, 4):
        ang = 2 * math.pi * f * k / 44100.0
        re += seg[k] * math.cos(ang); im -= seg[k] * math.sin(ang)
    mag.append((re * re + im * im, f))
print('DFT peak (assuming 44.1k):', max(mag)[1], 'Hz  -> real source rate if tone is 440 Hz:', 44100.0 * 440.0 / max(mag)[1])
