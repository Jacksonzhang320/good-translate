"""Reliable PDF translation orchestration and acceptance records.

This module performs short, deterministic stages. OCR, MinerU and other work
that can exceed a minute must be launched through start_task.ps1/job.py.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

import chunk_context
import glossary
import manifest
import meta
import pdf_document
import publish
import run_state


SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_PATH = SCRIPT_DIR.parent / "references" / "translate-prompt.md"
BLOCKING = {"error", "blocking", "fatal"}


class PipelineError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_file(path):
    return digest_bytes(Path(path).read_bytes())


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name + ".", delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        staged = Path(handle.name)
    staged.replace(path)


def write_once(path, data):
    path = Path(path)
    encoded = data.encode("utf-8") if isinstance(data, str) else data
    if path.exists():
        if path.read_bytes() != encoded:
            raise FileExistsError(f"{path} differs; use a new run directory")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)


def _page_columns(lines, width):
    prose = [line for line in lines if len(line["text"].strip()) >= 35]
    left = [line for line in prose if line["bbox"][2] <= width * .54]
    right = [line for line in prose if line["bbox"][0] >= width * .45]
    return 2 if len(left) >= 3 and len(right) >= 3 else 1


def inspect_pdf(pdf_path):
    """Return a cheap routing recommendation; never claims parse acceptance."""
    import fitz
    source = Path(pdf_path).resolve()
    pages = []
    total_chars = 0
    with fitz.open(source) as doc:
        if not len(doc):
            raise PipelineError("PDF has no pages")
        for page in doc:
            raw = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
            lines = []
            math_chars = 0
            for block in raw.get("blocks", []):
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "") for span in line.get("spans", []))
                    if not text.strip():
                        continue
                    lines.append({"text": text, "bbox": line["bbox"]})
                    for span in line.get("spans", []):
                        if re.search(r"math|symbol|cambria", span.get("font", ""), re.I):
                            math_chars += len(span.get("text", ""))
            chars = sum(len(line["text"].strip()) for line in lines)
            total_chars += chars
            images = page.get_image_info()
            page_area = max(1, page.rect.width * page.rect.height)
            largest_image = max((max(0, item["bbox"][2] - item["bbox"][0]) *
                                 max(0, item["bbox"][3] - item["bbox"][1])
                                 for item in images), default=0) / page_area
            pages.append({"page": page.number + 1, "chars": chars,
                          "columns": _page_columns(lines, page.rect.width),
                          "math_font_chars": math_chars,
                          "equation_symbols": len(re.findall(r"[=∑∫√≈≤≥±×÷]", "".join(x["text"] for x in lines))),
                          "images": len(images), "largest_image_ratio": round(largest_image, 4)})
    low_text = [p for p in pages if p["chars"] < 20]
    scan_like = len(low_text) / len(pages) >= .35 or total_chars < max(80, len(pages) * 12)
    two_column = [p["page"] for p in pages if p["columns"] > 1]
    formula_pages = [p["page"] for p in pages if p["math_font_chars"] >= 4 or p["equation_symbols"] >= 3]
    route = "ocr+mineru" if scan_like else "mineru" if two_column or formula_pages else "geometry"
    return {"schema_version": 1, "source": str(source), "sha256": digest_file(source),
            "page_count": len(pages), "text_characters": total_chars,
            "scan_like": scan_like, "low_text_pages": [p["page"] for p in low_text],
            "two_column_pages": two_column, "formula_pages": formula_pages,
            "recommended_route": route,
            "reason": ({"ocr+mineru": "insufficient text layer",
                        "mineru": "complex reading order or formula evidence",
                        "geometry": "single-column text geometry appears usable"})[route],
            "pages": pages}


def _marked(block):
    return f"<!-- tb:{block['id']} -->\n{block['text'].strip()}\n"


def split_document(doc, target_chars):
    if target_chars < 500:
        raise PipelineError("chunk size must be at least 500 characters")
    chunks, current, size = [], [], 0
    for block in doc["blocks"]:
        text = _marked(block)
        if current and size + len(text) > target_chars:
            chunks.append("\n".join(current).rstrip() + "\n")
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        chunks.append("\n".join(current).rstrip() + "\n")
    return chunks


def _blocking_issues(doc):
    return [issue for issue in doc.get("issues", []) if isinstance(issue, dict)
            and (issue.get("severity") in BLOCKING or issue.get("blocking"))]


def prepare(pdf_path, temp_dir, target_lang="zh-CN", instructions="", mineru_dir=None,
            ocr_pdf=None, chunk_size=6000):
    root = Path(temp_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    doc = pdf_document.build_document(pdf_path, root, mineru_dir=mineru_dir, ocr_pdf=ocr_pdf)
    blocked = _blocking_issues(doc)
    state = {"schema_version": 1, "status": "parse_failed" if blocked else "parse_review_pending",
             "updated_at": now(), "source": doc["source"], "parser": doc["parser"],
             "blocks": len(doc["blocks"]), "blocking_issues": blocked,
             "warnings": [x for x in doc.get("issues", []) if x not in blocked]}
    write_json(root / "pipeline_state.json", state)
    if blocked:
        raise PipelineError("Parser has blocking issues; inspect pipeline_state.json and rerun in a new directory with OCR/MinerU as recommended")

    full = "\n".join(_marked(block).rstrip() for block in doc["blocks"]) + "\n"
    write_once(root / "input.md", full)
    chunk_texts = split_document(doc, chunk_size)
    names = [f"chunk{index:04d}.md" for index in range(1, len(chunk_texts) + 1)]
    for name, text in zip(names, chunk_texts):
        write_once(root / name, text)
    glossary_path = root / "glossary.json"
    empty_glossary = {"version": 2, "terms": [], "high_frequency_top_n": 20,
                      "applied_meta_hashes": {}}
    if not glossary_path.exists():
        write_json(glossary_path, empty_glossary)
    split_contract = {"method": "block-boundary-v1", "target_chars": chunk_size,
                      "chunk_count": len(names), "structure_hash": doc["structure_hash"]}
    manifest.create_manifest(str(root), names, str(root / "input.md"),
                             expected_block_ids=[b["id"] for b in doc["blocks"]],
                             split_contract=split_contract, doc_path=root / "doc.json",
                             expected_chunk_files=names)
    prompt_hash = digest_file(PROMPT_PATH)
    prompt_version = f"tb-pdf-v1:{prompt_hash[:16]}:{doc['structure_hash'][:16]}"
    run_state.init_run(str(root), target_lang, instructions, prompt_version)
    config = (f"input_file={Path(pdf_path).resolve()}\noutput_lang={target_lang}\n"
              f"custom_instructions={instructions.replace(chr(10), ' ')}\n"
              f"prompt_version={prompt_version}\nparser={doc['parser']}\n")
    write_once(root / "config.txt", config)
    state.update({"chunks": len(names), "prompt_version": prompt_version})
    write_json(root / "pipeline_state.json", state)
    return state


def _parse_record(root):
    root = Path(root).resolve()
    doc = read_json(root / "doc.json")
    path = root / "qa" / "parse_acceptance.json"
    if not path.is_file():
        return None
    record = read_json(path)
    good = (record.get("status") == "accepted" and
            record.get("source_sha256") == doc.get("source", {}).get("sha256") and
            record.get("structure_hash") == doc.get("structure_hash") and
            record.get("doc_sha256") == digest_file(root / "doc.json") and
            record.get("style_sha256") == digest_file(root / "style.json") and
            bool(record.get("reviewer")) and bool(record.get("evidence")))
    if not good:
        raise PipelineError("Parse acceptance is incomplete or stale")
    return record


def accept_parse(temp_dir, reviewer, evidence):
    root = Path(temp_dir).resolve()
    doc = read_json(root / "doc.json")
    blocked = _blocking_issues(doc)
    if blocked:
        raise PipelineError("Cannot accept a parse with blocking issues")
    if not reviewer.strip() or not evidence.strip():
        raise PipelineError("reviewer and concrete visual evidence are required")
    record = {"schema_version": 1, "status": "accepted", "accepted_at": now(),
              "reviewer": reviewer, "evidence": evidence,
              "source_sha256": doc["source"]["sha256"],
              "structure_hash": doc["structure_hash"],
              "doc_sha256": digest_file(root / "doc.json"),
              "style_sha256": digest_file(root / "style.json"),
              "parser": doc["parser"]}
    write_json(root / "qa" / "parse_acceptance.json", record)
    return record


def dispatch(temp_dir, chunk_ids, context_chars=300):
    root = Path(temp_dir).resolve()
    if _parse_record(root) is None:
        raise PipelineError("Parse visual acceptance is required before translation dispatch")
    snapshots = run_state.dispatch_chunks(str(root), chunk_ids)
    base_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    packets = {}
    for chunk_id in chunk_ids:
        snapshot = snapshots[chunk_id]
        source = root / snapshot["source_file"]
        context = chunk_context.get_neighbor_context(str(root), source.name, context_chars)
        packet = {"schema_version": 1, "chunk_id": chunk_id,
                  "source_path": str(source),
                  "output_path": str(root / snapshot["output_file"]),
                  "meta_path": str(root / f"output_{chunk_id}.meta.json"),
                  "translation_contract": snapshot["translation_contract"],
                  "term_table": snapshot["term_table"], "neighbor_context": context,
                  "instructions": base_prompt}
        packet_hash = digest_bytes(json.dumps(packet, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode("utf-8"))
        path = root / "dispatch" / f"{chunk_id}-{packet_hash[:12]}.json"
        write_once(path, json.dumps(packet, ensure_ascii=False, indent=2) + "\n")
        packets[chunk_id] = {"packet_path": str(path), "packet_hash": packet_hash,
                             "output_path": packet["output_path"], "meta_path": packet["meta_path"]}
    return packets


def record(temp_dir, chunk_ids):
    root = Path(temp_dir).resolve()
    for chunk_id in chunk_ids:
        meta.load_meta(root / f"output_{chunk_id}.meta.json")
    return {"recorded_chunk_ids": run_state.record_chunks(str(root), chunk_ids)}


def _meta_state(root):
    root = Path(root).resolve()
    current_glossary = glossary.load_glossary(root / "glossary.json")
    applied = current_glossary.get("applied_meta_hashes", {})
    ids = [item["id"] for item in manifest.load_manifest(str(root)).get("chunks", [])]
    missing, malformed, unmerged = [], [], []
    for chunk_id in ids:
        path = root / f"output_{chunk_id}.meta.json"
        if not path.is_file():
            missing.append(chunk_id)
            continue
        try:
            data = meta.load_meta(path)
        except (ValueError, OSError):
            malformed.append(chunk_id)
            continue
        if applied.get(chunk_id) != meta.meta_content_hash(data):
            unmerged.append(chunk_id)
    return {"missing": missing, "malformed": malformed, "unmerged": unmerged,
            "complete": not (missing or malformed or unmerged)}


def freeze(temp_dir):
    root = Path(temp_dir).resolve()
    state = _meta_state(root)
    if not state["complete"]:
        raise PipelineError(f"Cannot freeze until every current meta is valid and merged: {state}")
    return {"frozen_glossary_hash": run_state.freeze_glossary(str(root)), "meta": state}


def build(temp_dir, **kwargs):
    result = publish.build(temp_dir, **kwargs)
    if result["status"] == "failed":
        raise PipelineError("Publication failed: " + "; ".join(result.get("errors", [])))
    return result


def accept_publish(temp_dir, reviewer, evidence):
    root = Path(temp_dir).resolve()
    result = read_json(root / "build_result.json")
    if result.get("status") not in {"generated", "accepted"}:
        raise PipelineError("No successfully generated publication is ready for review")
    if not reviewer.strip() or not evidence.strip():
        raise PipelineError("reviewer and concrete visual evidence are required")
    artifacts = {item["path"]: item["sha256"] for outputs in result.get("outputs", {}).values()
                 for item in outputs.values() if item.get("status") == "generated"}
    for path, expected in artifacts.items():
        if not Path(path).is_file() or digest_file(path) != expected:
            raise PipelineError(f"Generated artifact changed or is missing: {path}")
    record = {"schema_version": 1, "status": "accepted", "accepted_at": now(),
              "render_hash": result["render_hash"], "reviewer": reviewer,
              "evidence": evidence, "artifacts": artifacts}
    acceptance = root / "qa" / "publish_acceptance.json"
    write_json(acceptance, record)
    result["qa"]["acceptance"] = publish._acceptance(acceptance, result)
    result["status"] = "accepted"
    write_json(root / "build_result.json", result)
    write_json(Path(result["output_dir"]) / "build_result.json", result)
    return result


def status(temp_dir):
    root = Path(temp_dir).resolve()
    blockers = []
    doc = read_json(root / "doc.json") if (root / "doc.json").is_file() else None
    parse_status = "missing"
    if doc:
        blocking = _blocking_issues(doc)
        if blocking:
            parse_status = "failed"
            blockers.extend(issue.get("code", "parse_error") for issue in blocking)
        else:
            try:
                parse_status = "accepted" if _parse_record(root) else "review_pending"
            except PipelineError as exc:
                parse_status = "stale_acceptance"
                blockers.append(str(exc))
        if parse_status != "accepted" and parse_status not in {"failed", "stale_acceptance"}:
            blockers.append("parse visual acceptance is pending")
    else:
        blockers.append("doc.json is missing")
    translation = None
    if (root / "manifest.json").is_file():
        plan = run_state.plan(str(root))
        translation = {"needed": len(plan["translation_chunk_ids"]),
                       "record_only": len(plan["record_only_chunk_ids"]),
                       "unchanged": len(plan["unchanged_chunk_ids"]),
                       "translation_chunk_ids": plan["translation_chunk_ids"],
                       "pending_dispatch_ids": sorted(run_state.load_run_state(str(root)).get("dispatches", {}))}
        if translation["needed"]:
            blockers.append(f"{translation['needed']} chunks need translation/correction")
        if translation["pending_dispatch_ids"]:
            blockers.append(f"{len(translation['pending_dispatch_ids'])} dispatched chunks are not recorded")
        meta_state = _meta_state(root)
        if not meta_state["complete"]:
            blockers.append("current per-chunk observations are missing, malformed, or unmerged")
    else:
        meta_state = None
    result = read_json(root / "build_result.json") if (root / "build_result.json").is_file() else None
    publish_status = result.get("status") if result else "not_started"
    if publish_status == "generated":
        blockers.append("publication visual acceptance is pending")
    if publish_status == "accepted":
        for outputs in result.get("outputs", {}).values():
            for item in outputs.values():
                path = Path(item.get("path", ""))
                if not path.is_file() or digest_file(path) != item.get("sha256"):
                    blockers.append(f"accepted artifact changed or is missing: {path}")
    overall = "accepted" if publish_status == "accepted" and not blockers else "blocked" if blockers else "ready"
    return {"schema_version": 1, "status": overall, "parse": parse_status,
            "translation": translation, "meta": meta_state, "publish": publish_status,
            "outputs": result.get("outputs", {}) if result else {}, "blockers": blockers}


def cleanup(temp_dir):
    """Remove only known regenerable scratch files after accepted QA."""
    root = Path(temp_dir).resolve()
    result = read_json(root / "build_result.json")
    if result.get("status") != "accepted":
        raise PipelineError("Cleanup requires an accepted publication")
    removed = []
    for name in ("input.html", "output.html", "bilingual_raw.html"):
        path = (root / name).resolve()
        if path.parent == root and path.is_file():
            path.unlink()
            removed.append(str(path))
    for name in (".staging", ".render-tmp"):
        path = (root / name).resolve()
        if path.parent == root and path.is_dir():
            shutil.rmtree(path)
            removed.append(str(path))
    return {"status": "completed", "removed": removed,
            "preserved": ["doc.json", "style.json", "manifest.json", "run_state.json",
                          "glossary.json", "chunks", "outputs", "qa", "assets"]}


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("pdf")
    prep = sub.add_parser("prepare")
    prep.add_argument("pdf")
    prep.add_argument("--temp-dir", required=True)
    prep.add_argument("--lang", default="zh-CN")
    prep.add_argument("--instructions", default="")
    prep.add_argument("--instructions-file")
    prep.add_argument("--mineru-dir")
    prep.add_argument("--ocr-pdf")
    prep.add_argument("--chunk-size", type=int, default=6000)
    ap = sub.add_parser("accept-parse")
    ap.add_argument("temp_dir")
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--evidence", required=True)
    dsp = sub.add_parser("dispatch")
    dsp.add_argument("temp_dir")
    dsp.add_argument("chunk_ids", nargs="+")
    dsp.add_argument("--context-chars", type=int, default=300)
    rec = sub.add_parser("record")
    rec.add_argument("temp_dir")
    rec.add_argument("chunk_ids", nargs="+")
    frz = sub.add_parser("freeze")
    frz.add_argument("temp_dir")
    bld = sub.add_parser("build")
    bld.add_argument("temp_dir")
    bld.add_argument("--formats", default="html,pdf")
    bld.add_argument("--editions", default="mono,bilingual,gov_doc")
    bld.add_argument("--mono-preset", choices=["original", "gov_doc"], help="Preset layout for mono edition (e.g. gov_doc for GB/T 9704-2012)")
    for option in ("title", "author", "lang", "cover", "output-dir", "browser-path", "file-stem"):
        bld.add_argument("--" + option)
    pub = sub.add_parser("accept-publish")
    pub.add_argument("temp_dir")
    pub.add_argument("--reviewer", required=True)
    pub.add_argument("--evidence", required=True)
    stat = sub.add_parser("status")
    stat.add_argument("temp_dir")
    clean = sub.add_parser("cleanup")
    clean.add_argument("temp_dir")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "inspect":
            value = inspect_pdf(args.pdf)
        elif args.cmd == "prepare":
            instructions = args.instructions
            if args.instructions_file:
                instructions = Path(args.instructions_file).read_text(encoding="utf-8")
            value = prepare(args.pdf, args.temp_dir, args.lang, instructions,
                            args.mineru_dir, args.ocr_pdf, args.chunk_size)
        elif args.cmd == "accept-parse":
            value = accept_parse(args.temp_dir, args.reviewer, args.evidence)
        elif args.cmd == "dispatch":
            value = dispatch(args.temp_dir, args.chunk_ids, args.context_chars)
        elif args.cmd == "record":
            value = record(args.temp_dir, args.chunk_ids)
        elif args.cmd == "freeze":
            value = freeze(args.temp_dir)
        elif args.cmd == "build":
            value = build(args.temp_dir, formats=args.formats, editions=args.editions,
                          mono_preset=getattr(args, "mono_preset", None),
                          file_stem=getattr(args, "file_stem", None),
                          title=args.title, author=args.author, lang=args.lang,
                          cover=args.cover, output_dir=args.output_dir,
                          browser_path=args.browser_path)
        elif args.cmd == "accept-publish":
            value = accept_publish(args.temp_dir, args.reviewer, args.evidence)
        elif args.cmd == "status":
            value = status(args.temp_dir)
        elif args.cmd == "cleanup":
            value = cleanup(args.temp_dir)
        _print(value)
        return 0
    except Exception as exc:
        _print({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
