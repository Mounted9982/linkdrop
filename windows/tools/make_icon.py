from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


SIZE = 1024
OUTPUT_SIZES = (16, 24, 32, 48, 64, 128, 256)


def inside_rounded_square(x: int, y: int, left: int, top: int, right: int, bottom: int, radius: int) -> bool:
    if left + radius <= x <= right - radius or top + radius <= y <= bottom - radius:
        return left <= x <= right and top <= y <= bottom
    center_x = left + radius if x < left + radius else right - radius
    center_y = top + radius if y < top + radius else bottom - radius
    return (x - center_x) ** 2 + (y - center_y) ** 2 <= radius**2


def arrow_mask(x: int, y: int) -> bool:
    scale = SIZE / 256
    x /= scale
    y /= scale
    # Right arrow.
    if 76 <= y <= 96 and 54 <= x <= 188:
        return True
    if 170 <= x <= 222 and abs(y - 86) <= (222 - x) * 0.62:
        return True
    # Left arrow.
    if 160 <= y <= 180 and 68 <= x <= 202:
        return True
    if 34 <= x <= 86 and abs(y - 170) <= (x - 34) * 0.62:
        return True
    return False


def rgba_bytes() -> bytes:
    rows = []
    for y in range(SIZE):
        row = bytearray([0])
        for x in range(SIZE):
            if not inside_rounded_square(x, y, 54, 54, 970, 970, 238):
                row.extend((0, 0, 0, 0))
                continue
            if arrow_mask(x, y):
                row.extend((255, 255, 255, 255))
                continue
            ratio = (x + y) / (SIZE * 2)
            red = int(37 + 19 * ratio)
            green = int(99 + 45 * ratio)
            blue = int(235 + 15 * ratio)
            row.extend((red, green, blue, 255))
        rows.append(bytes(row))
    return b"".join(rows)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def resize_rgba(source: bytes, source_size: int, target_size: int) -> bytes:
    source_stride = source_size * 4 + 1
    output = bytearray()
    ratio = source_size // target_size
    for target_y in range(target_size):
        for target_x in range(target_size):
            total = [0, 0, 0, 0]
            for offset_y in range(ratio):
                source_y = target_y * ratio + offset_y
                row_start = source_y * source_stride + 1
                for offset_x in range(ratio):
                    source_x = target_x * ratio + offset_x
                    index = row_start + source_x * 4
                    for channel in range(4):
                        total[channel] += source[index + channel]
            count = ratio * ratio
            output.extend(channel // count for channel in total)
    return bytes(output)


def encode_png(size: int, pixels: bytes) -> bytes:
    stride = size * 4
    rows = [b"\x00" + pixels[y * stride : (y + 1) * stride] for y in range(size)]
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return header + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + png_chunk(b"IEND", b"")


def main() -> None:
    target = Path(__file__).resolve().parent.parent / "linkdrop" / "assets"
    target.mkdir(parents=True, exist_ok=True)
    source = rgba_bytes()
    images = {size: encode_png(size, resize_rgba(source, SIZE, size)) for size in OUTPUT_SIZES}
    icon_header = struct.pack("<HHH", 0, 1, len(OUTPUT_SIZES))
    entries = []
    payloads = []
    offset = 6 + 16 * len(OUTPUT_SIZES)
    for size in OUTPUT_SIZES:
        data = images[size]
        dimension = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset))
        payloads.append(data)
        offset += len(data)
    (target / "app.ico").write_bytes(icon_header + b"".join(entries) + b"".join(payloads))
    (target / "app.png").write_bytes(images[256])
    print(target / "app.ico")


if __name__ == "__main__":
    main()
