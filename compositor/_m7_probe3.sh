#!/bin/bash
set +e
W="/mnt/c/Users/sathi/PycharmProjects/HARTOS/compositor/src/wayland.rs"
echo "===wayland.rs impls + delegate macros + summon/xwayland fns==="
grep -nE "^impl |delegate_|fn on_real_map|fn expire_summons|fn sync_foreign_toplevels|fn handle_xwayland|fn .*xwayland|CompositorHandler|BufferHandler|ShmHandler|SeatHandler|XwmHandler|XdgShellHandler|XdgDecorationHandler|ForeignToplevelListHandler|XWaylandShellHandler|WlrLayerShellHandler|DndGrabHandler|OutputHandler|SelectionHandler|DataDeviceHandler" "$W"
echo ""
echo "===State fields in wayland.rs (what run_udev must construct)==="
awk '/pub struct State \{/{p=1} p{print} /^\}/{if(p){exit}}' "$W"
