"""Persistent browser-render smoke for the PDF translation pipeline."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import glossary
import meta
import pipeline


EMPTY_META = {"schema_version": 1, "new_entities": [], "alias_hypotheses": [],
              "attribute_hypotheses": [], "used_term_sources": [], "conflicts": []}


def stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def main(argv=None):
    import argparse
    import fitz
    from PIL import Image
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = stamp()

    source = out / "source.pdf"
    pdf = fitz.open()
    page = pdf.new_page(width=420, height=520)
    page.insert_text((25, 38), "A Styled Scientific Page", fontsize=19, color=(.1, .2, .45))
    left_lines = ["Left column establishes the measured body style.",
                  "The inline squared term stays mathematically exact.",
                  "A final sentence tests balanced column flow."]
    right_lines = ["Right column follows the intended reading order.",
                   "Its prose will become translated text in production.",
                   "This fixture keeps every source object covered."]
    for index, text in enumerate(left_lines):
        page.insert_text((25, 78 + index * 22), text, fontsize=7)
    for index, text in enumerate(right_lines):
        page.insert_text((225, 78 + index * 22), text, fontsize=7)
    image = Image.new("RGB", (220, 100), "#87a9c9")
    pixels = image.load()
    for x in range(20, 200):
        y = int(80 - 45 * (x - 20) / 180)
        for offset in range(3):
            if 0 <= y + offset < 100:
                pixels[x, y + offset] = (30, 55, 95)
    stream = io.BytesIO()
    image.save(stream, "PNG")
    page.insert_image(fitz.Rect(100, 180, 320, 280), stream=stream.getvalue())
    page.insert_text((95, 300), "Fig. 1 Complete source figure and caption.", fontsize=8)
    page.insert_text((125, 355), "E = mc2     (1)", fontsize=12)
    page.insert_text((105, 420), "Cell A          Cell B", fontsize=9)
    pdf.save(source)
    pdf.close()

    mineru = out / "mineru"
    mineru.mkdir()
    def block(kind, index, box, text):
        return {"type": kind, "index": index, "bbox": box,
                "lines": [{"spans": [{"content": text}]}]}
    blocks = [block("title", 0, [20, 18, 400, 52], "A Styled Scientific Page")]
    for index, text in enumerate(left_lines, 1):
        spans = ([{"content": "The inline "}, {"type": "inline_equation", "content": "x^2"},
                  {"content": " term stays mathematically exact."}]
                 if index == 2 else [{"content": text}])
        blocks.append({"type": "text", "index": index,
                       "bbox": [20, 60 + (index - 1) * 22, 205, 90 + (index - 1) * 22],
                       "lines": [{"spans": spans}]})
    for index, text in enumerate(right_lines, 4):
        blocks.append(block("text", index, [220, 60 + (index - 4) * 22, 410,
                                              90 + (index - 4) * 22], text))
    blocks.extend([
        {"type": "image", "index": 7, "bbox": [90, 170, 330, 315], "blocks": [
            {"type": "image_body", "bbox": [100, 180, 320, 280]},
            {"type": "image_caption", "bbox": [90, 285, 340, 315],
             "lines": [{"spans": [{"content": "Fig. 1 Complete source figure and caption."}]}]},
        ]},
        {"type": "interline_equation", "index": 8, "bbox": [115, 330, 300, 375],
         "spans": [{"content": r"E=mc^2\\tag{1}"}]},
        {"type": "table", "index": 9, "bbox": [90, 390, 330, 445], "blocks": [
            {"type": "table_body", "bbox": [90, 390, 330, 445],
             "spans": [{"html": "<table><tr><th>Measure</th><th>Value</th></tr><tr><td>Cell A</td><td>Cell B</td></tr></table>"}]}
        ]},
    ])
    middle = {"pdf_info": [{"page_idx": 0, "page_size": [420, 520],
                             "discarded_blocks": [], "para_blocks": blocks}]}
    (mineru / "source_middle.json").write_text(json.dumps(middle), encoding="utf-8")

    run = out / "run"
    pipeline.prepare(source, run, "zh-CN", "忠实、清晰的学术中文", mineru_dir=mineru,
                     chunk_size=1500)
    pipeline.accept_parse(run, "render-smoke", "fixture page object coverage, reading order, full figure and equation crop checked")
    manifest_data = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    ids = [item["id"] for item in manifest_data["chunks"]]
    pipeline.dispatch(run, ids)
    for item in manifest_data["chunks"]:
        shutil.copy2(run / item["source_file"], run / item["output_file"])
        meta.save_meta(run / f"output_{item['id']}.meta.json", EMPTY_META)
        pipeline.record(run, [item["id"]])
    current = glossary.load_glossary(run / "glossary.json")
    current["applied_meta_hashes"] = {chunk_id: meta.meta_content_hash(EMPTY_META) for chunk_id in ids}
    glossary.save_glossary(run / "glossary.json", current)
    pipeline.freeze(run)
    result = pipeline.build(run, formats=("html", "pdf"), editions=("mono", "bilingual"),
                            title="Render smoke", author="translate-book", lang="zh-CN")
    if result["status"] != "generated":
        raise RuntimeError(result)
    previews = {}
    for edition in ("mono", "bilingual"):
        pdf_path = Path(result["outputs"][edition]["pdf"]["path"])
        with fitz.open(pdf_path) as rendered:
            if not len(rendered):
                raise RuntimeError(f"empty rendered PDF: {pdf_path}")
            page_images = []
            for page in rendered:
                page_images.append(Image.open(io.BytesIO(page.get_pixmap(
                    matrix=fitz.Matrix(1.4, 1.4), alpha=False).tobytes("png"))).convert("RGB"))
            preview = out / f"preview-{edition}.png"
            canvas = Image.new("RGB", (max(image.width for image in page_images),
                                        sum(image.height for image in page_images) + 12 * (len(page_images) - 1)),
                               "#c5c5c5")
            offset = 0
            for image in page_images:
                canvas.paste(image, (0, offset))
                offset += image.height + 12
            canvas.save(preview)
            previews[edition] = {"pdf": str(pdf_path), "pages": len(rendered),
                                 "preview": str(preview), "sha256": pipeline.digest_file(pdf_path)}
    method_fingerprint = pipeline.digest_bytes(json.dumps({
        name: pipeline.digest_file(SCRIPTS / name) for name in
        ("normalize_mineru.py", "pdf_document.py", "manifest.py", "run_state.py",
         "publish.py", "qa_preview.py", "pipeline.py")
    }, sort_keys=True).encode("utf-8"))
    evidence = {"smoke_id": "tb-render-20260915-02", "test_type": "parse_and_browser_render",
                "entrypoint": "tests/render_smoke.py", "command_fingerprint": method_fingerprint,
                "started_at": started, "finished_at": stamp(), "status": "passed",
                "checks": {"parse_gate": "passed", "state_gate": "passed",
                           "mono_dom": result["qa"]["render"]["mono"],
                           "bilingual_dom": result["qa"]["render"]["bilingual"],
                           "publication_status": result["status"]},
                "output_dir": str(out), "previews": previews}
    pipeline.write_json(out / "smoke_pass.json", evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
