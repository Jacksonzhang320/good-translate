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

import audit_layout
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
    route = "ocr+mineru" if scan_like else "mineru" if (two_column or formula_pages) else "geometry"
    return {"schema_version": 1, "source": str(source), "sha256": digest_file(source),
            "page_count": len(pages), "text_characters": total_chars,
            "scan_like": scan_like, "low_text_pages": [p["page"] for p in low_text],
            "two_column_pages": two_column, "formula_pages": formula_pages,
            "recommended_route": route,
            "reason": ({"ocr+mineru": "insufficient text layer",
                        "mineru": "complex reading order, dual columns, or formula evidence",
                        "geometry": "clean single-column digital vector text geometry"})[route],
            "pages": pages}


def _marked(block):
    return f"<!-- tb:{block['id']} -->\n{block['text'].strip()}\n"


def split_document(doc, target_chars):
    if target_chars < 500:
        raise PipelineError("chunk size must be at least 500 characters")
    chunks, current, size = [], [], 0
    current_is_ref = None

    for block in doc["blocks"]:
        text = _marked(block)
        is_ref = bool(block.get("is_reference"))
        is_major_heading = block.get("kind") == "heading" and block.get("level", 3) <= 2

        should_split = False
        if current:
            # 1. Clean boundary: Never mix body prose and citations in one chunk
            if current_is_ref is not None and is_ref != current_is_ref:
                should_split = True
            # 2. Hard overflow on character count
            elif size + len(text) > target_chars:
                should_split = True
            # 3. Soft boundary at major headings when chunk is sufficiently full (>=60%)
            elif is_major_heading and size >= int(target_chars * 0.6):
                should_split = True

        if should_split and current:
            chunks.append("\n".join(current).rstrip() + "\n")
            current, size = [], 0

        current.append(text)
        size += len(text)
        current_is_ref = is_ref

    if current:
        chunks.append("\n".join(current).rstrip() + "\n")
    return chunks


def split_document_by_cuts(doc, cuts):
    """Deterministically split doc['blocks'] at specified block IDs into chunk markdown strings."""
    blocks = doc.get("blocks", [])
    if not blocks:
        return []
    cut_set = set(cuts or [])
    chunks = []
    current = []
    for block in blocks:
        b_id = block["id"]
        if b_id in cut_set and current:
            chunks.append("\n".join(current).rstrip() + "\n")
            current = []
        current.append(_marked(block))
    if current:
        chunks.append("\n".join(current).rstrip() + "\n")
    return chunks


def _blocking_issues(doc):
    return [issue for issue in doc.get("issues", []) if isinstance(issue, dict)
            and (issue.get("severity") in BLOCKING or issue.get("blocking"))]


def prepare(pdf_path, temp_dir, target_lang="zh-CN", instructions="", mineru_dir=None,
            ocr_pdf=None, chunk_size=12000):
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
    audit_res = audit_layout.audit_publication_dir(Path(result["output_dir"]))
    if audit_res.get("status") == "failed":
        err_msgs = []
        for fname, ed in audit_res.get("editions", {}).items():
            for iss in ed.get("issues", []):
                if iss.get("severity") == "error":
                    err_msgs.append(f"{fname}: {iss.get('message')}")
        raise PipelineError(f"Layout audit failed with critical errors: {'; '.join(err_msgs)}")
    record = {"schema_version": 1, "status": "accepted", "accepted_at": now(),
              "render_hash": result["render_hash"], "reviewer": reviewer,
              "evidence": evidence, "artifacts": artifacts,
              "layout_audit": audit_res.get("status", "passed")}
    acceptance = root / "qa" / "publish_acceptance.json"
    write_json(acceptance, record)
    result["qa"]["acceptance"] = publish._acceptance(acceptance, result)
    result["status"] = "accepted"
    write_json(root / "build_result.json", result)
    write_json(Path(result["output_dir"]) / "build_result.json", result)
    return result


