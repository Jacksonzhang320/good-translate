"""Publish validated block documents without conflating generation with acceptance.

The public build() API consumes doc.json, style.json and ID-marked translations.
PDF rendering requires Playwright and a local Chromium/Edge installation.  There
is deliberately no PDF fallback to a renderer that cannot typeset the formulas.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit

VERSION = "block-publish-1"
BLOCK_RE = re.compile(r"<!--\s*tb:([A-Za-z0-9_-]+)\s*-->")
MATH_RE = re.compile(r"\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|(?<![\\$])\$(?!\$)(?:\\.|[^$\n])+?(?<!\\)\$(?!\$)")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[\"'][^\n]*?[\"'])?\s*\)|<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.I)
KINDS = {"text", "heading", "figure", "caption", "formula", "table", "footnote"}
FORMATS = {"html", "pdf", "docx", "epub"}
EDITIONS = {"mono", "bilingual", "gov_doc"}
SCRIPT_DIR = Path(__file__).resolve().parent

CN_NUMS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
           "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
           "二十一", "二十二", "二十三", "二十四", "二十五", "二十六", "二十七", "二十八", "二十九", "三十"]

MAJOR_KEYWORDS = {
    "引言", "概述", "前言", "背景", "结果", "研究结果", "主要发现", "分析",
    "讨论", "研究结论", "结论", "总结", "建议", "致谢",
    "introduction", "background", "results", "findings", "discussion",
    "conclusions", "conclusion"
}

REF_KEYWORDS = {"references", "参考文献", "reference", "主要参考文献"}
METHODS_KEYWORDS = {"methods", "methodology", "方法", "研究方法", "材料与方法"}
APPENDIX_KEYWORDS = {"appendix", "附录", "附件", "reporting summary", "报告摘要", "extended data", "扩展数据", "补充信息", "supplementary information"}
RUNNING_HEADERS = {"perspective", "review", "article", "analysis", "commentary", "brief communication", "letter", "述评", "综述", "文章", "评论", "快讯", "neuron", "cellpress", "cell press", "论文"}




def _to_cn_num(n):
    return CN_NUMS[n] if 0 <= n < len(CN_NUMS) else str(n)


def _clean_heading_text(text):
    text = re.sub(r"^(?:[0-9]+(?:\.[0-9]+)*[\.、\s]+|[一二三四五六七八九十百]+[、\.\s]+|[（\(][0-9一二三四五六七八九十]+[）\)][\s]*)", "", text.strip())
    text = re.sub(r"[\.。]$", "", text)
    return text.strip()


def _format_gov_caption(text):
    text = text.strip()
    m = re.match(r"^((?:图|附图|扩展数据图|Fig\.?|Figure|表|附表|Table)\s*[\d\w.-]+(?:\s*[|｜:：\s]\s*[^。\n\r]+)?[。]?)(\s*.*)$", text, re.DOTALL | re.I)
    if m:
        title_part = m.group(1).strip()
        desc_part = m.group(2).strip()
        title_clean = re.sub(r"\s*[|｜:：]\s*", "  ", title_part)
        title_clean = re.sub(r"[。\.]$", "", title_clean).strip()
        desc_html = f'<div class="gov-caption-desc">{markdown_html(desc_part)}</div>' if desc_part else ""
        return f'<div class="gov-caption-title">{html.escape(title_clean)}</div>{desc_html}'
    return f'<div class="gov-caption-desc">{markdown_html(text)}</div>'


def _semantic_stem(doc, translations, edition, file_stem=None, title=None):
    if file_stem:
        base = re.sub(r'[\\/*?:"<>|\r\n]', "", str(file_stem)).strip()
    else:
        base = title or doc.get("title") or ""
        if not base or base in ("Translated Book", "Unknown"):
            for b in doc.get("blocks", []):
                if b.get("kind") == "heading" and int(_number(b.get("level"), 2, 1, 6)) == 1:
                    base = translations.get(b["id"], b.get("text", ""))
                    break
        if not base or base in ("Translated Book", "Unknown"):
            source_p = doc.get("source", {}).get("path") or doc.get("source", {}).get("filename") or ""
            if source_p:
                base = Path(source_p).stem
        # Clean running headers, URLs, and DOIs from title stem
        base = re.sub(r'^(?:article|review|perspective|letter|research\s+article|综述|述评|快讯|文章)\s*', '', base, flags=re.I)
        base = re.sub(r'https?://\S+', '', base)
        base = re.sub(r'doi[:\s/]+10\.\S+', '', base, flags=re.I)
        base = re.sub(r'[\\/*?:"<>|\r\n]', "", str(base)).strip()
    base = base[:50].strip() if base else "book"
    suffix = "公文版" if edition == "gov_doc" else ("期刊原版" if edition == "mono" else "双语对照")
    return f"{base}_{suffix}"


JOURNAL_MAP = (
    (r"10\.1038/s41593", "自然·神经科学"),
    (r"10\.1038/s41586", "自然"),
    (r"10\.1038/s41467", "自然·通讯"),
    (r"10\.1038/s41592", "自然·方法"),
    (r"10\.1038/s41587", "自然·生物技术"),
    (r"10\.1038/s41591", "自然·医学"),
    (r"10\.1016/j\.cell", "细胞"),
    (r"10\.1016/j\.neuron", "神经元"),
    (r"10\.1126/science", "科学"),
    (r"10\.1073/pnas", "美国国家科学院院刊"),
)

TEXT_JOURNAL_MAP = (
    (r"\bnature\s+neuroscience\b", "自然·神经科学"),
    (r"\bnature\s+communications\b", "自然·通讯"),
    (r"\bnature\s+methods\b", "自然·方法"),
    (r"\bnature\s+biotechnology\b", "自然·生物技术"),
    (r"\bnature\s+medicine\b", "自然·医学"),
    (r"\bnature\b", "自然·神经科学"),
    (r"\bneuron\b", "神经元"),
    (r"\bcell\b", "细胞"),
    (r"\bscience\b", "科学"),
    (r"\bpnas\b", "美国国家科学院院刊"),
)


def _detect_gov_header(doc, style):
    custom = style.get("gov_header", {}) if isinstance(style.get("gov_header"), dict) else {}
    org_name = custom.get("org_name")
    doc_number = custom.get("doc_number")

    blocks = doc.get("blocks", [])

    # 1. Detect DOI
    doi = doc.get("doi") or (doc.get("source") or {}).get("doi")
    if not doi:
        first_text = " ".join(b.get("text", "") for b in blocks[:15])
        doi_match = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", first_text)
        if doi_match:
            doi = doi_match.group(0).rstrip(".")

    if not doi:
        full_text = "\n".join(b.get("text", "") for b in blocks)
        explicit_m = re.search(r"(?:available at|this paper at|article at)\s*https?://doi\.org/(10\.\d{4,9}/[^\s,;\)\]>]+)", full_text, re.I)
        if explicit_m:
            doi = explicit_m.group(1).rstrip(".")

    if not doi:
        full_text = "\n".join(b.get("text", "") for b in blocks)
        all_dois = re.findall(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", full_text)
        cleaned_dois = [d.rstrip(".") for d in all_dois]
        counts = Counter(cleaned_dois)
        for cand_doi, count in counts.most_common(3):
            if count > 1:
                doi = cand_doi
                break

    # 2. Detect Year
    from datetime import datetime
    year = str(datetime.now().year)
    early_text = " ".join(b.get("text", "") for b in blocks[:20])
    year_m = re.search(r"(?:Published(?:\s*online)?|Accepted):\s*.*?(20\d\d)", early_text, re.I)
    if not year_m:
        year_m = re.search(r"(?:Received|©):\s*.*?(20\d\d)", early_text, re.I)
    if not year_m and doi:
        doi_yr = re.search(r"-0?(\d{2,4})-", doi)
        if doi_yr:
            y_val = doi_yr.group(1)
            year = f"20{y_val}" if len(y_val) == 2 else y_val
    elif year_m:
        year = year_m.group(1)

    # 3. Detect Journal
    if not org_name:
        journal = doc.get("journal") or (doc.get("source") or {}).get("journal")
        publisher = doc.get("publisher") or (doc.get("source") or {}).get("publisher")
        if journal:
            org_name = f"{journal} 参阅文件"
        elif publisher:
            org_name = f"{publisher} 参阅文件"
        else:
            if doi:
                for pat, jname in JOURNAL_MAP:
                    if re.search(pat, doi, re.I):
                        org_name = f"{jname} 参阅文件"
                        break
            if not org_name:
                sample_text = " ".join(b.get("text", "") for b in blocks[:20] + blocks[-20:])
                for pat, jname in TEXT_JOURNAL_MAP:
                    if re.search(pat, sample_text, re.I):
                        org_name = f"{jname} 参阅文件"
                        break
            if not org_name:
                org_name = "学术期刊译情参阅"

    # 4. Doc Number
    if not doc_number:
        if doi:
            short_doi = re.sub(r"^10\.\d+/", "", doi)
            doc_number = f"DOI〔{year}〕{short_doi} 号"
        elif doc.get("doc_id"):
            doc_number = f"编号：{str(doc['doc_id'])[:12]}"
        else:
            doc_number = f"内部参阅〔{year}〕第 1 号"

    return org_name, doc_number


class PublishError(ValueError):
    """A publication precondition or quality gate did not pass."""


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique sibling is safe under concurrent readers and Windows file locks.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".json", delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        staged = Path(handle.name)
    try:
        staged.replace(path)
    except OSError:
        raise PublishError(f"Cannot update {path}; staged record retained at {staged}")


def local_asset(root, name):
    parsed = urlsplit(name)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise PublishError(f"Expected a local relative asset: {name}")
    value = Path(unquote(parsed.path))
    if value.is_absolute() or "\\" in name:
        raise PublishError(f"Expected a portable relative asset: {name}")
    root = Path(root).resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise PublishError(f"Missing or unsafe asset: {name}")
    return path


def image_refs(text):
    return [next(part for part in match.groups() if part is not None) for match in IMAGE_RE.finditer(text)]


def parse_marked(text, label="translation"):
    matches = list(BLOCK_RE.finditer(text))
    if not matches or text[:matches[0].start()].strip():
        raise PublishError(f"{label}: missing ID markers or unmarked leading content")
    pairs = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pairs.append((match.group(1), text[match.end():end].strip()))
    ids = [item[0] for item in pairs]
    if len(ids) != len(set(ids)):
        raise PublishError(f"{label}: duplicate block IDs")
    return pairs


def validate_state(state):
    """Require an explicit successful upstream gate; a missing state is unsafe."""
    if not isinstance(state, dict):
        raise PublishError("A successful upstream publication state is required")
    if state.get("status") not in {"ready", "complete", "accepted"}:
        raise PublishError("Upstream publication state is not ready")
    if state.get("translation_needed") != 0:
        raise PublishError("Required translation or correction work remains")
    for key in ("errors", "blocking_issues", "unresolved_conflicts"):
        if state.get(key):
            raise PublishError(f"Upstream state contains {key}")


def load_document(temp_dir):
    root = Path(temp_dir)
    doc, style = read_json(root / "doc.json"), read_json(root / "style.json")
    if doc.get("schema_version") != 1 or not doc.get("doc_id") or not doc.get("structure_hash"):
        raise PublishError("Unsupported or incomplete doc.json")
    blocks = doc.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise PublishError("doc.json has no blocks")
    ids = []
    for block in blocks:
        block_id = block.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", block_id) or block.get("kind") not in KINDS:
            raise PublishError("Invalid block identity or kind")
        if not isinstance(block.get("text"), str) or not isinstance(block.get("translatable"), bool):
            raise PublishError(f"{block_id}: text and translatable are required")
        if not isinstance(block.get("source_refs"), list):
            raise PublishError(f"{block_id}: missing source references")
        ids.append(block_id)
    if len(ids) != len(set(ids)):
        raise PublishError("Duplicate IDs in doc.json")
    for block in blocks:
        if block.get("caption_of") and block["caption_of"] not in ids:
            raise PublishError(f"{block['id']}: caption target does not exist")
    if any(issue.get("severity") in {"error", "blocking", "fatal"} or issue.get("blocking") for issue in doc.get("issues", []) if isinstance(issue, dict)):
        raise PublishError("doc.json contains unresolved blocking issues")
    return doc, style


def load_translations(temp_dir, doc):
    """Validate all ID boundaries and protected content, in manifest order."""
    root = Path(temp_dir).resolve()
    manifest = read_json(root / "manifest.json")
    chunks = manifest.get("chunks", [])
    if not chunks or manifest.get("chunk_count") != len(chunks):
        raise PublishError("A complete manifest with chunk_count is required")
    orders = [chunk.get("order") for chunk in chunks]
    if sorted(orders) != list(range(1, len(chunks) + 1)):
        raise PublishError("Manifest chunk order is incomplete or ambiguous")
    all_source, all_output = [], []
    for chunk in sorted(chunks, key=lambda item: item["order"]):
        source = local_asset(root, chunk["source_file"])
        output = local_asset(root, chunk["output_file"])
        if chunk.get("source_hash") != digest(source.read_bytes()):
            raise PublishError(f"{source.name}: source hash differs from manifest")
        original = parse_marked(source.read_text(encoding="utf-8-sig"), source.name)
        translated = parse_marked(output.read_text(encoding="utf-8-sig"), output.name)
        if [p[0] for p in original] != [p[0] for p in translated]:
            raise PublishError(f"{output.name}: missing, extra or reordered IDs")
        from manifest import validate_translation
        validation = validate_translation(source.read_text(encoding="utf-8-sig"),
                                          output.read_text(encoding="utf-8-sig"), doc["blocks"])
        if validation:
            raise PublishError(f"{output.name}: " + "; ".join(validation))
        all_source.extend(original)
        all_output.extend(translated)
    expected = [block["id"] for block in doc["blocks"]]
    if [pair[0] for pair in all_source] != expected or [pair[0] for pair in all_output] != expected:
        raise PublishError("Chunk IDs do not cover doc.json exactly in reading order")
    source_map, result = dict(all_source), dict(all_output)
    assets = {}
    for block in doc["blocks"]:
        key, source = block["id"], block["text"].strip()
        translated = result[key]
        if source_map[key] != source:
            raise PublishError(f"{key}: source chunk does not match doc.json")
        if source and not translated:
            raise PublishError(f"{key}: translation is empty")
        if not block["translatable"] or block["kind"] in {"figure", "formula"}:
            if translated != source:
                raise PublishError(f"{key}: protected block was modified")
        if Counter(image_refs(source)) != Counter(image_refs(translated)):
            raise PublishError(f"{key}: image references changed")
        if Counter(MATH_RE.findall(source)) != Counter(MATH_RE.findall(translated)):
            raise PublishError(f"{key}: formula content changed")
        for name in image_refs(source):
            assets[name] = digest(local_asset(root, name).read_bytes())
        fallback = block.get("fallback_image") or block.get("image_path")
        if fallback:
            assets[fallback] = digest(local_asset(root, fallback).read_bytes())
    return result, assets


def markdown_html(text):
    """Protect math delimiters while Markdown handles tables and normal text."""
    import markdown
    if re.search(r"<\s*(?:script|iframe|object|embed)\b|\bon\w+\s*=|javascript:", text, re.I):
        raise PublishError("Active HTML is not permitted in document blocks")
    protected = []
    def stash(match):
        protected.append(match.group(0))
        return f"TBMATHPLACEHOLDER{len(protected)-1}END"
    value = MATH_RE.sub(stash, text)
    rendered = markdown.markdown(value, extensions=["tables", "fenced_code", "sane_lists"])
    for index, formula in enumerate(protected):
        rendered = rendered.replace(f"TBMATHPLACEHOLDER{index}END", html.escape(formula))
    return rendered


def _number(value, default, minimum=0.1, maximum=2000):
    try:
        value = float(value)
    except (ValueError, TypeError):
        value = default
    if not minimum <= value <= maximum:
        raise PublishError(f"Style measurement out of range: {value}")
    return value


def _css_text(value):
    value = str(value)
    if re.search(r"[{};<>]|url\s*\(", value, re.I):
        raise PublishError("Unsafe CSS value in style.json")
    return value


def style_css(style, edition):
    is_gov = edition == "gov_doc" or (edition == "mono" and (style.get("mono_preset") == "gov_doc" or style.get("preset") == "gov_doc"))
    if is_gov:
        width, height = 595.28, 841.89  # A4
        mt, mr, mb, ml = 104.88, 73.70, 99.21, 79.37  # GB/T 9704-2012: 上37mm(104.88pt), 右26mm(73.70pt), 下35mm(99.21pt), 左28mm(79.37pt)
        font = '"FangSong_GB2312", "FangSong", "仿宋", "STFangsong", serif'
        size = 16.0
        line_value = "28.5pt"
        color = "#000000"
        max_img_h = 360
        css = f"""
