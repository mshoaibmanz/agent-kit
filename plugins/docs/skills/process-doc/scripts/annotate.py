#!/usr/bin/env python3
"""Overlay edits onto a screenshot PNG for process-doc.

Adapt a neighbouring process's screen (cover old labels, write new ones) and/or bake annotation
pins onto a flat card. Run with the project venv or ephemerally:

    uv run --with pillow scripts/annotate.py --in neighbor.png --out scanner-confirm-zone.png \
        --ops '[{"op":"cover","xy":[12,10,260,44],"color":"#101418"},
                {"op":"text","xy":[18,16],"text":"Zone Sorting","size":18,"color":"#FFFFFF"},
                {"op":"pin","xy":[420,230],"n":1}]'

Ops (JSON list; coords are pixels from top-left):
  cover  {"xy":[x0,y0,x1,y1], "color":"#RRGGBB"}            filled rect to hide old UI
  text   {"xy":[x,y], "text":"...", "size":18, "color":"#RRGGBB",
          "weight":"regular|bold", "anchor":"la|mm|ma|...",  (PIL anchor; e.g. "mm" centers on xy)
          "bg":"#RRGGBB"(optional), "pad":4}                 write a label (optional bg box)
  rect   {"xy":[x0,y0,x1,y1], "color":"#RRGGBB", "width":3}  outline a region
  pin    {"xy":[x,y], "n":1, "color":"#D98A0B"}              numbered amber call-out circle

Relabel a neighbour cleanly: `cover` the old text block, then `text` the new copy over it. Match the
app's weight ("regular" for body/instructions, "bold" for titles/codes) and use anchor "mm" with the
header centre-x to re-create a centred title.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("annotate")

AMBER = "#D98A0B"
_BOLD_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_REGULAR_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (_BOLD_FONTS if weight == "bold" else _REGULAR_FONTS):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def apply_ops(img: Image.Image, ops: list[dict]) -> Image.Image:
    """Apply the op list to a copy of the image and return it."""
    out = img.convert("RGBA")
    draw = ImageDraw.Draw(out)
    for op in ops:
        kind = op["op"]
        if kind == "cover":
            draw.rectangle(op["xy"], fill=op.get("color", "#FFFFFF"))
        elif kind == "rect":
            draw.rectangle(op["xy"], outline=op.get("color", AMBER), width=int(op.get("width", 3)))
        elif kind == "text":
            font = _font(int(op.get("size", 18)), op.get("weight", "bold"))
            x, y = op["xy"]
            anchor = op.get("anchor")
            if op.get("bg"):
                pad = int(op.get("pad", 4))
                box = draw.textbbox((x, y), op["text"], font=font, anchor=anchor)
                draw.rectangle([box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad], fill=op["bg"])
            draw.text((x, y), op["text"], fill=op.get("color", "#1E2127"), font=font, anchor=anchor)
        elif kind == "pin":
            x, y = op["xy"]
            r = int(op.get("r", 15))
            color = op.get("color", AMBER)
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline="#FFFFFF", width=2)
            font = _font(int(r * 0.95))
            label = str(op["n"])
            w, h = _text_size(draw, label, font)
            draw.text((x - w / 2, y - h / 2 - 1), label, fill="#FFFFFF", font=font)
        else:
            raise ValueError(f"unknown op: {kind!r}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Overlay edits onto a screenshot for process-doc.")
    ap.add_argument("--in", dest="src", required=True, type=Path, help="input PNG")
    ap.add_argument("--out", dest="dst", required=True, type=Path, help="output PNG")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ops", help="ops as an inline JSON list")
    g.add_argument("--ops-file", type=Path, help="path to a JSON file with the ops list")
    args = ap.parse_args()

    ops = json.loads(args.ops_file.read_text() if args.ops_file else args.ops)
    if not isinstance(ops, list):
        raise SystemExit("ops must be a JSON list")

    img = Image.open(args.src)
    result = apply_ops(img, ops).convert("RGB")
    args.dst.parent.mkdir(parents=True, exist_ok=True)
    result.save(args.dst)
    log.info("wrote %s (%d ops)", args.dst, len(ops))


if __name__ == "__main__":
    main()