def outline(temp_dir):
    root = Path(temp_dir).resolve()
    doc_path = root / "doc.json"
    if not doc_path.is_file():
        raise PipelineError("doc.json not found; run prepare first")
    doc = read_json(doc_path)
    entries = doc.get("outline", [])
    if not entries:
        for b in doc.get("blocks", []):
            if b.get("kind") == "heading":
                entries.append({
                    "id": b["id"],
                    "level": b.get("level", 1),
                    "text": b.get("text", "").strip(),
                    "page": b.get("source_refs", [{}])[0].get("page", 1)
                })

    suspicious_words = {"perspective", "review", "article", "analysis", "commentary",
                        "brief communication", "letter", "述评", "综述", "文章", "评论", "快讯"}
    warnings = []
    lines = []
    for item in entries:
        txt = item["text"]
        norm_txt = re.sub(r"^[一二三四五六七八九十\d\.\s、]+", "", txt).strip().lower()
        is_suspicious = norm_txt in suspicious_words
        tag = " [WARN: 疑似页眉]" if is_suspicious else ""
        if is_suspicious:
            warnings.append(f"{item['id']} (p.{item['page']}): 疑似走马页眉 '{txt}' 被列为第 {item['level']} 级标题")
        indent = "  " * (item["level"] - 1)
        lines.append(f"{indent}- [p.{item['page']:02d}] [L{item['level']}] [{item['id']}] {txt}{tag}")

    return {
        "status": "warning" if warnings else "passed",
        "total_headings": len(entries),
        "warnings": warnings,
        "outline_tree": "\n".join(lines),
        "headings": entries
    }


def plan_split(temp_dir, target_chars=12000):
    root = Path(temp_dir).resolve()
    doc_path = root / "doc.json"
    if not doc_path.is_file():
        raise PipelineError("doc.json not found; run prepare first")
    doc = read_json(doc_path)
    blocks = doc.get("blocks", [])
    if not blocks:
        return {"status": "failed", "error": "doc.json has no blocks"}

    landmarks = []
    cum_chars = 0
    ref_started = False

    for i, b in enumerate(blocks):
        b_id = b.get("id", f"b{i+1:06d}")
        text = b.get("text", "").strip()
        kind = b.get("kind", "text")
        page = b.get("source_refs", [{}])[0].get("page", 1)
        b_len = len(text)

        is_ref = bool(b.get("is_reference")) or (
            kind == "heading" and bool(re.match(r"^(?:references?|bibliography|参考文献)\b", text, re.I))
        )
        if is_ref and not ref_started:
            ref_started = True
            landmarks.append({
                "id": b_id,
                "page": page,
                "kind": "references",
                "text": text[:80],
                "char_offset": cum_chars
            })
        elif kind == "heading":
            if not b.get("is_running_header") and b.get("style_id") != "running-header":
                landmarks.append({
                    "id": b_id,
                    "page": page,
                    "kind": "heading",
                    "text": text[:80],
                    "char_offset": cum_chars
                })
        cum_chars += b_len

    suggested_cuts = []
    last_cut_offset = 0

    for item in landmarks:
        offset_since_last = item["char_offset"] - last_cut_offset
        if item["kind"] == "references":
            if offset_since_last >= 500:
                suggested_cuts.append(item["id"])
                last_cut_offset = item["char_offset"]
        elif offset_since_last >= int(target_chars * 0.70):
            suggested_cuts.append(item["id"])
            last_cut_offset = item["char_offset"]

    tree_lines = []
    for item in landmarks:
        is_cut = " [SUGGESTED CUT]" if item["id"] in suggested_cuts else ""
        tree_lines.append(f"[{item['id']}] (p.{item['page']:02d}, offset {item['char_offset']}) {item['kind']}: {item['text']}{is_cut}")

    return {
        "status": "success",
        "total_blocks": len(blocks),
        "total_chars": cum_chars,
        "target_chars": target_chars,
        "estimated_chunks": len(suggested_cuts) + 1,
        "suggested_cuts": suggested_cuts,
        "outline_tree": "\n".join(tree_lines),
        "landmarks": landmarks
    }


