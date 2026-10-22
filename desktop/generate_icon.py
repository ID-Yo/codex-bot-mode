"""Create small deterministic Tauri PNG/ICO assets using only Python stdlib."""

import struct
import zlib
from pathlib import Path


def chunk(name, payload):
    body = name + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)


def main():
    size = 64
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            frame = 8 <= x < 56 and 9 <= y < 46
            screen = 12 <= x < 52 and 13 <= y < 42
            stand = 28 <= x < 36 and 46 <= y < 52
            base = 20 <= x < 44 and 52 <= y < 56
            if screen:
                color = (248, 250, 249, 255)
            elif frame or stand or base:
                color = (22, 116, 97, 255)
            else:
                color = (0, 0, 0, 0)
            row.extend(color)
        rows.append(bytes(row))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">2I5B", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
           + chunk(b"IEND", b""))
    icon_dir = Path(__file__).parent / "src-tauri" / "icons"
    icon_dir.mkdir(parents=True, exist_ok=True)
    (icon_dir / "icon.png").write_bytes(png)
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png), 22)
    (icon_dir / "icon.ico").write_bytes(struct.pack("<HHH", 0, 1, 1) + entry + png)


if __name__ == "__main__":
    main()
