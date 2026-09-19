#!/usr/bin/env python3
"""Audit layout, typography, and reference consistency in published publications."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup


SUSPICIOUS_RUNNING_HEADERS = {
    "perspective", "review", "article", "analysis", "commentary",
    "brief communication", "letter", "research article",
    "述评", "综述", "文章", "评论", "快讯", "通讯"
}

ALLOWED_END_MATTER = {
    "致谢", "利益冲突", "补充信息", "相关链接", "附录", "作者信息",
    "acknowledgements", "acknowledgments", "competing interests",
    "conflict of interest", "supplementary information", "author information"
}

CHINESE_NUMERALS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
                    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十"]


def audit_glossary_consistency(soup, glossary_path: Path | None) -> list[dict]:
    issues = []
    if not glossary_path or not Path(glossary_path).is_file():
        return issues
    try:
        gloss = json.loads(Path(glossary_path).read_text(encoding="utf-8"))
    except Exception:
        return issues

    terms = gloss.get("terms", [])
    full_text = soup.get_text()

    for term in terms:
        target = term.get("target", "").strip()
        aliases = term.get("aliases", [])
        if not target or not aliases:
            continue
        for alias in aliases:
            alias = alias.strip()
            if not alias or alias == target:
                continue
            if alias in full_text:
                issues.append({
                    "severity": "warning",
                    "code": "term_drift_alias_leak",
                    "message": f"发布文档中检测到已废弃别名/旧译法: '{alias}'，应统一为标准词 '{target}' (运行 pipeline.py patch-terms 可一键修复)",
                    "context": f"{alias} -> {target}"
                })
    return issues


def audit_html(html_path: Path, glossary_path: Path | None = None) -> dict:
    content = html_path.read_text(encoding="utf-8")
    if glossary_path is None:
        candidate = html_path.parent.parent / "glossary.json"
        if candidate.is_file():
            glossary_path = candidate
    return audit_html_content(content, filename=html_path.name, html_path=html_path, glossary_path=glossary_path)


def audit_html_content(content: str, filename: str = "", html_path: Path | None = None, glossary_path: Path | None = None) -> dict:
    soup = BeautifulSoup(content, "html.parser")
    file_label = str(html_path) if html_path else filename
    is_gov_doc = (
        "gov_doc" in filename
        or "公文版" in filename
        or bool(soup.find("main", attrs={"data-edition": "gov_doc"}))
        or bool(soup.find(class_="gov-header") or soup.find(class_="gov-org-name"))
    )

    issues = []
    
    # Audit Terminology Consistency against Glossary
    if glossary_path:
        issues.extend(audit_glossary_consistency(soup, glossary_path))
    
    # 1. Audit Header Metadata (gov_doc specific)
    if is_gov_doc:
        header_org = soup.find(class_=re.compile(r"gov-(?:org-name|header-org)"))
        header_docno = soup.find(class_=re.compile(r"gov-(?:doc-number|header-docno)"))
        if not header_org:
            issues.append({
                "severity": "error",
                "code": "missing_gov_header",
                "message": "公文版缺少发文红头 (.gov-org-name)"
            })
        elif "学术期刊译情参阅" in header_org.get_text():
            issues.append({
                "severity": "error",
                "code": "fallback_gov_header",
                "message": f"公文版红头使用了通用占位符: '{header_org.get_text().strip()}'，必须使用正式期刊全称"
            })
            
        if not header_docno:
            issues.append({
                "severity": "error",
                "code": "missing_gov_docno",
                "message": "公文版缺少发文字号 (.gov-doc-number)"
            })
        elif re.search(r"编号：[0-9a-f]{8,}", header_docno.get_text()):
            issues.append({
                "severity": "error",
                "code": "fallback_gov_docno",
                "message": f"公文版字号使用了随机哈希占位符: '{header_docno.get_text().strip()}'，必须使用正式 DOI 与出版年份"
            })

    # 2. Audit Spurious Running Headers as Headings
    headings = soup.find_all(re.compile(r"^h[1-6]$"))
    heading_records = []
    for h in headings:
        # Ignore hidden/suppressed blocks
        if h.has_attr("style") and "display:none" in h["style"].replace(" ", ""):
            continue
        text = h.get_text(strip=True)
        norm_text = re.sub(r"^[一二三四五六七八九十]+、", "", text).strip().lower()
        norm_text = re.sub(r"^[（(][一二三四五六七八九十]+[）)]", "", norm_text).strip()
        norm_text = re.sub(r"^\d+[\.、]", "", norm_text).strip()
        
        heading_records.append({
            "tag": h.name,
            "text": text,
            "norm": norm_text,
            "class": h.get("class", [])
        })
        
        if norm_text in SUSPICIOUS_RUNNING_HEADERS:
            issues.append({
                "severity": "error",
                "code": "spurious_running_header",
                "message": f"检测到疑似 OCR 走马页眉被误识别为正文标题: '<{h.name}>{text}</{h.name}>'",
                "context": text
            })

    # 3. Audit Heading Numbering Hierarchy (gov_doc specific)
    if is_gov_doc:
        h1_items = [hr["text"] for hr in heading_records if "gov-h1" in hr["class"] or hr["tag"] == "h2"]
        h1_nums = []
        for text in h1_items:
            m = re.match(r"^([一二三四五六七八九十]+)、", text)
            if m:
                h1_nums.append(m.group(1))
        
        # Check sequence
        expected_seq = CHINESE_NUMERALS[:len(h1_nums)]
        if h1_nums and h1_nums != expected_seq:
            issues.append({
                "severity": "warning",
                "code": "heading_sequence_mismatch",
                "message": f"公文版一级标题编号不连续: 实际为 {h1_nums}，预期为 {expected_seq}"
            })

    # 4. Audit Reference List Integrity
    ref_items = []
    # Try finding .gov-ref-item or .ref-item or reference containers
    ref_elements = soup.find_all(class_=re.compile(r"gov-ref-item|ref-item"))
    if not ref_elements:
        # Fallback: search for numbered paragraphs or list items after a reference header
        ref_header = soup.find(lambda el: el.name in ["h1", "h2", "h3", "div", "p"] and "参考文献" in el.get_text())
        if ref_header:
            curr = ref_header.find_next_sibling()
            while curr:
                # If hit another major heading, break
                if curr.name in ["h1", "h2"] and not any(allowed in curr.get_text() for allowed in ALLOWED_END_MATTER):
                    break
                if curr.name == "ol":
                    for li in curr.find_all("li"):
                        ref_items.append(li.get_text(strip=True))
                elif curr.name in ["p", "div"]:
                    txt = curr.get_text(strip=True)
                    if re.match(r"^(?:\[?\d+\]?|\d+\.)", txt):
                        ref_items.append(txt)
                curr = curr.find_next_sibling()
    else:
        for re_el in ref_elements:
            ref_items.append(re_el.get_text(strip=True))

    # Check for <ol> fallback inside gov_doc
    if is_gov_doc:
        gov_ol = soup.find_all("ol")
        if gov_ol:
            # Check if any ol is used for references
            for ol in gov_ol:
                items = ol.find_all("li")
                if len(items) > 3 and any(re.match(r"^\d+\.", it.get_text(strip=True)) for it in items):
                    issues.append({
                        "severity": "error",
                        "code": "ref_style_downgrade",
                        "message": f"公文版中发现 {len(items)} 条参考文献被退化渲染为普通 <ol> 列表，未应用 GB/T 7714 悬挂缩进样式"
                    })

    # Check reference numbering continuity
    ref_numbers = []
    for txt in ref_items:
        m = re.match(r"^(?:\[?(\d+)\]?|(\d+)\.)", txt)
        if m:
            num = int(m.group(1) or m.group(2))
            ref_numbers.append(num)

    if ref_numbers:
        # Check monotonicity & gaps
        expected = list(range(ref_numbers[0], ref_numbers[0] + len(ref_numbers)))
        if ref_numbers != expected:
            # Check missing
            seen = set()
            duplicates = []
            for n in ref_numbers:
                if n in seen:
                    duplicates.append(n)
                seen.add(n)
            
            missing = [n for n in range(min(ref_numbers), max(ref_numbers) + 1) if n not in seen]
            is_sorted = (ref_numbers == sorted(ref_numbers))
            
            err_msg_parts = []
            if not is_sorted:
                err_msg_parts.append("文献序号乱序/倒置")
            if missing:
                err_msg_parts.append(f"缺失序号 {missing[:10]}{'...' if len(missing) > 10 else ''}")
            if duplicates:
                err_msg_parts.append(f"重复序号 {duplicates[:5]}")
                
            issues.append({
                "severity": "error",
                "code": "ref_numbering_anomaly",
                "message": f"参考文献编号异常: {'; '.join(err_msg_parts)} (共 {len(ref_numbers)} 条，范围 {min(ref_numbers)}~{max(ref_numbers)})"
            })

    # 5. Summary determination
    has_errors = any(i["severity"] == "error" for i in issues)
    has_warnings = any(i["severity"] == "warning" for i in issues)
    status = "failed" if has_errors else ("warning" if has_warnings else "passed")

    return {
        "file": file_label,
        "status": status,
        "is_gov_doc": is_gov_doc,
        "metrics": {
            "total_headings": len(heading_records),
            "total_references": len(ref_numbers),
            "ref_range": [min(ref_numbers), max(ref_numbers)] if ref_numbers else None
        },
        "issues": issues
    }


def audit_publication_dir(pub_dir: Path, glossary_path: Path | None = None) -> dict:
    pub_path = Path(pub_dir).resolve()
    if glossary_path is None:
        candidate = pub_path.parent.parent / "glossary.json"
        if candidate.is_file():
            glossary_path = candidate
    html_files = sorted(list(pub_path.glob("*.html")))
    if not html_files:
        return {
            "status": "failed",
            "error": f"未在 {pub_path} 发现任何 HTML 文件"
        }

    results = {}
    overall_status = "passed"
    for h in html_files:
        res = audit_html(h, glossary_path=glossary_path)
        results[h.name] = res
        if res["status"] == "failed":
            overall_status = "failed"
        elif res["status"] == "warning" and overall_status != "failed":
            overall_status = "warning"

    return {
        "status": overall_status,
        "directory": str(pub_path),
        "files_audited": len(html_files),
        "editions": results
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Audit layout & typography of published HTML files.")
    parser.add_argument("target", help="Path to published directory or single HTML file")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args()

    target_path = Path(args.target).resolve()
    if target_path.is_file() and target_path.suffix.lower() == ".html":
        report = audit_html(target_path)
    elif target_path.is_dir():
        report = audit_publication_dir(target_path)
    else:
        print(f"Error: Target path '{target_path}' is neither an HTML file nor a directory.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        # Pretty console print
        print("=" * 60)
        print(f"版式终审质量报告 (Layout Audit Report): {report.get('status', 'unknown').upper()}")
        print("=" * 60)
        if "editions" in report:
            for fname, item in report["editions"].items():
                print(f"\n[FILE] {fname} [{item['status'].upper()}]")
                metrics = item.get("metrics", {})
                print(f"   - 标题总数: {metrics.get('total_headings', 0)}")
                print(f"   - 参考文献: {metrics.get('total_references', 0)} 条 (编号: {metrics.get('ref_range')})")
                if item["issues"]:
                    for iss in item["issues"]:
                        tag = "[ERROR]" if iss["severity"] == "error" else "[WARN]"
                        print(f"   {tag} {iss['code']}: {iss['message']}")
                else:
                    print("   [OK] 无版式异常发现")
        else:
            print(f"文件: {report.get('file')}")
            if report.get("issues"):
                for iss in report["issues"]:
                    tag = "[ERROR]" if iss["severity"] == "error" else "[WARN]"
                    print(f"{tag} {iss['code']}: {iss['message']}")
            else:
                print("[OK] 无版式异常发现")
        print("=" * 60)

    sys.exit(1 if report.get("status") == "failed" else 0)


if __name__ == "__main__":
    main()
