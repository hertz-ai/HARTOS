#!/usr/bin/env python3
"""Generate hart-logo.png for Plymouth boot splash.

Run during ISO build (in chroot hook) or manually:
    python3 generate-logo.py

Outputs: hart-logo.png (200x200, dark background with teal H hexagon)
Requires: Pillow (already in requirements.txt)
"""

import os
import math

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    # Fallback: create a minimal 200x200 teal circle PNG manually (no Pillow)
    # This is a valid 200x200 PNG with a teal circle — generated as raw bytes
    print("Pillow not available, creating minimal placeholder logo")
    # Create a simple solid-color PNG using pure Python
    import struct
    import zlib

    width, height = 200, 200

    def create_png(w, h, r, g, b):
        """Create minimal PNG with solid circle."""
        rows = []
        cx, cy, radius = w // 2, h // 2, w // 2 - 10
        for y in range(h):
            row = b'\x00'  # filter byte
            for x in range(w):
                dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
                if dist <= radius:
                    row += bytes([r, g, b, 255])
                else:
                    row += bytes([0, 0, 0, 0])
            rows.append(row)
        raw = b''.join(rows)
        compressed = zlib.compress(raw)

        # PNG file structure
        sig = b'\x89PNG\r\n\x1a\n'

        def chunk(chunk_type, data):
            c = chunk_type + data
            return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

        ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)  # 8bit RGBA
        return sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')

    png_data = create_png(width, height, 78, 205, 196)
    out_path = os.path.join(os.path.dirname(__file__), 'hart-logo.png')
    with open(out_path, 'wb') as f:
        f.write(png_data)
    print(f"Created {out_path}")
    raise SystemExit(0)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(SCRIPT_DIR, 'hart-logo.png')

# 200x200 transparent PNG
SIZE = 200
img = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Teal hexagon background
TEAL = (78, 205, 196, 255)
cx, cy = SIZE // 2, SIZE // 2
radius = 85
points = []
for i in range(6):
    angle = math.radians(60 * i - 30)
    points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
draw.polygon(points, fill=TEAL)

# White "H" in center
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
except (OSError, IOError):
    try:
        font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", 80)
    except (OSError, IOError):
        font = ImageFont.load_default()

bbox = draw.textbbox((0, 0), "H", font=font)
tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
draw.text((cx - tw // 2, cy - th // 2 - 5), "H", fill=(255, 255, 255, 255), font=font)

img.save(OUTPUT, 'PNG')
print(f"Generated: {OUTPUT} ({SIZE}x{SIZE})")