def split_run(temp_dir, cuts=None, chunk_size=12000):
    root = Path(temp_dir).resolve()
    doc_path = root / "doc.json"
    if not doc_path.is_file():
        raise PipelineError(f"doc.json missing from {temp_dir}; run prepare first")
    doc = read_json(doc_path)

    r_state = run_state.load_run_state(str(root))
    if r_state.get("dispatches") or r_state.get("recorded"):
        raise PipelineError("Translation already in progress; cannot re-split chunks")

    if cuts:
        chunk_texts = split_document_by_cuts(doc, cuts)
        split_method = "agent-outline-cuts"
    else:
        plan = plan_split(temp_dir, target_chars=chunk_size)
        suggested = plan.get("suggested_cuts", [])
        if suggested:
            chunk_texts = split_document_by_cuts(doc, suggested)
            split_method = "agent-outline-cuts"
            cuts = suggested
        else:
            chunk_texts = split_document(doc, chunk_size)
            split_method = "block-boundary-v1"

    names = [f"chunk{index:04d}.md" for index in range(1, len(chunk_texts) + 1)]

    for old_file in root.glob("chunk*.md"):
        try:
            old_file.unlink()
        except OSError:
            pass

    for name, text in zip(names, chunk_texts):
        (root / name).write_text(text, encoding="utf-8")

    split_contract = {
        "method": split_method,
        "target_chars": chunk_size if not cuts else "custom",
        "cuts": cuts or [],
        "chunk_count": len(names),
        "structure_hash": doc["structure_hash"]
    }
    manifest.create_manifest(
        str(root),
        names,
        str(root / "input.md"),
        expected_block_ids=[b["id"] for b in doc["blocks"]],
        split_contract=split_contract,
        doc_path=root / "doc.json",
        expected_chunk_files=names
    )

    state_path = root / "pipeline_state.json"
    if state_path.is_file():
        state = read_json(state_path)
        state["chunks"] = len(names)
        write_json(state_path, state)

    return {
        "status": "success",
        "chunk_count": len(names),
        "chunk_files": names,
        "method": split_method,
        "cuts": cuts or []
    }


def bypass_references(temp_dir):
    """Automatically populate verbatim output for pure reference chunks to bypass translation LLM."""
    root = Path(temp_dir).resolve()
    doc_path = root / "doc.json"
    manifest_path = root / "manifest.json"
    if not doc_path.is_file() or not manifest_path.is_file():
        raise PipelineError("doc.json and manifest.json are required")
    doc = read_json(doc_path)
    ref_block_ids = {
        b["id"] for b in doc.get("blocks", [])
        if b.get("is_reference") or (
            b.get("kind") == "heading" and any(w in b.get("text", "").lower() for w in ("references", "参考文献", "bibliography"))
        )
    }

    if not ref_block_ids:
        in_refs = False
        for b in doc.get("blocks", []):
            txt = b.get("text", "").strip().lower()
            if b.get("kind") == "heading":
                if any(w in txt for w in ("references", "参考文献")):
                    in_refs = True
                elif in_refs and any(w in txt for w in ("acknowledgements", "致谢", "利益冲突", "附录")):
                    in_refs = False
            elif in_refs:
                ref_block_ids.add(b["id"])

    mf = read_json(manifest_path)
    bypassed_chunks = []
    for chunk in mf.get("chunks", []):
        cid = chunk["id"]
        source_path = root / chunk["source_file"]
        output_path = root / chunk["output_file"]
        meta_path = root / f"output_{cid}.meta.json"

        src_text = source_path.read_text(encoding="utf-8")
        chunk_bids = set(re.findall(r"^<!--\s*tb:(b\d+)\s*-->", src_text, re.M))
        
        # Safety gate: A chunk CANNOT be bypassed if it contains non-reference prose,
        # author affiliations, acknowledgments, competing interests, or supplementary info.
        src_lower = src_text.lower()
        has_end_matter = any(term in src_lower for term in (
            "acknowledg", "致谢", "利益冲突", "competing interest", "conflict of interest",
            "author contribution", "作者贡献", "author information", "作者信息",
            "affiliations", "supplementary information", "补充信息",
            "department of", "university", "institute of", "hospital"
        ))
        
        if chunk_bids and chunk_bids.issubset(ref_block_ids) and not has_end_matter:
            output_path.write_text(src_text, encoding="utf-8")
            empty_meta = {
                "schema_version": 1,
                "new_entities": [],
                "alias_hypotheses": [],
                "attribute_hypotheses": [],
                "used_term_sources": [],
                "conflicts": []
            }
            write_json(meta_path, empty_meta)
            bypassed_chunks.append(cid)

    if bypassed_chunks:
        run_state.record_chunks(str(root), bypassed_chunks, provenance="bypassed_reference")

    return {
        "status": "completed",
        "bypassed_chunks": bypassed_chunks,
        "count": len(bypassed_chunks)
    }


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


