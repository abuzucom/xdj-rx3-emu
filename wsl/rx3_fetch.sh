#!/usr/bin/env bash
# Fetch everything the RX3 emulation needs on WSL without root: proot, an armv7 hard-float toolchain,
# the official firmware + GPL sources (recover-firmware.py), and unpack the cramfs.
set -u
cd ~/rx3
# proot (static, no root needed for the chroot); SHA-256 pinned, re-download on mismatch
PROOT_URL=https://proot.gitlab.io/proot/bin/proot
PROOT_SHA256=90375de3807212b8f948ff98ed66020f7c9cf7ea447c8734f1af02a2643c8d26
verify_sha256() { echo "$2  $1" | sha256sum --check --status; }
[ -x proot ] && ! verify_sha256 proot "$PROOT_SHA256" && rm -f proot
[ -x proot ] || { curl -L --fail -sS -o proot "$PROOT_URL" && chmod +x proot; }
verify_sha256 proot "$PROOT_SHA256" || { echo "proot SHA-256 mismatch; refusing unverified binary"; exit 1; }
./proot --version 2>&1 | head -1
# bootlin armv7-eabihf (hard-float EABI) glibc toolchain; SHA-256 pinned, re-download on mismatch
TC_BASE=https://toolchains.bootlin.com/downloads/releases/toolchains/armv7-eabihf/tarballs
TC_NAME=armv7-eabihf--glibc--stable-2024.05-1.tar.xz
TC_SHA256=608263bc9dc3eadf0962ddb1165f1c2291001190f9927dee47d464e26374462c
if [ ! -d tc-armel ]; then
  [ -f tc-armel.tar.xz ] && ! verify_sha256 tc-armel.tar.xz "$TC_SHA256" && rm -f tc-armel.tar.xz
  [ -f tc-armel.tar.xz ] || { curl -L --fail -sS -o tc-armel.tar.xz "$TC_BASE/$TC_NAME" || { echo "toolchain download failed; check network access to toolchains.bootlin.com"; exit 1; }; }
  verify_sha256 tc-armel.tar.xz "$TC_SHA256" || { echo "toolchain SHA-256 mismatch; refusing unverified archive"; exit 1; }
  mkdir -p tc-armel && tar -xJf tc-armel.tar.xz -C tc-armel --strip-components=1 && ls tc-armel/bin | grep -m1 gcc
fi
cd ~/rx3/rx3-handoff
python3 recover-firmware.py 2>&1 | tail -5
python3 extract_cramfs.py 2>&1 | tail -3
ls extracted; ls extracted/runtime-files | head; du -sh extracted 2>/dev/null
echo RX3_FETCH_DONE
