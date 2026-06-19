#!/usr/bin/env bash
# Read-only dependency probe for the M2 WebKit glass-shell host stack.
set -u
echo "=== GTK4 (libgtk-4-1 + gir) ==="
dpkg -l 2>/dev/null | grep -E "libgtk-4-1|gir1.2-gtk-4.0" || echo "  MISSING"
echo "=== WebKit-6.0 (lib + gir) ==="
dpkg -l 2>/dev/null | grep -E "libwebkitgtk-6.0|gir1.2-webkit-6.0" || echo "  MISSING"
echo "=== build tools (meson/ninja/gobject-introspection) ==="
dpkg -l 2>/dev/null | grep -E "^ii  (meson|ninja-build|libgirepository1.0-dev|gobject-introspection) " || echo "  (none of meson/ninja/gi-dev installed via apt)"
command -v meson && meson --version
command -v ninja && ninja --version
echo "=== libwayland-dev + valac ==="
dpkg -l 2>/dev/null | grep -E "libwayland-dev|valac" || echo "  MISSING"
echo "=== python3-gi (PyGObject) ==="
dpkg -l 2>/dev/null | grep -E "python3-gi " || echo "  MISSING python3-gi"
echo "=== existing gtk4-layer-shell artifacts ==="
ls -la /usr/local/lib/*/libgtk4-layer-shell.so* 2>/dev/null
ls -la /usr/local/lib/*/girepository-1.0/Gtk4LayerShell-1.0.typelib 2>/dev/null
ls -la /usr/lib/*/libgtk4-layer-shell.so* 2>/dev/null
test -f /usr/local/lib/x86_64-linux-gnu/girepository-1.0/Gtk4LayerShell-1.0.typelib && echo "  TYPELIB PRESENT" || echo "  typelib not yet built"
echo "=== GTK3 gtk-layer-shell (the WRONG one, just noting) ==="
dpkg -l 2>/dev/null | grep gtklayershell || echo "  (gtk3 layer-shell not installed)"