def extract_candidate_terms(doc, top_n=30):
    """Scan doc blocks for high-frequency domain acronyms and hyphenated compound terms."""
    COMMON_IGNORE = {
        "THE", "AND", "FOR", "WITH", "THAT", "THIS", "FROM", "THEIR", "WHICH", "WERE",
        "HAVE", "BEEN", "ALSO", "MORE", "SUCH", "THAN", "EACH", "WHEN", "INTO", "BOTH",
        "SOME", "THEN", "THESE", "THOSE", "ONLY", "MOST", "OVER", "AFTER", "BEFORE",
        "PDF", "URL", "HTML", "USA", "UK", "DOI", "HTTP", "HTTPS", "FIG", "REF", "PAGE",
        "ET", "AL", "VOL", "NO", "YES", "NOT", "CAN", "MAY", "USE", "NEW", "ONE", "TWO",
        "ALL", "ANY", "BUT", "ARE", "HAS", "HAD", "WAS", "OUT", "OFF", "PER", "VIA", "SEE"
    }

    full_text = " ".join(b.get("text", "") for b in doc.get("blocks", []) if not b.get("is_reference"))
    candidates = Counter()

    # 1. Acronyms / Uppercase compounds: e.g. SAR, PK/PD, HTS, ADMET, PROTAC, CNS, RNA, DNA, QSAR
    acronyms = re.findall(r'\b[A-Z0-9]{2,}(?:/[A-Z0-9]{2,})*\b', full_text)
    for a in acronyms:
        if a not in COMMON_IGNORE and len(a) >= 2 and not a.isdigit():
            candidates[a] += 1

    # 2. Technical hyphenated terms: e.g. hit-to-lead, high-throughput, machine-learning, structure-activity
    hyphenated = re.findall(r'\b[a-zA-Z]{2,}(?:-[a-zA-Z]{2,})+\b', full_text)
    for h in hyphenated:
        lower_h = h.lower()
        if lower_h not in {"state-of-the-art", "peer-reviewed", "well-known", "in-depth", "real-time"}:
            candidates[lower_h] += 1

    ranked = candidates.most_common(top_n)
    return [{"source": term, "frequency": count} for term, count in ranked]


def seed_glossary(temp_dir, top_n=30):
    """Seed glossary.json with candidate domain terms before translation dispatch."""
    root = Path(temp_dir).resolve()
    doc_path = root / "doc.json"
    if not doc_path.exists():
        raise PipelineError("doc.json not found; run prepare first")
    doc = read_json(doc_path)
    
    glossary_path = root / "glossary.json"
    if glossary_path.exists():
        gloss = read_json(glossary_path)
    else:
        gloss = {"version": 2, "terms": [], "high_frequency_top_n": 20, "applied_meta_hashes": {}}
    
    existing_sources = {t.get("source", "").lower(): t for t in gloss.get("terms", [])}
    candidates = extract_candidate_terms(doc, top_n=top_n)
    added = []
    
    for item in candidates:
        src = item["source"]
        if src.lower() not in existing_sources:
            new_term = {
                "id": src,
                "source": src,
                "target": "",  # To be curated by Agent or translation
                "category": "acronym" if src.isupper() else "compound",
                "aliases": [],
                "gender": "unknown",
                "confidence": "medium",
                "frequency": item["frequency"],
                "evidence_refs": [],
                "notes": "auto-seeded from high-frequency scan"
            }
            gloss.setdefault("terms", []).append(new_term)
            added.append(item)
    
    write_json(glossary_path, gloss)
    return {
        "status": "seeded",
        "glossary_path": str(glossary_path),
        "total_terms": len(gloss.get("terms", [])),
        "newly_added_count": len(added),
        "newly_added": added
    }


