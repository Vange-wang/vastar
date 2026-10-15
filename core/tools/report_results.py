"""Validate private staging before any deliverable artifact is published."""
import base64
import io
import json
import xml.etree.ElementTree as ET
import zipfile

from core.tools.report_source import reader_safe


def validate(result, source):
    if result.get("status") != "validated":
        raise ValueError("未完成输出：隔离生成或视觉检查未通过")
    visual, structure = result.get("visual", {}), result.get("structure", {})
    pages = result.get("pages", [])
    if not pages or len(pages) != structure.get("page_count") or visual.get("edge_check") != "passed":
        raise ValueError("未完成输出：缺少实际页图证据")
    if visual.get("pages_checked") != list(range(1, len(pages) + 1)):
        raise ValueError("未完成输出：页图覆盖不完整")
    reader_safe(structure)
    extracted = ''.join(''.join(structure.get("text", [])).split())
    expected = [p["text"] for p in source.get("paragraphs", [])]
    expected += [r["citation"] for r in source.get("references", [])]
    expected += [c for t in source.get("tables", []) for row in [t["headers"], *t["rows"]] for c in row]
    if any(''.join(text.split()) not in extracted for text in expected):
        raise ValueError("PDF 内容与原文不一致")
    files = {name: base64.b64decode(result[name], validate=True) for name in ("pdf", "docx")}
    if not files["pdf"].startswith(b"%PDF-"):
        raise ValueError("PDF 不可打开")
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(files["docx"])) as archive:
        if archive.testzip() is not None:
            raise ValueError("DOCX 损坏")
        root = ET.fromstring(archive.read("word/document.xml"))
        header = ET.fromstring(archive.read("word/header1.xml"))
        texts = [e.text or "" for e in root.findall(".//w:t", ns)]
        reader_safe(texts + [e.text or "" for e in header.findall(".//w:t", ns)])
        tables = root.findall(".//w:tbl", ns)
        if len(tables) != len(source.get("tables", [])):
            raise ValueError("表格缺失")
        for table, expected in zip(tables, source.get("tables", []), strict=True):
            if len(table.findall("w:tblGrid/w:gridCol", ns)) != len(expected["headers"]):
                raise ValueError("DOCX 缺少必需的表格列网格")
            rows = [["".join(e.itertext()) for e in row.findall("w:tc/w:p/w:r/w:t", ns)]
                    for row in table.findall("w:tr", ns)]
            if rows != [expected["headers"], *expected["rows"]] or table.find("w:tr/w:trPr/w:tblHeader", ns) is None:
                raise ValueError("表格与原数据不一致或缺重复表头")
        if ''.join(header.itertext()) != (source.get("header") or "报告"):
            raise ValueError("页眉不一致")
        for item in source.get("paragraphs", []):
            if item["text"] + ' [' + item["source"] + ']' not in texts:
                raise ValueError("段落与来源不一致")
        for ref in source.get("references", []):
            if '[' + ref["id"] + '] ' + ref["citation"] not in texts:
                raise ValueError("引用不一致")
        for index, item in enumerate(source.get("images", []), 1):
            if archive.read(f"word/media/image{index}.png") != base64.b64decode(item["png_base64"], validate=True):
                raise ValueError("图片与来源不一致")
            if item["caption"] + ' [' + item["source"] + ']；图片授权：' + item["license"] not in texts:
                raise ValueError("图片授权未沿来源")
    for index, encoded in enumerate(pages, 1):
        data = base64.b64decode(encoded, validate=True)
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError("缺少真实 PNG 页图")
        files[f"page-{index}.png"] = data
    return files


def publish(artifacts, payload, result):
    files = validate(result, payload.source)
    hashes = payload.verify_unchanged()
    # Files stay in an owned UUID directory, never in a user-selected destination.
    for name, data in files.items():
        filename = "report." + name if name in {"pdf", "docx"} else name
        (payload.staging / filename).write_bytes(data)
    meta = []
    try:
        for name, data in files.items():
            item = artifacts.put(data, source_ref="report:" + payload.before + ":" + name)
            meta.append(item)
    except Exception:
        for item in meta:
            artifacts.delete(item.artifact_id, item.version)
        raise
    (payload.staging / "STATUS.txt").write_text("机器检查通过；真人版式待 user\n", encoding="utf-8")
    proof = {"structure": result["structure"], "visual": result["visual"], "versions": payload.versions,
             "hashes": hashes, "artifacts": {name: item.uri for name, item in zip(files, meta, strict=True)}}
    (payload.staging / "evidence.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "machine_verified", "human_layout": "pending user", "directory": str(payload.staging),
            "files": list(files), "artifact_refs": proof["artifacts"], "hashes": hashes,
            "versions": payload.versions, "note": "可打开及机器检查已完成；不代表真人版式合格。"}
