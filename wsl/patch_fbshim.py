#!/usr/bin/env python3
"""Derive fbshim-wsl.c from the Pi project's fbshim.c: force stereo when the (absent) card reports 0 channels,
and route every output write through wsl_capture() for real-time pacing and raw capture.
"""

import pathlib
import sys

src = pathlib.Path(sys.argv[1]).read_text()
dst = pathlib.Path(sys.argv[2])
a = "int snd_pcm_hw_params_set_channels(void*a,void*b,unsigned c){REAL(snd_pcm_hw_params_set_channels,int,(void*,void*,unsigned));"
assert a in src, "set_channels anchor"
src = src.replace(a, a + "if(c==0)c=2;", 1)
b = " void *r=o?o->real:pcm;\n ulk();\n long v=real(r,buf,frames);"
assert b in src, "writei anchor"
src = src.replace(
    b,
    " void *r=o?o->real:pcm;\n ulk();\n if(o)wsl_capture(o->target,buf,frames,o->channels,o->format,o->rate);\n long v=real(r,buf,frames);",
    1,
)
src = "extern void wsl_capture(const char*,const void*,unsigned long,unsigned,int,unsigned);\n" + src
dst.write_text(src)
print("fbshim-wsl.c written")
