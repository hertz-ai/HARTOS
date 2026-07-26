#!/bin/bash
set +e
WINLOCK="/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/Cargo.lock"
echo "===committed compositor/Cargo.lock present?==="
ls -la "$WINLOCK" 2>/dev/null
echo "===smithay/calloop/git entries in committed lock==="
grep -nE 'name = "smithay"|name = "calloop"|name = "pixman"|name = "drm"|name = "gbm"|source = "git' "$WINLOCK" 2>/dev/null | head -50
echo "===total [[package]] count==="
grep -c '^\[\[package\]\]' "$WINLOCK" 2>/dev/null
echo "===smithay stanza (full) in committed lock==="
awk '/^name = "smithay"/{p=1} p{print} /^$/{if(p)exit}' "$WINLOCK" 2>/dev/null
echo "===root hart-comp synced lock (if any)==="
ls -la /root/hart-comp/Cargo.lock 2>/dev/null
grep -nE 'name = "smithay"|source = "git' /root/hart-comp/Cargo.lock 2>/dev/null | head -20
echo "===git log compositor/==="
cd /mnt/c/Users/sathi/PycharmProjects/HARTOS && git log --oneline -18 -- compositor/ 2>/dev/null
