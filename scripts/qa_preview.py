"""Create versioned contact sheets for parse and publication visual review."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re

from PIL import Image, ImageDraw, ImageFont


IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sheets(items, out_dir, columns=2, rows=3, cell=(620, 760)):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default()
    manifest, per_sheet = [], columns * rows
    for start in range(0, len(items), per_sheet):
        group = items[start:start + per_sheet]
        canvas = Image.new("RGB", (columns * cell[0], rows * cell[1]), "#d0d0d0")
        draw = ImageDraw.Draw(canvas)
        entries = []
        for position, item in enumerate(group):
            col, row = position % columns, position // columns
            x, y = col * cell[0], row * cell[1]
            image = item["image"].convert("RGB")
            image.thumbnail((cell[0] - 20, cell[1] - 42), Image.Resampling.LANCZOS)
            ox = x + (cell[0] - image.width) // 2
            oy = y + 28 + (cell[1] - 42 - image.height) // 2
            canvas.paste(image, (ox, oy))
            draw.text((x + 8, y + 7), item["label"], fill="#111111", font=font)
            entries.append({key: value for key, value in item.items() if key != "image"})
        path = out_dir / f"sheet-{start // per_sheet + 1:03d}.png"
        canvas.save(path)
        manifest.append({"path": str(path), "sha256": sha(path), "items": entries})
    record = {"schema_version": 1, "sheets": manifest, "item_count": len(items)}
    (out_dir / "manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    return record


def pdf_preview(pdf_path, output_root, label=None, scale=.7):
    import fitz
    pdf_path = Path(pdf_path).resolve()
    version = sha(pdf_path)[:12]
    out = Path(output_root).resolve() / f"{label or pdf_path.stem}-{version}"
    items = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            data = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
            items.append({"label": f"page {page.number + 1}", "page": page.number + 1,
                          "image": Image.open(io.BytesIO(data))})
    return sheets(items, out)


def object_preview(temp_dir):
    root = Path(temp_dir).resolve()
    doc_path = root / "doc.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8-sig"))
    out = root / "qa" / "previews" / f"objects-{sha(doc_path)[:12]}"
    items = []
    for block in doc.get("blocks", []):
        if block.get("kind") not in {"figure", "formula", "table"} or block.get("role") == "page_fallback":
            continue
        match = IMAGE_RE.search(block.get("text", ""))
        if not match:
            continue
        path = (root / match.group(1)).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"unsafe or missing object asset: {match.group(1)}")
        refs = ",".join(str(ref.get("page")) for ref in block.get("source_refs", []))
        items.append({"label": f"{block['id']} {block['kind']} source p.{refs}",
                      "block_id": block["id"], "kind": block["kind"],
                      "source_pages": refs, "asset": str(path), "asset_sha256": sha(path),
                      "image": Image.open(path)})
    return sheets(items, out, columns=2, rows=3, cell=(640, 460))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    pdf = sub.add_parser("pdf")
    pdf.add_argument("pdf")
    pdf.add_argument("--out", required=True)
    pdf.add_argument("--label")
    pdf.add_argument("--scale", type=float, default=.7)
    objects = sub.add_parser("objects")
    objects.add_argument("temp_dir")
    args = parser.parse_args(argv)
    result = (pdf_preview(args.pdf, args.out, args.label, args.scale)
              if args.cmd == "pdf" else object_preview(args.temp_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
