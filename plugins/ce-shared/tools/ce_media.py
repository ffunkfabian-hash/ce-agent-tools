#!/usr/bin/env python3
"""ce_media.py sheet|clip|still - the one way to make the evidence the rules ask for (› Belege), same sizes everywhere.

    ce_media.py sheet  <frames_dir_or_pngs...> --out sheet.png [--columns 6] [--width 1280] [--label]
    ce_media.py clip   <frames_dir> --out clip.mp4 [--fps 30] [--height 480] [--seconds 8]
    ce_media.py still  <image.png> --out still.png [--width 1280]

sheet: contact sheet (ImageMagick `montage` when present, else Pillow), at most --width px wide, file names as labels (--label).
clip: H.264 MP4 from PNG frames (ffmpeg, yuv420p, crf 23), scaled to --height, cut to --seconds (rule: 480p, <= 8 s, one clip per change).
still: one downscaled picture (<= 1280 px wide) for a visible change without motion.
Prints the output path and size; exit 2 when a tool is missing. Frames are taken sorted by name.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = os.name == "nt"  # (0.31)


def frames_of(sources) -> list[Path]:
    files = []
    for source in sources:
        path = Path(source)
        if path.is_dir():
            files += sorted(p for p in path.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        elif path.is_file():
            files.append(path)
    return files


def sheet_tile_size(width: int, columns: int) -> int:
    """Largest square tile that fits the requested width including gutters."""
    if columns <= 0:
        raise ValueError("--columns muss positiv sein")
    tile = (width - 4 * columns) // columns
    if tile < 1:
        raise ValueError("--width muss mindestens 5 Pixel je Spalte enthalten")
    return tile


def sheet(sources, out: Path, columns: int, width: int, label: bool) -> int:
    files = frames_of(sources)
    if not files:
        print("ce_media sheet: keine Bilder")
        return 2
    step = max(1, len(files) // 36)   # a sheet holds at most 36 frames
    files = files[::step][:36]
    try:
        tile = sheet_tile_size(width, columns)
    except ValueError as error:
        print(f"ce_media sheet: {error}", file=sys.stderr)
        return 2
    if shutil.which("magick") or shutil.which("montage"):
        base = ["magick", "montage"] if shutil.which("magick") else ["montage"]
        args = base + [*map(str, files), "-tile", f"{columns}x", "-geometry", f"{tile}x{tile}+2+2", "-background", "#202020"]
        if label:
            args = base + ["-label", "%f", *map(str, files), "-tile", f"{columns}x", "-geometry", f"{tile}x{tile}+2+2",
                           "-background", "#202020", "-fill", "#dddddd", "-pointsize", "11"]
        subprocess.run(args + [str(out)], check=True)
    else:
        from PIL import Image, ImageDraw
        rows = -(-len(files) // columns)
        canvas = Image.new("RGB", (columns * (tile + 4), rows * (tile + 4 + (14 if label else 0))), "#202020")
        for index, file in enumerate(files):
            with Image.open(file) as source:
                image = source.convert("RGBA")
            image.thumbnail((tile, tile))
            x = (index % columns) * (tile + 4) + 2
            y = (index // columns) * (tile + 4 + (14 if label else 0)) + 2
            canvas.paste(image, (x, y), image)
            if label:
                ImageDraw.Draw(canvas).text((x, y + tile + 1), file.name[:24], fill="#dddddd")
        canvas.save(out)
    print(f"{out} ({out.stat().st_size // 1024} KB, {len(files)} Bilder)")
    return 0


def clip(sources, out: Path, fps: int, height: int, seconds: float) -> int:
    if not shutil.which("ffmpeg"):
        print("ce_media clip: ffmpeg fehlt")
        return 2
    files = frames_of(sources)
    if not files:
        print("ce_media clip: keine Bilder")
        return 2
    listing = out.with_suffix(".frames.txt")
    # (0.31) Windows: forward slashes for ffmpeg's concat list (C:/…), nothing else changes
    spell = (lambda f: f.resolve().as_posix()) if WINDOWS else (lambda f: f.resolve())
    listing.write_text("".join(f"file '{spell(f)}'\nduration {1 / fps:.6f}\n" for f in files), encoding="utf-8")
    command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-t", str(seconds),
               "-vf", f"scale=-2:{height}", "-r", str(fps), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", str(out)]
    result = subprocess.run(command, capture_output=True, text=True,
                            **({"encoding": "utf-8", "errors": "replace"} if WINDOWS else {}))
    listing.unlink(missing_ok=True)
    if result.returncode:
        print(result.stderr.strip()[-400:])
        return 1
    print(f"{out} ({out.stat().st_size // 1024} KB, {min(len(files) / fps, seconds):.1f} s, {height}p)")
    return 0


def still(source: Path, out: Path, width: int) -> int:
    from PIL import Image
    image = Image.open(source).convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    image.save(out)
    print(f"{out} ({out.stat().st_size // 1024} KB, {image.width}x{image.height})")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sheet"); s.add_argument("sources", nargs="+"); s.add_argument("--out", type=Path, required=True)
    s.add_argument("--columns", type=int, default=6); s.add_argument("--width", type=int, default=1280); s.add_argument("--label", action="store_true")
    c = sub.add_parser("clip"); c.add_argument("sources", nargs="+"); c.add_argument("--out", type=Path, required=True)
    c.add_argument("--fps", type=int, default=30); c.add_argument("--height", type=int, default=480); c.add_argument("--seconds", type=float, default=8)
    t = sub.add_parser("still"); t.add_argument("source", type=Path); t.add_argument("--out", type=Path, required=True); t.add_argument("--width", type=int, default=1280)
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "sheet":
        return sheet(args.sources, args.out, args.columns, args.width, args.label)
    if args.command == "clip":
        return clip(args.sources, args.out, args.fps, args.height, args.seconds)
    return still(args.source, args.out, args.width)


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
