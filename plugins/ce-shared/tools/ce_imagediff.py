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
import sys
from pathlib import Path

THRESHOLDS = {"ui": 0.98, "world": 0.95, "bake": 0.99, "any": 0.95}


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
            resize: bool = False, pixel_tolerance: int = 8) -> dict:
    import numpy as np
    a = load(path_a)
    b = load(path_b, a.size if resize else None)
    if a.size != b.size:
        return {"a": str(path_a), "b": str(path_b), "error": f"Größen verschieden {a.size} vs {b.size} (--resize)", "same": False}
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
    if out is not None:
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
    args = parser.parse_args(argv)
    pairs = [(args.a, args.b)] if args.a and args.b else []
    for item in args.pairs or []:
        left, right = item.split(":", 1)
        pairs.append((Path(left), Path(right)))
    if not pairs:
        parser.error("A B oder --pairs A:B …")
    results, status = [], 0
    for left, right in pairs:
        if not left.is_file() or not right.is_file():
            results.append({"a": str(left), "b": str(right), "error": "Datei fehlt", "same": False})
            status = max(status, 2)
            continue
        result = compare(left, right, args.out if len(pairs) == 1 else None, args.kind, args.ssim, args.resize)
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
    sys.exit(main(sys.argv[1:]))