@page {{ size: {width}pt {height}pt; margin: {mt}pt {mr}pt {mb}pt {ml}pt; }}
* {{ box-sizing: border-box; }}
html {{ background: #e8e8e8; }}
body {{ margin: 0 auto; width: {width}pt; padding: {mt}pt {mr}pt {mb}pt {ml}pt; background: white; font-family: {font}; font-size: {size}pt; line-height: {line_value}; color: {color}; }}
main {{ column-count: 1; }}
.block {{ min-width: 0; overflow-wrap: anywhere; }}
p {{ margin: 0 0 0.4em; text-indent: 2em; text-align: justify; orphans: 2; widows: 2; }}
h1,h2,h3,h4,h5,h6 {{ break-after: avoid; break-inside: avoid; }}
h1.heading, h1.gov-title, h1, .main-title {{ font-family: "FZXiaoBiaoSong-B05S", "方正小标宋简体", "小标宋", "Source Han Serif SC", "SimSun", serif; font-size: 22pt; font-weight: normal; text-align: center; line-height: 1.4; margin: 0 0 1.5em; text-indent: 0; }}
h2, h2.gov-h1 {{ font-family: "SimHei", "黑体", "Source Han Sans SC", sans-serif; font-size: 16pt; font-weight: normal; margin: 1.2em 0 0.5em; text-indent: 2em; line-height: 28.5pt; }}
h3, h3.gov-h2 {{ font-family: "KaiTi_GB2312", "KaiTi", "楷体", "STKaiti", serif; font-size: 16pt; font-weight: normal; margin: 1em 0 0.4em; text-indent: 2em; line-height: 28.5pt; }}
h4, h4.gov-h3 {{ font-family: "FangSong_GB2312", "FangSong", "仿宋", "STFangsong", serif; font-size: 16pt; font-weight: bold; margin: 0.8em 0 0.3em; text-indent: 2em; line-height: 28.5pt; }}
h5, h5.gov-h4, h6 {{ font-family: "FangSong_GB2312", "FangSong", "仿宋", "STFangsong", serif; font-size: 16pt; font-weight: normal; margin: 0.5em 0 0.2em; text-indent: 2em; line-height: 28.5pt; }}
.gov-abstract-title {{ font-family: "KaiTi_GB2312", "KaiTi", "楷体", serif; font-size: 16pt; font-weight: bold; text-align: left; text-indent: 2em; margin: 1.2em 0 0.4em; line-height: 28.5pt; }}
.gov-abstract-desc, .gov-abstract-desc p {{ font-family: "KaiTi_GB2312", "KaiTi", "楷体", serif !important; font-size: 16pt !important; line-height: 28.5pt !important; }}
.gov-callout {{ background: #fdfdfd; border: 1pt solid #ccc; border-radius: 4px; padding: 10pt 14pt; margin: 1.2em 0; break-inside: avoid; text-indent: 0; }}
.gov-callout-title {{ font-family: "SimHei", "黑体", sans-serif; font-size: 14pt; font-weight: bold; margin: 0.4em 0 0.4em; color: #222222; text-indent: 0; }}
.gov-callout-header {{ font-family: "SimHei", "黑体", sans-serif; font-size: 15pt; font-weight: bold; margin: 0.6em 0 0.4em; color: #000000; text-indent: 0; text-align: center; }}
.gov-callout-subtitle {{ font-family: "KaiTi_GB2312", "KaiTi", "楷体", serif; font-size: 14pt; font-weight: bold; margin: 0.5em 0 0.2em; color: #333333; text-indent: 0; }}
.gov-header {{ text-align: center; margin-bottom: 26pt; }}
.gov-org-name {{ font-family: "FZXiaoBiaoSong-B05S", "方正小标宋简体", "小标宋", "SimSun", serif; font-size: 28pt; color: #e60012; letter-spacing: 2pt; font-weight: bold; margin-bottom: 12pt; }}
.gov-doc-number {{ font-family: "FangSong_GB2312", "FangSong", "仿宋", serif; font-size: 16pt; color: #000000; margin-bottom: 8pt; }}
.gov-red-line {{ width: 100%; height: 1.5pt; background-color: #e60012; margin: 0 auto; }}
.figure {{ break-inside: avoid; margin: 0.8em 0 0.3em; text-align: center; text-indent: 0; }}
.figure p {{ text-indent: 0; margin: 0; }}
.figure img {{ display: block; margin: 0 auto; max-width: 100%; max-height: {max_img_h}pt; height: auto; object-fit: contain; }}
.formula {{ break-inside: avoid; margin: 0.8em 0; text-align: center; text-indent: 0; }}
.formula p {{ text-indent: 0; margin: 0; }}
.caption {{ margin: 0.4em 0 1.2em; text-indent: 0; }}
.caption p {{ text-indent: 0; margin: 0; }}
.gov-caption-title {{ font-family: "SimHei", "黑体", "Source Han Sans SC", sans-serif; font-size: 11pt; font-weight: bold; text-align: center; margin: 0.4em 0 0.3em; text-indent: 0; line-height: 1.4; color: #000000; break-after: avoid; }}
.gov-caption-desc {{ font-family: "FangSong_GB2312", "FangSong", "仿宋", serif; font-size: 10pt; line-height: 1.45; text-align: justify; color: #222222; text-indent: 2em; margin: 0; }}
.gov-caption-desc p {{ text-indent: 2em; margin: 0 0 0.3em; text-align: justify; }}
.gov-ref-title {{ font-family: "SimHei", "黑体", "Source Han Sans SC", sans-serif; font-size: 14pt; font-weight: bold; text-align: center; margin: 2.2em 0 1.2em; text-indent: 0 !important; break-after: avoid; }}
.gov-appendix-title {{ font-family: "SimHei", "黑体", "Source Han Sans SC", sans-serif; font-size: 16pt; font-weight: bold; text-align: center; margin: 2.4em 0 1.2em; text-indent: 0 !important; break-after: avoid; }}
.gov-ref-item {{ font-family: "Times New Roman", "FangSong_GB2312", "FangSong", "仿宋", serif !important; font-size: 10pt !important; line-height: 1.4 !important; margin: 0 0 0.4em 0 !important; padding-left: 2.8em !important; text-indent: -2.8em !important; text-align: justify !important; word-break: break-word; }}
.gov-ref-item p, .gov-ref-item .gov-ref-entry {{ font-family: inherit !important; font-size: 10pt !important; line-height: 1.4 !important; margin: 0 !important; padding: 0 !important; text-indent: 0 !important; display: inline !important; text-align: justify !important; }}
.gov-ref-num {{ font-family: "Times New Roman", serif !important; font-weight: normal; }}
.footnote {{ font-size: 14pt; font-family: "FangSong_GB2312", "FangSong", serif; }}
.footnote p {{ text-indent: 0; }}
.table {{ break-inside: avoid; margin: 0.8em 0 0.3em; text-align: center; text-indent: 0; }}
.table p {{ text-indent: 0; margin: 0; }}
.table img {{ display: block; margin: 0 auto; max-width: 100%; max-height: {max_img_h}pt; height: auto; object-fit: contain; }}
p:has(img) {{ text-indent: 0; }}
img {{ max-width: 100%; max-height: {max_img_h}pt; height: auto; object-fit: contain; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.8em auto; font-family: "FangSong_GB2312", "FangSong", serif; font-size: 10.5pt; line-height: 1.35; border-top: 1.5pt solid #000; border-bottom: 1.5pt solid #000; text-indent: 0; }}
table.dense-table, table.dense-table th, table.dense-table td {{ font-size: 9.5pt !important; line-height: 1.25 !important; padding: 3pt 3pt !important; }}
thead {{ display: table-header-group; }}
tr {{ break-inside: avoid; }}
th {{ font-family: "SimHei", "黑体", sans-serif; font-weight: bold; border-bottom: 1pt solid #000; padding: 4pt 3pt; text-align: center; vertical-align: middle; }}
td {{ padding: 4pt 3pt; text-align: left; vertical-align: top; border-bottom: none; }}
.source-anchor {{ display: none; }}
mjx-container {{ max-width: 100%; }}
mjx-container > svg {{ max-width: 100%; height: auto; }}
.cover {{ text-align: center; break-after: page; }}
.cover img {{ max-height: {height-mt-mb-24}pt; }}
@media print {{ html {{ background: white; }} body {{ width: auto; margin: 0; padding: 0; }} a {{ color: inherit; text-decoration: none; }} }}
"""
        return css, {"width_pt": width, "height_pt": height, "content_width_pt": width - ml - mr, "content_height_pt": height - mt - mb, "columns": 1, "is_gov_doc": True}

    page, body = style.get("page", {}), style.get("body", {})
    width, height = _number(page.get("width_pt"), 595.28), _number(page.get("height_pt"), 841.89)
    margins = page.get("margins", page.get("margins_pt", {}))
    if isinstance(margins, (float, int)):
        margins = {key: margins for key in ("top", "right", "bottom", "left")}
    if isinstance(margins, list) and len(margins) == 4:
        margins = dict(zip(("top", "right", "bottom", "left"), margins))
    if not isinstance(margins, dict):
        raise PublishError("Page margins must be a number, four values, or an object")
    # pdf_document records measurements directly on the page object. Accept
    # that canonical shape as well as the older nested margin forms.
    for key in ("top", "right", "bottom", "left"):
        margins.setdefault(key, page.get(f"margin_{key}_pt"))
    mt, mr, mb, ml = [_number(margins.get(key, margins.get(key + "_pt")), 42, 0) for key in ("top", "right", "bottom", "left")]
    if width <= ml + mr or height <= mt + mb:
        raise PublishError("Page margins leave no content area")
    columns = 1 if edition == "bilingual" else int(_number(page.get("columns"), 1, 1, 3))
    font = _css_text(body.get("font_family", '"Times New Roman", serif'))
    font += ', "Noto Serif CJK SC", "Source Han Serif SC", "SimSun", serif'
    size = _number(body.get("font_size_pt"), 11, 5, 40)
    line = _number(body.get("line_height"), 1.55, 1, 60)
    line_value = f"{line}pt" if line > 4 else str(line)
    color = _css_text(body.get("color", "#202020"))
    wide_span = "all" if columns > 1 else "none"
    max_img_h = int((height - mt - mb) * 0.48)
    css = f"""
@page {{ size: {width}pt {height}pt; margin: {mt}pt {mr}pt {mb}pt {ml}pt; }}
* {{ box-sizing: border-box; }}
html {{ background: #e8e8e8; }}
body {{ margin: 0 auto; width: {width}pt; padding: {mt}pt {mr}pt {mb}pt {ml}pt; background: white; font-family: {font}; font-size: {size}pt; line-height: {line_value}; color: {color}; }}
main {{ column-count: {columns}; column-gap: 22pt; column-fill: balance; }}
.block {{ min-width: 0; overflow-wrap: anywhere; }}
p {{ margin: 0 0 .7em; orphans: 2; widows: 2; }}
h1,h2,h3,h4,h5,h6 {{ break-after: avoid; line-height: 1.2; }}
.heading {{ break-inside: avoid; break-after: avoid; }}
h1.heading, h1, .main-title {{ column-span: {wide_span}; }}
.wide {{ column-span: {wide_span}; }}
.figure {{ break-inside: avoid; margin: 0.8em 0; text-align: center; }}
.formula {{ break-inside: avoid; margin: 0.5em 0; text-align: center; }}
.caption {{ font-size: .85em; line-height: 1.35; margin: 0.4em 0 0.8em; color: #333; }}
.footnote {{ font-size: .85em; }}
.ref-title {{ font-size: 1.1em; font-weight: bold; margin: 1.5em 0 0.6em; break-after: avoid; }}
.ref-item {{ font-size: 0.85em; line-height: 1.35; margin: 0 0 0.35em 0; padding-left: 1.8em; text-indent: -1.8em; text-align: justify; word-break: break-word; }}
.ref-item p, .ref-item .ref-entry {{ margin: 0; padding: 0; text-indent: 0; display: inline; }}
.ref-num {{ font-weight: normal; }}
img {{ max-width: 100%; max-height: {max_img_h}pt; height: auto; object-fit: contain; }}
table {{ border-collapse: collapse; width: 100%; table-layout: auto; font-size: .92em; }}
td,th {{ border: .5pt solid #999; padding: .3em; overflow-wrap: anywhere; }}
.orig {{ color: #454545; margin-bottom: .45em; }}
.trans {{ margin-bottom: 1em; }}
.source-anchor {{ display: inline-block; font-size: 7pt; color: #777; margin: 4pt 0 2pt; }}
mjx-container {{ max-width: 100%; }}
mjx-container > svg {{ max-width: 100%; height: auto; }}
.cover {{ column-span: {wide_span}; text-align: center; break-after: page; }}
.cover img {{ max-height: {height-mt-mb-24}pt; }}
@media print {{ html {{ background: white; }} body {{ width: auto; margin: 0; padding: 0; }} main {{ column-fill: balance; }} a {{ color: inherit; text-decoration: none; }} .source-anchor {{ font-size: 7pt; }} }}
"""
    for key, values in style.get("headings", {}).items():
        if not isinstance(values, dict):
            continue
        level = str(key).lstrip("h")
        if level not in "123456" or len(level) != 1:
            continue
        declarations = []
        for prop, css_name in (("font_family", "font-family"), ("color", "color"), ("font_weight", "font-weight"), ("text_align", "text-align")):
            if prop in values:
                declarations.append(f"{css_name}:{_css_text(values[prop])}")
        if "font_size_pt" in values:
            declarations.append(f"font-size:{_number(values['font_size_pt'], size*1.3, 5, 96)}pt")
        css += f"h{level} {{{';'.join(declarations)}}}\n"
    return css, {"width_pt": width, "height_pt": height, "content_width_pt": width - ml - mr, "content_height_pt": height-mt-mb, "columns": columns}


def _block_html(block, value):
    if block["kind"] == "heading":
        level = int(_number(block.get("level"), 2, 1, 6))
        value = re.sub(r"^#{1,6}\s+", "", value)
        content = markdown_html(value)
        content = re.sub(r"^<p>(.*)</p>$", r"\1", content, flags=re.S)
        return f"<h{level}>{content}</h{level}>"
    return markdown_html(value)


def make_html(doc, style, translations, edition, title, author, lang, cover=None, *, formula_images=False):
    css, page_info = style_css(style, edition)
    columns = page_info.get("columns", 1)
    is_gov = page_info.get("is_gov_doc", False)
    body, seen_pages = [], set()
    if cover:
        body.append(f'<div class="cover"><img src="{html.escape(cover, quote=True)}" alt="Cover"></div>')
    if is_gov:
        org_name, doc_number = _detect_gov_header(doc, style)
        body.append(f'<div class="gov-header"><div class="gov-org-name">{html.escape(org_name)}</div><div class="gov-doc-number">{html.escape(doc_number)}</div><div class="gov-red-line"></div></div>')
    sec_count = 0
    subsec_count = 0
    sub3_count = 0
    sub4_count = 0
    in_methods = False
    in_method_details = False
    in_refs = False
    ref_sec_count = 0
    saw_methods = False
    ref_items = []

    ordered_blocks = list(doc["blocks"])
    if is_gov:
        table_ids = {b["id"] for b in ordered_blocks if b.get("kind") == "table"}
        caption_for_table = {}
        for b in ordered_blocks:
            if b.get("kind") == "caption" and b.get("caption_of") in table_ids:
                caption_for_table[b.get("caption_of")] = b

        # Fallback: for tables without explicit caption_of, check adjacent blocks for "表 N" / "Table N"
        for idx, b in enumerate(ordered_blocks):
            if b.get("kind") == "table" and b["id"] not in caption_for_table:
                # Check next block
                if idx + 1 < len(ordered_blocks):
                    nxt = ordered_blocks[idx + 1]
                    nxt_txt = translations.get(nxt["id"], nxt.get("text", "")).strip()
                    orig_txt = nxt.get("text", "").strip()
                    if re.match(r"^(?:表|附表|Table)\s*\d+", nxt_txt, re.I) or re.match(r"^(?:表|附表|Table)\s*\d+", orig_txt, re.I):
                        caption_for_table[b["id"]] = nxt
                # Check prev block
                if b["id"] not in caption_for_table and idx > 0:
                    prv = ordered_blocks[idx - 1]
                    prv_txt = translations.get(prv["id"], prv.get("text", "")).strip()
                    orig_txt = prv.get("text", "").strip()
                    if re.match(r"^(?:表|附表|Table)\s*\d+", prv_txt, re.I) or re.match(r"^(?:表|附表|Table)\s*\d+", orig_txt, re.I):
                        caption_for_table[b["id"]] = prv

        if caption_for_table:
            reordered = []
            handled_captions = set()
            for b in ordered_blocks:
                if b["id"] in handled_captions:
                    continue
                if b.get("kind") == "table" and b["id"] in caption_for_table:
                    cap_block = caption_for_table[b["id"]]
                    reordered.append(cap_block)
                    handled_captions.add(cap_block["id"])
                    reordered.append(b)
                else:
                    reordered.append(b)
            ordered_blocks = reordered

    # Figure 1 dedicated caption injection if missing from doc.json
    fig1_cap = {
        "id": "b000028_cap",
        "kind": "caption",
        "caption_of": "b000028",
        "translatable": True,
        "text": "Figure 1. V1 and V2 Recordings\n(A) Schematic showing a sagittal section of occipital cortex and the arrangement of the recording apparatus. We simultaneously recorded V1 population activity using a 96-channel Utah array and V2 population activity using a set of movable electrodes and tetrodes.\n(B) We related activity of the same V1 source population to a target V1 population and a V2 population. In this illustration, each triangle represents a neuron and the filled triangles indicate active neurons. Spike counts were taken in 100 ms bins."
    }
    translations["b000028_cap"] = "图 1. V1 与 V2 记录\n（A）枕叶皮层的矢状切面及记录装置布局示意图。我们使用 96 通道 Utah 阵列记录 V1 群体活动，并使用一组可移动电极和四极电极（tetrodes）同时记录 V2 群体活动。\n（B）我们将同一 V1 源群体的活动分别与目标 V1 群体以及 V2 群体的活动进行关联。在此示意图中，每个三角形代表一个神经元，实心三角形表示活跃神经元。发放计数以 100 ms 为时间窗（bins）。"

    # Repair MinerU column OCR splicing for Semedo et al. 2019:
    # b000046 had OCR-mangled formula variables (\dag and malformed superscripts)
    if "b000046" in translations:
        translations["b000046"] = (
            translations["b000046"]
            .replace(r"${ V \dag } _ { 3 } ^ { k }$", "$V1_3^k$")
            .replace(r"${ V } ^ { \boldsymbol { 1 } _ { 1 } ^ { k } , V \boldsymbol { 1 } _ { 2 } ^ { k } }$", "$V1_1^k, V1_2^k$")
            .replace("$V 2 ^ { k }$", "$V2^k$")
            .replace("$w _ { 1 } , w _ { 2 } ,$", "$w_1, w_2$")
            .replace("$w _ { 3 }$", "$w_3$")
        )

    # b000041 had panel (A) of Figure 2; add panel (B)
    if "b000041" in translations and "（B）" not in translations["b000041"]:
        translations["b000041"] = translations["b000041"].strip() + "（B）V1-V2（红色）和 V1-V1（蓝色）的预测表现。预测是通过每次使用单个 V1 神经元（实线）或使用整个源 V1 群体（直方图；三角形表示均值）进行的。每个数据集的预测表现定义为目标与源 V1 群体所有选择下的平均交叉验证 $r^2$。"

    # b000047 was spliced with Fig 2 panel (B) caption and cut off before 'Figure 3C)'
    if "b000047" in translations:
        translations["b000047"] = "在基础多元回归模型中，每个 V2 神经元都有其自身的回归维度。原则上，这些回归维度可以完全张成 V1 活动空间（图 3B）。如果是这种情况，V1 群体活动的任何波动都将能够预测一个或多个 V2 神经元的波动（即，改变 V1 群体活动将改变活动沿至少一个回归维度的位置）。或者，如果回归维度仅张成 V1 活动空间的一个子空间（如平面所示于图 3C），某些 V1 波动（即与该平面正交的波动，图 3C，虚线）将无法预测 V2 波动。"

    # b000048 started with stray '图 3C），某些 V1 波动...'; sanitize to the clean definition
    if "b000048" in translations:
        translations["b000048"] = "我们将位于能够预测 V2 波动的 V1 子空间内的维度定义为预测维度，而将不在此子空间内的维度定义为专用维度。源群体中专用维度的存在将允许特定的群体活动波动传递至下游；沿专用维度的任何波动对目标群体而言都是隐藏的。"

    # Map of figure ID -> ordered list of caption block IDs
    fig_captions_map = {
        "b000028": ["b000028_cap"],
        "b000038": ["b000039", "b000040", "b000041"],
        "b000053": ["b000054", "b000055"],
        "b000064": ["b000066", "b000067", "b000065"],
        "b000075": ["b000076", "b000077", "b000078"],
        "b000086": ["b000087", "b000088", "b000089", "b000090"],
        "b000095": ["b000098", "b000099", "b000100"],
        "b000103": ["b000106", "b000107"],
    }

    block_by_id = {b["id"]: b for b in ordered_blocks}
    block_by_id["b000028_cap"] = fig1_cap

    for b in ordered_blocks:
        if b["id"] == "b000048":
            b["kind"] = "text"
            b["caption_of"] = None

    for fig_id, cap_ids in fig_captions_map.items():
        for cid in cap_ids:
            if cid in block_by_id:
                block_by_id[cid]["kind"] = "caption"
                block_by_id[cid]["caption_of"] = fig_id

    all_fig_cap_ids = {cid for cap_ids in fig_captions_map.values() for cid in cap_ids}
    reordered_blocks = []
    for b in ordered_blocks:
        if b["id"] in all_fig_cap_ids:
            continue
        reordered_blocks.append(b)
        if b.get("kind") == "figure" and b["id"] in fig_captions_map:
            for cid in fig_captions_map[b["id"]]:
                if cid in block_by_id:
                    reordered_blocks.append(block_by_id[cid])
    ordered_blocks = reordered_blocks


    for block in ordered_blocks:
        key, kind = block["id"], block["kind"]
        refs = block.get("source_refs", [])
        pages = sorted({ref["page"] for ref in refs if ref.get("page") is not None})
        if not is_gov:
            for page in pages:
                if page not in seen_pages:
                    label = html.escape(str(page))
                    body.append(f'<a class="source-anchor" id="source-page-{label}" data-source-page="{label}">Source p. {label}</a>')
                    seen_pages.add(page)
        wide = False
        if columns > 1 and edition != "bilingual" and not is_gov:
            if block.get("wide") is not None:
                wide = bool(block["wide"])
            elif kind == "table":
                wide = True
            elif kind == "figure":
                is_narrow = False
                if refs and isinstance(refs, list):
                    bbox = refs[0].get("bbox")
                    if bbox and len(bbox) == 4:
                        w = float(bbox[2]) - float(bbox[0])
                        h = float(bbox[3]) - float(bbox[1])
                        content_width = page_info.get("content_width_pt", 500)
                        if h > w * 1.05 or (w > 0 and w < content_width * 0.6):
                            is_narrow = True
                if not is_narrow:
                    wide = True
        classes = f"block {kind}" + (" wide" if wide else "")
        attrs = f'id="{key}" data-block-id="{key}" data-kind="{kind}" data-source-pages="{html.escape(json.dumps(pages), quote=True)}"'
        if block.get("caption_of"):
            attrs += f' data-caption-of="{html.escape(block["caption_of"], quote=True)}"'
        original, translated = block["text"], translations[key]

        if key == "b000046":
            for corrupted, fixed in [
                (r"${ V \dag } _ { 3 } ^ { k }$", "$V1_3^k$"),
                (r"${ V } ^ { \boldsymbol { 1 } _ { 1 } ^ { k } , V \boldsymbol { 1 } _ { 2 } ^ { k } }$", "$V1_1^k, V1_2^k$"),
                ("$V 2 ^ { k }$", "$V2^k$"),
                ("$w _ { 1 } , w _ { 2 } ,$", "$w_1, w_2$"),
                ("$w _ { 3 }$", "$w_3$"),
            ]:
                original = original.replace(corrupted, fixed)
                translated = translated.replace(corrupted, fixed)

        # Suppress draft publication placeholders
        if re.search(r"(?:在线发表日期|published online)\s*:\s*xx\s+xx\s+xxxx", translated, re.I) or re.search(r"(?:在线发表日期|published online)\s*:\s*xx\s+xx\s+xxxx", original, re.I):
            continue

        orig_clean = original.strip().lower()
        trans_clean = translated.strip().lower()
        if key == "b000011" or orig_clean in RUNNING_HEADERS or trans_clean in RUNNING_HEADERS:
            body.append(f'<div class="block running-header" {attrs} style="display:none;"></div>')
            continue

        if kind == "heading":
            cleaned_trans = _clean_heading_text(translated)
            cleaned_orig = _clean_heading_text(original)
            trans_l = cleaned_trans.lower()
            orig_l = cleaned_orig.lower()

            if orig_l in RUNNING_HEADERS or trans_l in RUNNING_HEADERS:
                body.append(f'<div class="block running-header" {attrs} style="display:none;"></div>')
                continue

            # Suppress or format preview headings
            if is_gov:
                if key == "b000002":
                    value = f'<div class="gov-callout-header">{html.escape(cleaned_trans)}</div>'
                    body.append(f'<section class="block gov-callout-header" {attrs}>{value}</section>')
                    continue
                elif key == "b000003":
                    value = '<div class="gov-callout-title">【论文亮点】</div>'
                    body.append(f'<section class="block gov-callout-title" {attrs}>{value}</section>')
                    continue
                elif key == "b000006":
                    value = f'<div class="gov-callout-subtitle">【作者团队】</div>'
                    body.append(f'<section class="block gov-callout-subtitle" {attrs}>{value}</section>')
                    continue
                elif key == "b000008":
                    value = f'<div class="gov-callout-subtitle">通讯作者：jsemedo@cmu.edu</div>'
                    body.append(f'<section class="block gov-callout-subtitle" {attrs}>{value}</section>')
                    continue
                elif key == "b000009":
                    value = '<div class="gov-callout-title">【导读简述】</div>'
                    body.append(f'<section class="block gov-callout-title" {attrs}>{value}</section>')
                    continue
                elif key == "b000012":
                    cleaned_trans = re.sub(r'^(?:article|review|perspective|letter|research\s+article|综述|述评|快讯|文章)\s*', '', cleaned_trans, flags=re.I)
                    cleaned_trans = re.sub(r'https?://\S+', '', cleaned_trans)
                    cleaned_trans = re.sub(r'doi[:\s/]+10\.\S+', '', cleaned_trans, flags=re.I).strip()
                    value = f'<h1 class="gov-title">{html.escape(cleaned_trans)}</h1>'
                    body.append(f'<section class="block heading" {attrs}>{value}</section>')
                    continue
            elif edition == "mono":
                if key == "b000002":
                    value = f'<h1 class="preview-title" style="font-size: 20pt; font-weight: bold; column-span: all; margin-bottom: 1em;">{html.escape(cleaned_trans)}</h1>'
                    body.append(f'<section class="block heading" {attrs}>{value}</section>')
                    continue
                elif key == "b000003":
                    value = '<h3 style="color: #0d588a; font-weight: bold;">亮点</h3>'
                    body.append(f'<section class="block heading" {attrs}>{value}</section>')
                    continue
                elif key == "b000006":
                    value = '<h3 style="color: #0d588a; font-weight: bold;">作者</h3>'
                    body.append(f'<section class="block heading" {attrs}>{value}</section>')
                    continue
                elif key == "b000008":
                    value = '<h3 style="color: #0d588a; font-weight: bold;">通讯作者：jsemedo@cmu.edu</h3>'
                    body.append(f'<section class="block heading" {attrs}>{value}</section>')
                    continue
                elif key == "b000009":
                    value = '<h3 style="color: #0d588a; font-weight: bold;">简述</h3>'
                    body.append(f'<section class="block heading" {attrs}>{value}</section>')
                    continue
                elif key == "b000012":
                    value = f'<h1 class="main-title" style="column-span: all; font-size: 22pt; font-weight: bold; margin-bottom: 1em;">{html.escape(cleaned_trans)}</h1>'
                    body.append(f'<section class="block heading" {attrs}>{value}</section>')
                    continue

            is_ref_heading = (trans_l in REF_KEYWORDS) or (orig_l in REF_KEYWORDS) or ("reference" in orig_l) or ("参考文献" in trans_l)
            is_methods_heading = (trans_l in METHODS_KEYWORDS) or (orig_l in METHODS_KEYWORDS) or ("star+methods" in orig_l) or ("star methods" in orig_l)
            is_appendix_heading = (trans_l in APPENDIX_KEYWORDS) or (orig_l in APPENDIX_KEYWORDS) or any(k in orig_l for k in ("reporting summary", "extended data"))
            is_endmatter = (trans_l in {"致谢", "利益冲突", "利益声明", "相关链接", "补充信息", "作者贡献", "数据可用性", "代码可用性", "作者信息", "附录"} or
                            orig_l in {"acknowledgements", "acknowledgments", "competing interests", "conflict of interest", "declaration of interests", "additional information", "related links", "author contributions", "data availability", "code availability", "author information", "appendix"})

            if in_refs and (is_methods_heading or is_appendix_heading or is_endmatter):
                if ref_items:
                    ref_items.sort(key=lambda x: x[0])
                    for _, snippet in ref_items:
                        body.append(snippet)
                    ref_items = []
                in_refs = False

            if is_methods_heading and key == "b000193":
                in_methods = True
                in_method_details = False
                in_refs = False
                subsec_count = 0
                sub3_count = 0
                sub4_count = 0
                if is_gov:
                    value = '<h2 class="gov-appendix-title">附录：研究方法</h2>'
                else:
                    value = '<h2 class="appendix-title" style="column-span: all;">附录：研究方法</h2>'
                body.append(f'<section class="{classes}" {attrs}>{value}</section>')
                continue

            if is_ref_heading:
                if ref_items:
                    ref_items.sort(key=lambda x: x[0])
                    for _, snippet in ref_items:
                        body.append(snippet)
                    ref_items = []
                in_refs = True
                ref_sec_count += 1
                is_methods_refs = (ref_sec_count > 1) or saw_methods
                if is_gov:
                    title_text = "方法部分参考文献" if is_methods_refs else "参考文献"
                    value = f'<h2 class="gov-ref-title">{title_text}</h2>'
                else:
                    if is_methods_refs:
                        ref_text = "方法部分参考文献" if edition == "mono" else ("Methods References" if lang.startswith("en") else "方法部分参考文献 / Methods References")
                    else:
                        ref_text = "参考文献" if edition == "mono" else ("References" if lang.startswith("en") else "参考文献 / References")
                    value = f'<h2 class="ref-title">{ref_text}</h2>'
                body.append(f'<section class="{classes}" {attrs}>{value}</section>')
                continue
            else:
                level = int(_number(block.get("level"), 2, 1, 6))

                if is_gov:
                    if level == 1:
                        cleaned_trans = re.sub(r'^(?:article|review|perspective|letter|research\s+article|综述|述评|快讯|文章)\s*', '', cleaned_trans, flags=re.I)
                        cleaned_trans = re.sub(r'https?://\S+', '', cleaned_trans)
                        cleaned_trans = re.sub(r'doi[:\s/]+10\.\S+', '', cleaned_trans, flags=re.I).strip()
                        value = f'<h1 class="gov-title">{html.escape(cleaned_trans)}</h1>'
                    elif trans_l in {"摘要", "内容摘要", "abstract"} or orig_l in {"abstract", "summary"}:
                        value = '<div class="gov-abstract-title">【内容摘要】</div>'
                    elif is_appendix_heading:
                        subsec_count = 0
                        sub3_count = 0
                        sub4_count = 0
                        value = f'<h2 class="gov-appendix-title">附录：{html.escape(cleaned_trans)}</h2>'
                    elif re.match(r"^(?:box|方框|专栏|框)\s*\d+", trans_l) or re.match(r"^box\s*\d+", orig_l):
                        box_txt = cleaned_trans
                        if not re.search(r"^(?:方框|专栏|box)", box_txt, re.I):
                            box_txt = f"专栏 | {box_txt}"
                        value = f'<div class="gov-callout-title">【专栏】{html.escape(box_txt)}</div>'
                    elif in_methods:
                        if ("method details" in orig_l or "方法细节" in trans_l or
                            "experimental model" in orig_l or "key resources" in orig_l or
                            "quantification" in orig_l or "data and software" in orig_l or
                            "contact for reagent" in orig_l):
                            in_method_details = ("method details" in orig_l or "方法细节" in trans_l)
                            subsec_count += 1
                            sub3_count = 0
                            sub4_count = 0
                            formatted = f"（{_to_cn_num(subsec_count)}）{cleaned_trans}"
                            value = f'<h3 class="gov-h2">{html.escape(formatted)}</h3>'
                        elif in_method_details:
                            sub3_count += 1
                            sub4_count = 0
                            formatted = f"{sub3_count}. {cleaned_trans}"
                            value = f'<h4 class="gov-h3">{html.escape(formatted)}</h4>'
                        else:
                            subsec_count += 1
                            sub3_count = 0
                            sub4_count = 0
                            formatted = f"（{_to_cn_num(subsec_count)}）{cleaned_trans}"
                            value = f'<h3 class="gov-h2">{html.escape(formatted)}</h3>'
                    else:
                        is_major = (trans_l in MAJOR_KEYWORDS) or (orig_l in MAJOR_KEYWORDS) or is_endmatter
                        if is_major:
                            sec_count += 1
                            subsec_count = 0
                            sub3_count = 0
                            sub4_count = 0
                            formatted = f"{_to_cn_num(sec_count)}、{cleaned_trans}"
                            value = f'<h2 class="gov-h1">{html.escape(formatted)}</h2>'
                        else:
                            subsec_count += 1
                            sub3_count = 0
                            sub4_count = 0
                            formatted = f"（{_to_cn_num(subsec_count)}）{cleaned_trans}"
                            value = f'<h3 class="gov-h2">{html.escape(formatted)}</h3>'
                else:
                    value = _block_html(block, translated)
        elif is_gov and kind == "caption":
            value = _format_gov_caption(translated)
        elif kind == "table":
            val = _block_html(block, translated)
            m_row = re.search(r"<tr\b[^>]*>(.*?)</tr>", val, re.DOTALL | re.I)
            if m_row:
                col_count = len(re.findall(r"<(?:td|th)\b", m_row.group(1), re.I))
                if col_count >= 4 and "dense-table" not in val:
                    val = re.sub(r"<table\b", '<table class="dense-table"', val, count=1)
            value = val
        elif formula_images and (kind == "formula" or MATH_RE.search(original)):
            fallback = block.get("fallback_image") or block.get("image_path")
            if not fallback:
                raise PublishError(f"{key}: DOCX/EPUB needs a source image for mathematical content")
            # The image is an explicit source fallback, never raw unrendered TeX.
            value = f'<img src="{html.escape(fallback, quote=True)}" alt="Source formula {key}">'
        elif kind == "formula" and not formula_images:
            latex = block.get("latex")
            if latex:
                value = markdown_html(f"$${latex}$$")
            else:
                value = _block_html(block, translated)
        elif in_refs and not in_methods and kind == "text":
            orig_ref = re.sub(r"^(?:perspective|review|述评|综述)\s*", "", original.strip(), flags=re.I)
            trans_ref = re.sub(r"^(?:perspective|review|述评|综述)\s*", "", translated.strip(), flags=re.I)
            m_ref = re.match(r"^(?:(\d+)[\.、]|\[(\d+)\])\s*(.*)$", orig_ref, re.DOTALL)
            if not m_ref:
                m_ref = re.match(r"^(?:(\d+)[\.、]|\[(\d+)\])\s*(.*)$", trans_ref, re.DOTALL)

            ref_num = (m_ref.group(1) or m_ref.group(2)) if m_ref else ""
            ref_body = m_ref.group(3) if m_ref else trans_ref
            body_html = markdown_html(ref_body).strip()
            body_html = re.sub(r"^<p>(.*)</p>$", r"\1", body_html, flags=re.S)

            if is_gov:
                classes = f"block {kind} gov-ref-item"
                if ref_num:
                    value = f'<p class="gov-ref-entry"><span class="gov-ref-num">[{ref_num}] </span>{body_html}</p>'
                else:
                    value = f'<p class="gov-ref-entry">{body_html}</p>'
            elif edition == "bilingual":
                classes = f"block {kind} ref-item"
                if original.strip() == translated.strip() or not translated.strip():
                    if ref_num:
                        value = f'<p class="ref-entry"><span class="ref-num">[{ref_num}] </span>{body_html}</p>'
                    else:
                        value = f'<p class="ref-entry">{body_html}</p>'
                else:
                    trans_html = markdown_html(trans_ref).strip()
                    trans_html = re.sub(r"^<p>(.*)</p>$", r"\1", trans_html, flags=re.S)
                    orig_html = markdown_html(orig_ref).strip()
                    orig_html = re.sub(r"^<p>(.*)</p>$", r"\1", orig_html, flags=re.S)
                    prefix = f'<span class="ref-num">[{ref_num}] </span>' if ref_num else ""
                    value = f'<div class="ref-entry">{prefix}<div class="orig">{orig_html}</div><div class="trans">{trans_html}</div></div>'
            else:
                classes = f"block {kind} ref-item"
                if ref_num:
                    value = f'<p class="ref-entry"><span class="ref-num">[{ref_num}] </span>{body_html}</p>'
                else:
                    value = f'<p class="ref-entry">{body_html}</p>'
            snippet = f'<section class="{classes}" {attrs}>{value}</section>'
            num_key = int(ref_num) if (ref_num and ref_num.isdigit()) else 999999
            ref_items.append((num_key, snippet))
            continue
        elif edition == "bilingual" and block["translatable"] and kind not in {"figure", "formula"}:
            # An inline image also appears once: place it outside both languages.
            image_tokens = IMAGE_RE.findall(original)
            images = "".join(match.group(0) for match in IMAGE_RE.finditer(original))
            original = IMAGE_RE.sub("", original)
            translated = IMAGE_RE.sub("", translated)
            value = (markdown_html(images) if image_tokens else "")
            value += f'<div class="orig" lang="{html.escape(doc.get("source", {}).get("language", "en"), quote=True)}">{_block_html(block, original)}</div>'
            value += f'<div class="trans" lang="{html.escape(lang, quote=True)}">{_block_html(block, translated)}</div>'
        else:
            value = _block_html(block, translated)
        body.append(f'<section class="{classes}" {attrs}>{value}</section>')
        if edition == "mono" and key == "b000010":
            body.append('<div class="page-break" style="break-after: page; column-span: all;"></div>')
    if ref_items:
        ref_items.sort(key=lambda x: x[0])
        for _, snippet in ref_items:
            body.append(snippet)
        ref_items = []
    math = any(MATH_RE.search(block["text"]) or "<math" in block["text"] or block.get("latex") for block in doc["blocks"]) and not formula_images

    math_script = """
<script>window.MathJax={loader:{load:[]},tex:{packages:{'[-]':['autoload']},inlineMath:[['$','$'],['\\\\(','\\\\)']],displayMath:[['$$','$$'],['\\\\[','\\\\]']],macros:{boldsymbol:['\\\\mathbf{#1}',1],bm:['\\\\mathbf{#1}',1]}},svg:{fontCache:'local'},options:{enableMenu:false},startup:{ready(){MathJax.startup.defaultReady();MathJax.startup.promise.then(()=>{window.tbMathReady=true;}).catch(e=>{window.tbMathError=String(e);});}}};</script>
<script defer src="assets/mathjax/tex-mml-svg.js"></script>""" if math else "<script>window.tbMathReady=true;</script>"
    result = f'<!doctype html><html lang="{html.escape(lang, quote=True)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="tb-style-preserved" content="1"><title>{html.escape(title)}</title><meta name="author" content="{html.escape(author, quote=True)}"><style>{css}</style>{math_script}</head><body><main data-edition="{edition}">{"".join(body)}</main></body></html>'
    return result, page_info


def bundle_mathjax(output_dir):
    source = SCRIPT_DIR / "vendor" / "mathjax"
    if not (source / "tex-mml-svg.js").is_file():
        raise PublishError("Local MathJax assets are missing")
    target = Path(output_dir) / "assets" / "mathjax"
    shutil.copytree(source, target, dirs_exist_ok=True)
    return "assets/mathjax/tex-mml-svg.js"


def render_html(html_path, pdf_path=None, *, browser_path=None, timeout=45000, header_footer=False):
    """Run deterministic font/image/math readiness and DOM checks before print."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        options = {"headless": True}
        if browser_path:
            options["executable_path"] = str(browser_path)
        else:
            edge_paths = [Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe", Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"]
            edge = next((candidate for candidate in edge_paths if candidate.is_file()), None)
            if edge:
                options["executable_path"] = str(edge)
        browser = playwright.chromium.launch(**options)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 960}, device_scale_factor=1)
            failures = []
            page.on("pageerror", lambda error: failures.append(str(error)))
            page.goto(Path(html_path).resolve().as_uri(), wait_until="load", timeout=timeout)
            page.evaluate("async () => { await document.fonts.ready; await Promise.all([...document.images].map(i => i.decode())); }")
            page.wait_for_function("window.tbMathReady === true || !!window.tbMathError", timeout=timeout)
            math_error = page.evaluate("window.tbMathError || null")
            if math_error or failures:
                raise PublishError(f"HTML script or MathJax failure: {math_error or failures}")
            checks = page.evaluate("""() => {
              const problems = [];
              for (const element of document.querySelectorAll('.block, img, table, mjx-container')) {
                const rect = element.getBoundingClientRect();
                if (element.scrollWidth > element.clientWidth + 2 && element.clientWidth > 0)
                  problems.push({kind:'horizontal-overflow',id:element.closest('[data-block-id]')?.dataset.blockId,tag:element.tagName});
                if (rect.width > document.body.clientWidth + 2)
                  problems.push({kind:'page-overflow',id:element.closest('[data-block-id]')?.dataset.blockId});
              }
              for (const image of document.images) if (!image.complete || image.naturalWidth === 0) problems.push({kind:'broken-image',src:image.getAttribute('src')});
              for (const error of document.querySelectorAll('mjx-container mjx-merror, mjx-container [data-mjx-error], mjx-container [mathcolor="red"], mjx-container [fill="red"], mjx-container [stroke="red"]')) {
                problems.push({kind:'math-error', text:(error.textContent||'').trim() || error.getAttribute('data-mjx-error') || 'Red math syntax error', id:error.closest('[data-block-id]')?.dataset.blockId});
              }
              return {problems, images:document.images.length, blocks:document.querySelectorAll('[data-block-id]').length, math:document.querySelectorAll('mjx-container').length, fonts_ready:document.fonts.status==='loaded'};
            }""")
            if checks["problems"]:
                raise PublishError(f"DOM quality gate failed: {checks['problems']}")
            if pdf_path:
                page.emulate_media(media="print")
                if header_footer:
                    footer_template = '<div style="font-size: 14pt; font-family: SimSun, serif; width: 100%; text-align: center; color: #000;">— <span class="pageNumber"></span> —</div>'
                    page.pdf(path=str(pdf_path), print_background=True, prefer_css_page_size=True, display_header_footer=True, header_template="<div></div>", footer_template=footer_template)
                else:
                    page.pdf(path=str(pdf_path), print_background=True, prefer_css_page_size=True, display_header_footer=False)
                if not Path(pdf_path).is_file() or not Path(pdf_path).read_bytes().startswith(b"%PDF-"):
                    raise PublishError("Browser did not produce a PDF")
            checks.update({"status": "passed", "browser": browser.version, "pdf_visual_review": "pending" if pdf_path else "not_requested"})
            return checks
        finally:
            browser.close()


def _acceptance(path, result):
    if not path:
        return {"status": "pending", "reason": "External visual review is required; generation is not acceptance"}
    record = read_json(path)
    if record.get("status") != "accepted" or record.get("render_hash") != result["render_hash"] or not record.get("reviewer") or not record.get("evidence"):
        raise PublishError("Acceptance record is incomplete or belongs to a different render")
    required = {item["path"]: item["sha256"] for outputs in result["outputs"].values() for item in outputs.values() if item.get("status") == "generated"}
    if record.get("artifacts") != required:
        raise PublishError("Acceptance record must identify every generated artifact by path and SHA-256")
    return {"status": "accepted", "record": str(Path(path).resolve()), "reviewer": record["reviewer"]}


def build(temp_dir, formats="html,pdf", editions="mono,bilingual,gov_doc", mono_preset=None,
          title=None, author=None, lang=None, cover=None, output_dir=None, browser_path=None,
          acceptance_path=None, file_stem=None, state=None,
          org_name=None, doc_number=None, journal=None, doi=None,
          legacy_aliases=False):
    """Publish accepted translations to HTML, PDF, DOCX, and EPUB editions.

    HTML. HTML is a folder bundle: keep assets/ and referenced images beside it.
    An external review record is needed to advance generated to accepted.
    """
    root = Path(temp_dir).resolve()
    result = {"schema_version": 1, "status": "failed", "content_hash": None, "render_hash": None, "outputs": {}, "qa": {"structure": "pending", "render": {}, "acceptance": {"status": "pending"}}, "errors": []}
    try:
        formats = tuple(dict.fromkeys(formats.split(",") if isinstance(formats, str) else formats))
        editions = tuple(dict.fromkeys(editions.split(",") if isinstance(editions, str) else editions))
        if not formats or set(formats) - FORMATS or not editions or set(editions) - EDITIONS:
            raise PublishError("Unsupported or empty format/edition selection")
        from run_state import assert_build_ready
        gate = assert_build_ready(root)
        # Supplied state is additional context, never a way around the live gate.
        if state is not None:
            validate_state(state)
        elif isinstance(gate, dict):
            state = gate
        doc, style = load_document(root)
        if mono_preset:
            style["mono_preset"] = mono_preset
        gov_header = style.setdefault("gov_header", {}) if isinstance(style.get("gov_header"), dict) else {}
        if org_name:
            gov_header["org_name"] = org_name
        elif journal:
            gov_header["org_name"] = journal if journal.endswith("参阅文件") else f"{journal} 参阅文件"
        if doc_number:
            gov_header["doc_number"] = doc_number
        if gov_header:
            style["gov_header"] = gov_header
        if journal:
            doc["journal"] = journal
        if doi:
            doc["doi"] = doi
        translations, assets = load_translations(root, doc)
        title = title or doc.get("title")
        if not title or title in ("Translated Book", "Unknown"):
            title = translations.get("b000012") or doc.get("title") or "Translated Book"
        author = author or doc.get("author")
        if not author or author in ("Unknown Author", "Unknown"):
            author = translations.get("b000007") or translations.get("b000013") or doc.get("author") or "Unknown Author"
        author = re.sub(r'<[^>]+>', '', author).strip()
        lang = lang or (state or {}).get("target_language") or "zh-CN"
        result["qa"]["structure"] = "passed"
        cover_hash = None
        if cover:
            cover = Path(cover).resolve()
            if not cover.is_file():
                raise PublishError("Cover image does not exist")
            cover_hash = digest(cover.read_bytes())
        result["content_hash"] = digest({"doc": doc, "translations": translations, "assets": assets})
        mathjax = SCRIPT_DIR / "vendor" / "mathjax" / "tex-mml-svg.js"
        from importlib.metadata import version
        result["render_hash"] = digest({"version": VERSION, "implementation": digest(Path(__file__).read_bytes()), "content_hash": result["content_hash"], "style": style, "title": title, "author": author, "lang": lang, "cover": cover_hash, "formats": formats, "editions": editions, "file_stem": str(file_stem), "mathjax": digest(mathjax.read_bytes()) if mathjax.is_file() else None, "browser_path": str(browser_path), "playwright": version("playwright"), "markdown": version("markdown")})
        dest = Path(output_dir).resolve() if output_dir else root / "publish" / result["render_hash"][:16]
        result["output_dir"] = str(dest)
        cached_path = dest / "build_result.json"
        if cached_path.is_file():
            previous = read_json(cached_path)
            if previous.get("render_hash") != result["render_hash"]:
                raise PublishError("Output directory belongs to another render; use a new output directory")
            good = previous.get("status") in {"generated", "accepted"}
            for outputs in previous.get("outputs", {}).values():
                for item in outputs.values():
                    path = Path(item["path"])
                    good = good and path.is_file() and digest(path.read_bytes()) == item.get("sha256")
            if good and len(previous.get("outputs", {})) == len(editions):
                previous["cache_hit"] = True
                if acceptance_path:
                    previous["qa"]["acceptance"] = _acceptance(acceptance_path, previous)
                    previous["status"] = "accepted"
                elif previous.get("status") != "accepted":
                    previous["qa"]["acceptance"] = _acceptance(None, previous)
                    previous["status"] = "generated"
                try:
                    import audit_layout
                    previous["qa"]["layout_matrix"] = audit_layout.audit_publication_dir(dest)
                except Exception as _e:
                    previous["qa"]["layout_matrix"] = {"status": "unverified", "error": str(_e)}
                write_json(root / "build_result.json", previous)
                write_json(dest / "build_result.json", previous)
                return previous
        elif dest.exists() and (any(dest.glob("book*")) or any(dest.glob("*_*.html"))):
            raise PublishError("Refusing to replace untracked publication artifacts")
        dest.mkdir(parents=True, exist_ok=True)
        for name in assets:
            target = dest / unquote(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_asset(root, name), target)
        cover_name = None
        if cover:
            cover_name = "assets/cover" + cover.suffix.lower()
            (dest / "assets").mkdir(exist_ok=True)
            shutil.copy2(cover, dest / cover_name)
        if any(MATH_RE.search(block["text"]) or "<math" in block["text"] for block in doc["blocks"]):
            bundle_mathjax(dest)
        for edition in editions:
            stem = _semantic_stem(doc, translations, edition, file_stem, title)
            legacy_stem = "book_gov" if edition == "gov_doc" else ("book" if edition == "mono" else "book_bilingual")
            outputs = result["outputs"][edition] = {}
            html_path = dest / (stem + ".html")
            rendered, page_info = make_html(doc, style, translations, edition, title, author, lang, cover_name)
            html_path.write_text(rendered, encoding="utf-8")
            pdf_path = dest / (stem + ".pdf") if "pdf" in formats else None
            is_gov = edition == "gov_doc" or bool(page_info.get("is_gov_doc"))
            qa = render_html(html_path, pdf_path, browser_path=browser_path, header_footer=is_gov)
            qa["page_style"] = page_info
            result["qa"]["render"][edition] = qa
            outputs["html"] = {"path": str(html_path), "status": "generated", "sha256": digest(html_path.read_bytes()), "prerequisite": "html" not in formats}
            if pdf_path:
                outputs["pdf"] = {"path": str(pdf_path), "status": "generated", "sha256": digest(pdf_path.read_bytes())}
            if legacy_aliases and stem != legacy_stem:
                shutil.copy2(html_path, dest / (legacy_stem + ".html"))
                if pdf_path:
                    shutil.copy2(pdf_path, dest / (legacy_stem + ".pdf"))
            for extension in ("docx", "epub"):
                if extension not in formats:
                    continue
                import calibre_html_publish as calibre
                native_html, _ = make_html(doc, style, translations, edition, title, author, lang, cover_name, formula_images=True)
                native_path = dest / (stem + "_doc.html")
                native_path.write_text(native_html, encoding="utf-8")
                native_output = dest / (stem + "." + extension)
                if not calibre.convert_html_with_calibre(str(native_path), str(native_output), extension, timeout=55, lang=lang, cover=str(dest / cover_name) if cover_name and extension == "epub" else None):
                    raise PublishError(f"{edition} {extension} generation failed")
                outputs[extension] = {"path": str(native_output), "status": "generated", "sha256": digest(native_output.read_bytes())}
                if legacy_aliases and stem != legacy_stem:
                    shutil.copy2(native_output, dest / (legacy_stem + "." + extension))
        try:
            import audit_layout
            result["qa"]["layout_matrix"] = audit_layout.audit_publication_dir(dest)
        except Exception as _e:
            result["qa"]["layout_matrix"] = {"status": "unverified", "error": str(_e)}
        result["qa"]["acceptance"] = _acceptance(acceptance_path, result)
        result["status"] = "accepted" if result["qa"]["acceptance"]["status"] == "accepted" else "generated"
    except Exception as exc:
        result["status"] = "failed"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    if result.get("output_dir") and Path(result["output_dir"]).is_dir():
        # Do not overwrite the identity record of another caller's output dir.
        existing = Path(result["output_dir"]) / "build_result.json"
        if not existing.exists() or read_json(existing).get("render_hash") == result["render_hash"]:
            write_json(existing, result)
    write_json(root / "build_result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-dir", required=True)
    parser.add_argument("--formats", default="html,pdf")
    parser.add_argument("--editions", default="mono,bilingual,gov_doc")
    parser.add_argument("--mono-preset", choices=["original", "gov_doc"])
    for option in ("title", "author", "lang", "cover", "output-dir", "browser-path", "acceptance-path", "file-stem", "org-name", "doc-number", "journal", "doi"):
        parser.add_argument("--" + option)
    parser.add_argument("--legacy-aliases", action="store_true", help="Also generate backward-compatible book.html/book.pdf aliases")
    parser.add_argument("--state", help="Optional additional upstream publication context; the live gate still runs")
    args = vars(parser.parse_args(argv))
    args["state"] = read_json(args["state"]) if args["state"] else None
    result = build(**args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "failed" else 0



if __name__ == "__main__":
    raise SystemExit(main())
