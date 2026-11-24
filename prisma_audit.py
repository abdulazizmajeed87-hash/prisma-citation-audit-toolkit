"""Deterministic PRISMA DOCX citation-sequence and SHA-256 corpus verifier."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

W_NS = "{http://openxmlformats.org}"
CITE_RE = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")
TOKEN_RE = re.compile(r"\S+")
MAX_XML_BYTES = 50 * 1024 * 1024  # 50 MB XML ceiling


def expand_brackets(expr: str) -> list[int]:
    """Expand bracketed numeric citations like '1, 3-5' into individual integers."""
    nums: list[int] = []
    for part in re.split(r"\s*,\s*", expr.strip()):
        if "-" in part or "–" in part:
            lo_s, hi_s = re.split(r"\s*[-–]\s*", part, maxsplit=1)
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo or (hi - lo) > 5000:
                raise ValueError(f"Invalid citation range: {part}")
            nums.extend(range(lo, hi + 1))
        elif part.isdigit():
            nums.append(int(part))
    return nums


def sha256_file(path: Path) -> str:
    """Compute streaming SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_docx(docx_path: Path) -> dict:
    """Verify sequential 1..N first-citation order and word counts in a DOCX file."""
    with zipfile.ZipFile(docx_path, "r") as archive:
        for info in archive.infolist():
            if ".." in Path(info.filename).parts:
                raise ValueError(f"Unsafe archive path detected: {info.filename}")
        doc_info = archive.getinfo("word/document.xml")
        if doc_info.file_size > MAX_XML_BYTES:
            raise ValueError("word/document.xml exceeds safe size ceiling.")
        xml_bytes = archive.read("word/document.xml")

    root = ET.fromstring(xml_bytes)
    paragraphs = [
        "".join(node.text or "" for node in para.iter(f"{W_NS}t")).strip()
        for para in root.iter(f"{W_NS}p")
    ]

    seen_order: list[int] = []
    total_mentions = 0
    for text in paragraphs:
        for match in CITE_RE.finditer(text):
            for ref_num in expand_brackets(match.group(1)):
                total_mentions += 1
                if ref_num not in seen_order:
                    seen_order.append(ref_num)

    word_tokens = sum(
        1
        for para in paragraphs
        for tok in TOKEN_RE.findall(para)
        if any(ch.isalnum() for ch in tok)
    )
    mismatches = [
        {"expected": idx + 1, "observed": val}
        for idx, val in enumerate(seen_order)
        if val != idx + 1
    ]
    return {
        "file": docx_path.name,
        "sha256": sha256_file(docx_path),
        "alnum_word_tokens": word_tokens,
        "unique_citations": len(seen_order),
        "expanded_mentions": total_mentions,
        "max_citation": max(seen_order, default=0),
        "sequential_first_appearance_ok": len(mismatches) == 0,
        "first_mismatch": mismatches[0] if mismatches else None,
    }


def verify_corpus_manifest(
    csv_path: Path, path_col: str = "path", hash_col: str = "sha256"
) -> dict:
    """Verify local full-text PDFs against an expected SHA-256 CSV registry."""
    checked = matched = missing = mismatched = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            checked += 1
            target = Path(row[path_col])
            expected = row[hash_col].strip().lower()
            if not target.is_file():
                missing += 1
            elif sha256_file(target) == expected:
                matched += 1
            else:
                mismatched += 1
    return {
        "rows_checked": checked,
        "sha256_matched": matched,
        "missing_files": missing,
        "hash_mismatches": mismatched,
        "integrity_ok": (checked > 0 and missing == 0 and mismatched == 0),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prisma-audit",
        description="Deterministic PRISMA DOCX & corpus SHA-256 auditor.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_docx = sub.add_parser("docx", help="Audit sequential citations in a DOCX file.")
    p_docx.add_argument("path", type=Path, help="Path to manuscript .docx")

    p_csv = sub.add_parser("manifest", help="Verify PDF hashes against a CSV index.")
    p_csv.add_argument("csv_path", type=Path, help="Path to manifest .csv")
    p_csv.add_argument("--path-col", default="path")
    p_csv.add_argument("--hash-col", default="sha256")

    args = parser.parse_args(argv)
    if args.cmd == "docx":
        result = audit_docx(args.path)
        print(json.dumps(result, indent=2))
        return 0 if result["sequential_first_appearance_ok"] else 1

    result = verify_corpus_manifest(args.csv_path, args.path_col, args.hash_col)
    print(json.dumps(result, indent=2))
    return 0 if result["integrity_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
