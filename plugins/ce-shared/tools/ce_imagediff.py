#!/usr/bin/env python3
"""ce_imagediff.py A.png B.png [--out DIFF.png] [--kind ui|world|bake|any] [--json] - how different are two images, as numbers.

0.5.0: before, a reviewer got two screenshots and had to look; shotdiff.py (Cube Escape) compared pixels, Toolgen hashed bytes.
This gives one verdict with numbers a judge can quote: SSIM (structure, 0..1), PSNR (dB), changed-pixel share (%) and the
bounding box of the change, plus a difference image (heatmap over A) when --out is given. Thresholds per kind of image
(from the repos' practice; override with --ssim): ui 0.98, world 0.95, bake 0.99, any 0.95. Exit 0 = same within threshold,
1 = different, 2 = cannot compare (size differs and --resize not given, file missing).

Needs numpy and Pillow (both in the repos' Python). Several pairs: ce_imagediff.py --pairs a1.png:b1.png a2.png:b2.png
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

THRESHOLDS = {"ui": 0.98, "world": 0.95, "bake": 0.99, "any": 0.95}
# (0.31) Windows: each side may start with a drive (C:\a.png:D:\b.png), the colon after it is no separator
WINDOWS_PAIR = re.compile(r"((?:[A-Za-z]:)?[^:]*):((?:[A-Za-z]:)?[^:]*)")


def split_pair(item: str, windows: bool = os.name == "nt") -> list[str]:
    """A:B of --pairs; on Windows drive letters stay with their path. ValueError without a separator."""
    if not windows:
        return item.split(":", 1)
    match = WINDOWS_PAIR.fullmatch(item)
    if not match:
        raise ValueError(f"--pairs {item}: A:B erwartet")
    return list(match.groups())


def load(path: Path, size=None):
    from PIL import Image
    image = Image.open(path).convert("RGB")
    if size and image.size != size:
        image = image.resize(size, Image.LANCZOS)
    return image


def to_gray(image):
    import numpy as np
    arr = np.asarray(image, dtype=np.float64)
    return 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]


def ssim(a, b, window: int = 7) -> float:
    """Mean SSIM on grayscale with a uniform window (no scipy)."""
    import numpy as np
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2

    def box(x):
        pad = window // 2
        padded = np.pad(x, pad, mode="reflect")
        summed = np.cumsum(np.cumsum(padded, axis=0), axis=1)
        summed = np.pad(summed, ((1, 0), (1, 0)))
        h, w = x.shape
        return (summed[window:window + h, window:window + w] - summed[:h, window:window + w]
                - summed[window:window + h, :w] + summed[:h, :w]) / (window * window)

    mu_a, mu_b = box(a), box(b)
    var_a, var_b = box(a * a) - mu_a ** 2, box(b * b) - mu_b ** 2
    cov = box(a * b) - mu_a * mu_b
    num = (2 * mu_a * mu_b + c1) * (2 * cov + c2)
    den = (mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2)
    return float(np.mean(num / den))


def compare(path_a: Path, path_b: Path, out: Path | None = None, kind: str = "any", threshold: float | None = None,
            resize: bool = False, pixel_tolerance: int = 8, max_changed_percent: float | None = None,
            region: tuple[int, int, int, int] | None = None) -> dict:
    import numpy as np
    a = load(path_a)
    b = load(path_b, a.size if resize else None)
    if a.size != b.size:
        return {"a": str(path_a), "b": str(path_b), "error": f"Größen verschieden {a.size} vs {b.size} (--resize)", "same": False}
    if threshold is not None and (type(threshold) not in (float, int) or not math.isfinite(threshold) or not 0 <= threshold <= 1):
        raise ValueError("SSIM-Grenze muss zwischen 0 und 1 liegen")
    if max_changed_percent is not None and (type(max_changed_percent) not in (float, int) or not math.isfinite(max_changed_percent) or not 0 <= max_changed_percent <= 100):
        raise ValueError("Pixelgrenze muss zwischen 0 und 100 liegen")
    if type(pixel_tolerance) is not int or not 0 <= pixel_tolerance <= 255:
        raise ValueError("Pixeltoleranz muss eine ganze Zahl zwischen 0 und 255 sein")
    if region is not None:
        x, y, width, height = region
        if (any(type(v) is not int for v in region) or min(x, y) < 0 or min(width, height) <= 0
                or x+width > a.width or y+height > a.height):
            raise ValueError("Vergleichsregion liegt außerhalb des Originalbildes")
        a, b = a.crop((x, y, x+width, y+height)), b.crop((x, y, x+width, y+height))
    ga, gb = to_gray(a), to_gray(b)
    arr_a, arr_b = np.asarray(a, dtype=np.int16), np.asarray(b, dtype=np.int16)
    delta = np.abs(arr_a - arr_b).max(axis=2)
    changed = delta > pixel_tolerance
    share = float(changed.mean() * 100)
    mse = float(np.mean((ga - gb) ** 2))
    psnr = float("inf") if mse == 0 else 20 * math.log10(255 / math.sqrt(mse))
    score = ssim(ga, gb)
    limit = threshold if threshold is not None else THRESHOLDS.get(kind, THRESHOLDS["any"])
    box = None
    if changed.any():
        ys, xs = np.where(changed)
        box = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    result = {"a": str(path_a), "b": str(path_b), "size": list(a.size), "ssim": round(score, 4),
              "psnr_db": None if psnr == float("inf") else round(psnr, 2), "changed_percent": round(share, 3),
              "changed_box": box, "threshold": limit, "kind": kind, "same": score >= limit}
    result.update(max_changed_percent=max_changed_percent, region=list(region) if region else None,
                  pixel_tolerance=pixel_tolerance, ssim_passed=score >= limit,
                  pixels_passed=max_changed_percent is None or share <= max_changed_percent)
    result['same'] = result['ssim_passed'] and result['pixels_passed']
    if out is not None:
        if out.resolve() in (path_a.resolve(), path_b.resolve()):
            raise ValueError('Differenzbild darf keine Eingabe überschreiben')
        from PIL import Image
        heat = np.zeros_like(arr_a, dtype=np.uint8)
        heat[..., 0] = np.clip(delta * 3, 0, 255).astype(np.uint8)
        base = (arr_a.astype(np.float64) * 0.35).astype(np.uint8)
        composite = np.maximum(base, heat)
        Image.fromarray(composite).save(out)
        result["diff_image"] = str(out)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("a", nargs="?", type=Path)
    parser.add_argument("b", nargs="?", type=Path)
    parser.add_argument("--pairs", nargs="*", help="A:B pairs")
    parser.add_argument("--out", type=Path, help="difference image (single pair)")
    parser.add_argument("--kind", choices=sorted(THRESHOLDS), default="any")
    parser.add_argument("--ssim", type=float, help="threshold override")
    parser.add_argument("--resize", action="store_true", help="resize B to A when sizes differ")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-changed-percent", type=float, help="zusätzliche RGB-Pixelgrenze; muss ebenfalls bestehen")
    parser.add_argument("--pixel-tolerance", type=int, default=8)
    parser.add_argument("--region", type=int, nargs=4, metavar=('X', 'Y', 'WIDTH', 'HEIGHT'))
    parser.add_argument("--reference", type=Path, help="versioniertes Referenzprofil mit SSIM-/Pixelgrenzen und optionaler Region")
    parser.add_argument("--viewer", type=Path, help="portablen HTML-Vergleich erzeugen (ein Bildpaar)")
    parser.add_argument("--clip", type=Path, help="vorhandenen MP4-Prüfclip in den Viewer einbetten")
    args = parser.parse_args(argv)
    if args.reference:
        try:
            from ce_contract import read_json
            profile = read_json(args.reference)
            if (not isinstance(profile, dict) or set(profile)-{'version','ssim','max_changed_percent','pixel_tolerance','region'}
                    or profile.get('version') != 1 or type(profile.get('version')) is not int
                    or type(profile.get('max_changed_percent')) not in (int, float)
                    or not 0 <= profile['max_changed_percent'] <= 100 or not math.isfinite(profile['max_changed_percent'])):
                raise ValueError('Referenzprofil Version 1 mit expliziter Pixelgrenze erforderlich')
            for key in ('ssim','max_changed_percent','pixel_tolerance','region'):
                if key in profile:
                    setattr(args, key, profile[key])
        except (OSError, ValueError, TypeError) as error:
            parser.error(str(error))
    pairs = [(args.a, args.b)] if args.a and args.b else []
    for item in args.pairs or []:
        left, right = split_pair(item)
        pairs.append((Path(left), Path(right)))
    if not pairs:
        parser.error("A B oder --pairs A:B …")
    if (args.viewer or args.clip) and len(pairs) != 1:
        parser.error('Viewer und Clip benötigen genau ein Bildpaar')
    if args.clip and not args.viewer:
        parser.error('--clip benötigt --viewer')
    if args.viewer and not args.out:
        args.out = args.viewer.with_suffix('.diff.png')
    inputs = [path for pair in pairs for path in pair] + [path for path in (args.clip, args.reference) if path]
    outputs = [path for path in (args.viewer, args.out) if path]
    def same_file(a, b):
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    if any(same_file(out, source) for out in outputs for source in inputs):
        parser.error('Ausgabe darf keine Bild-, Clip- oder Referenzeingabe überschreiben')
    if len(outputs) == 2 and same_file(*outputs):
        parser.error('Viewer und Differenzbild brauchen getrennte Ausgaben')
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    results, status = [], 0
    for left, right in pairs:
        if not left.is_file() or not right.is_file():
            results.append({"a": str(left), "b": str(right), "error": "Datei fehlt", "same": False})
            status = max(status, 2)
            continue
        try:
            result = compare(left, right, args.out if len(pairs) == 1 else None, args.kind, args.ssim, args.resize,
                             args.pixel_tolerance, args.max_changed_percent, args.region)
            if args.viewer:
                from ce_console import evidence
                evidence(result, left, right, args.viewer, args.clip)
        except (OSError, ValueError, TypeError) as error:
            result = {'a': str(left), 'b': str(right), 'error': str(error), 'same': False}
        results.append(result)
        status = max(status, 2 if "error" in result else (0 if result["same"] else 1))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        for r in results:
            if "error" in r:
                print(f"{Path(r['a']).name} vs {Path(r['b']).name}: {r['error']}")
            else:
                print(f"{Path(r['a']).name} vs {Path(r['b']).name}: {'gleich' if r['same'] else 'VERSCHIEDEN'} - SSIM {r['ssim']} (Grenze {r['threshold']}), "
                      f"PSNR {r['psnr_db'] if r['psnr_db'] is not None else '∞'} dB, {r['changed_percent']} % Pixel geändert"
                      + (f", Bereich {r['changed_box']}" if r["changed_box"] else "") + (f", Diffbild {r['diff_image']}" if r.get("diff_image") else ""))
    return status


if __name__ == "__main__":
    try:  # (0.31) Windows: a cp1252 console or pipe cannot take the tool's „“ → ·
        import ce_common as _console
        _console.utf8_console()
    except ImportError:
        pass
    sys.exit(main(sys.argv[1:]))