def patch_glossary_terms(temp_dir):
    """Surgically replace term aliases with canonical targets across all translated chunk files."""
    root = Path(temp_dir).resolve()
    glossary_path = root / "glossary.json"
    if not glossary_path.exists():
        return {"status": "skipped", "reason": "glossary.json not found", "patches": {}}
    
    gloss = read_json(glossary_path)
    terms = gloss.get("terms", [])
    
    replacements = []
    for term in terms:
        target = term.get("target", "").strip()
        aliases = term.get("aliases", [])
        if not target:
            continue
        for alias in aliases:
            alias = alias.strip()
            if alias and alias != target:
                replacements.append((alias, target, term.get("source", "")))
    
    if not replacements:
        return {"status": "unchanged", "message": "No aliases defined in glossary", "patches": {}}
    
    # Sort replacements by length descending so longer phrases match first
    replacements.sort(key=lambda x: len(x[0]), reverse=True)
    out_files = sorted(list(root.glob("output_chunk*.md")))
    patches = {}
    total_replaced = 0

    def _safe_replace(content, alias, target):
        # Protect comments <!-- ... -->, markdown images ![...](...), HTML tags <...>, code `...`
        pattern = re.compile(r'(<!--.*?-->|!\[.*?\]\(.*?\)|<[^>]+>|`[^`]+`)', re.DOTALL)
        parts = pattern.split(content)
        count = 0
        for i in range(0, len(parts), 2):
            occurrences = parts[i].count(alias)
            if occurrences:
                parts[i] = parts[i].replace(alias, target)
                count += occurrences
        return ''.join(parts), count

    for fpath in out_files:
        content = fpath.read_text(encoding="utf-8")
        orig_content = content
        file_patches = []
        for alias, target, source in replacements:
            new_content, count = _safe_replace(content, alias, target)
            if count > 0:
                content = new_content
                file_patches.append({"alias": alias, "target": target, "source": source, "count": count})
                total_replaced += count
        if content != orig_content:
            fpath.write_text(content, encoding="utf-8")
            patches[fpath.name] = file_patches

    # Update run_state output hashes if run_state.json exists
    state_path = root / "run_state.json"
    if state_path.exists() and patches:
        state = read_json(state_path)
        chunks = state.get("chunks", {})
        for fname in patches:
            chunk_id = fname.replace("output_", "").replace(".md", "")
            if chunk_id in chunks:
                chunks[chunk_id]["output_hash"] = digest_file(root / fname)
        write_json(state_path, state)

    return {
        "status": "patched" if total_replaced > 0 else "unchanged",
        "total_replacements": total_replaced,
        "files_modified": len(patches),
        "patches": patches
    }


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
    try:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(value, ensure_ascii=True, indent=2))


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
    prep.add_argument("--chunk-size", type=int, default=12000)
    ap = sub.add_parser("accept-parse")
    ap.add_argument("temp_dir")
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--evidence", required=True)
    sg = sub.add_parser("seed-glossary")
    sg.add_argument("temp_dir")
    sg.add_argument("--top-n", type=int, default=30)
    pt = sub.add_parser("patch-terms")
    pt.add_argument("temp_dir")
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
    for option in ("title", "author", "lang", "cover", "output-dir", "browser-path", "file-stem", "org-name", "doc-number", "journal", "doi"):
        bld.add_argument("--" + option)
    bld.add_argument("--legacy-aliases", action="store_true", help="Also generate backward-compatible book.html/book.pdf aliases")
    pub = sub.add_parser("accept-publish")
    pub.add_argument("temp_dir")
    pub.add_argument("--reviewer", required=True)
    pub.add_argument("--evidence", required=True)
    outl = sub.add_parser("outline")
    outl.add_argument("temp_dir")
    byp = sub.add_parser("bypass-refs")
    byp.add_argument("temp_dir")
    aud = sub.add_parser("audit-layout")
    aud.add_argument("temp_dir")
    stat = sub.add_parser("status")
    stat.add_argument("temp_dir")
    psplit = sub.add_parser("plan-split")
    psplit.add_argument("temp_dir")
    psplit.add_argument("--target-chunk-size", type=int, default=12000)
    spl = sub.add_parser("split")
    spl.add_argument("temp_dir")
    spl.add_argument("--cuts", nargs="*", default=None, help="Block IDs at which to cut chunks")
    spl.add_argument("--chunk-size", type=int, default=12000)
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
        elif args.cmd == "outline":
            value = outline(args.temp_dir)
        elif args.cmd == "seed-glossary":
            value = seed_glossary(args.temp_dir, top_n=args.top_n)
        elif args.cmd == "patch-terms":
            value = patch_glossary_terms(args.temp_dir)
        elif args.cmd == "bypass-refs":
            value = bypass_references(args.temp_dir)
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
                          browser_path=args.browser_path,
                          org_name=getattr(args, "org_name", None),
                          doc_number=getattr(args, "doc_number", None),
                          journal=getattr(args, "journal", None),
                          doi=getattr(args, "doi", None),
                          legacy_aliases=getattr(args, "legacy_aliases", False))
        elif args.cmd == "accept-publish":
            value = accept_publish(args.temp_dir, args.reviewer, args.evidence)
        elif args.cmd == "audit-layout":
            res = read_json(Path(args.temp_dir).resolve() / "build_result.json")
            glossary_path = Path(args.temp_dir).resolve() / "glossary.json"
            value = audit_layout.audit_publication_dir(Path(res["output_dir"]), glossary_path=glossary_path if glossary_path.is_file() else None)
        elif args.cmd == "status":
            value = status(args.temp_dir)
        elif args.cmd == "plan-split":
            value = plan_split(args.temp_dir, target_chars=args.target_chunk_size)
        elif args.cmd == "split":
            value = split_run(args.temp_dir, cuts=args.cuts, chunk_size=args.chunk_size)
        elif args.cmd == "cleanup":
            value = cleanup(args.temp_dir)
        _print(value)
        return 0
    except Exception as exc:
        _print({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
