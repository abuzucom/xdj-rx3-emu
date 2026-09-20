#!/usr/bin/env bash
# Fetch everything the RX3 emulation needs on WSL without root: proot, an armv7 softfloat toolchain,
# the official firmware + GPL sources (recover-firmware.py), and unpack the cramfs.
set -u
cd ~/rx3
which clang && clang --version | head -1
# proot (static, no root needed for the chroot)
[ -x proot ] || { curl -L --fail -sS -o proot https://proot.gitlab.io/proot/bin/proot && chmod +x proot; }
./proot --version 2>&1 | head -1
# bootlin armv7-eabi (softfloat EABI, matches arm-linux-gnueabi) glibc toolchain
if [ ! -d tc-armel ]; then
  curl -L --fail -sS -o tc-armel.tar.xz "https://toolchains.bootlin.com/downloads/releases/toolchains/armv7-eabi/tarballs/armv7-eabi--glibc--stable-2024.05-1.tar.xz" || echo "bootlin download failed"
  mkdir -p tc-armel && tar -xJf tc-armel.tar.xz -C tc-armel --strip-components=1 && ls tc-armel/bin | grep -m1 gcc
fi
cd ~/rx3/rx3-handoff
python3 recover-firmware.py 2>&1 | tail -5
python3 extract_cramfs.py 2>&1 | tail -3
ls extracted; ls extracted/runtime-files | head; du -sh extracted 2>/dev/null
echo RX3_FETCH_DONE
