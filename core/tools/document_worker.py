"""Standalone stdlib worker. Invoked only in a sealed, offline B27/B28 Job."""
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

TEXT_LIMIT = 1024 * 1024
NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class EngineUnavailable(Exception):
    """Only a failed external-engine launch, never a document parsing error."""


def failure(request, exc):
    unavailable = isinstance(exc, EngineUnavailable)
    reason = ("PDF 解析引擎不可用：隔离环境下无法启动外部 pdftotext；未退回非沙箱执行。"
              if unavailable else "文档读取不可用：无法访问隔离输入文件。" if isinstance(exc, OSError) else str(exc))
    original = exc.__cause__ or exc
    result = {"status": "partial", "records": [], "uncovered": [reason], "more": False,
              "diagnostics": {"error_type": type(original).__name__, "raw_error": str(original),
                              "errno": getattr(original, "errno", None), "winerror": getattr(original, "winerror", None)},
              "ocr": {"requested": request["ocr"], "status": "unavailable", "confidence": None,
                      "unrecognized_regions": ["entire requested range; bounds unknown"]}}
    if unavailable:
        result["availability"] = {"status": "unavailable", "reason_code": "external_program_start_failed",
                                  "isolation": "appcontainer", "fallback_used": False}
    return result


def record(text, page, section, *, cells=None):
    item = {"kind": "table_row" if cells else "paragraph", "text": text,
            "source_page": page, "source_section": section}
    if cells:
        item["cells"] = cells
    return item


def text_window(path, request):
    first, stop = request["first"], request["first"] + request["count"]
    records, used, page, line_no, more = [], 0, 1, 0, False
    with path.open(encoding="utf-8-sig", errors="strict") as stream:
        while line := stream.readline(65537):
            line_no += 1
            if len(line) > 65536 or "\x00" in line:
                raise ValueError("wrong type or oversized line; UTF-8 text required")
            for part_no, part in enumerate(line.split("\f")):
                if part_no:
                    page += 1
                if page >= stop or used + len(part.encode("utf-8")) > TEXT_LIMIT:
                    more = True
                    break
                if page >= first:
                    text = part.rstrip("\r\n")
                    cells = text.strip(" |\t").split("|") if request["kind"] == "md" and "|" in text else None
                    records.append(record(text, page, f"line:{line_no}", cells=cells))
                    used += len(part.encode("utf-8"))
            # 200 source lines per window page for text without form feeds.
            if line_no % 200 == 0:
                page += 1
            if more:
                break
    return records, ["Text pages are 200-line/form-feed windows; physical layout is unavailable"], more


def pdf_window(path, request):
    with path.open("rb") as source:
        signature = source.read(5)
    if signature != b"%PDF-":
        raise ValueError("wrong file type: expected PDF signature")
    first, count = request["first"], request["count"]
    command = [str(Path("poppler/pdftotext.exe").resolve()), "-f", str(first), "-l", str(first + count - 1),
               "-layout", "-enc", "UTF-8", str(path), "-"]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise EngineUnavailable() from exc
    with process:
        data = process.stdout.read(TEXT_LIMIT + 1)
        clipped = len(data) > TEXT_LIMIT
        if clipped:
            process.kill()
        code = process.wait()
    if code in (0xC0000135, 0xC0000142, 0xC000007B, 0xC0000139):
        raise EngineUnavailable(f"Windows loader failed: exit_code=0x{code:08X}")
    if code and not clipped:
        raise ValueError("PDF extraction failed: damaged/encrypted/unsupported or page out of range")
    text = data[:TEXT_LIMIT].decode("utf-8", errors="replace")
    pages = text.split("\f")
    if pages[-1] == "":
        pages.pop()
    records = []
    for offset, page in enumerate(pages):
        for index, line in enumerate(page.splitlines() or [""]):
            cells = re.split(r"\s{2,}", line.strip())
            records.append(record(line, first + offset, f"extracted-line:{index + 1}",
                                  cells=cells if len(cells) > 1 else None))
    gaps = ["PDF always partial: images/OCR, reading order and image-text alignment unverified",
            "Table rows are layout candidates; cross-page joins and numeric unit conversion unverified",
            "Empty extracted pages may be blank OR scanned; not evidence of an empty source page"]
    if clipped:
        gaps.append("1 MiB text window clipped; remaining text unrecognized")
    if len(pages) >= count:
        gaps.append("Requested page window filled; next window is a candidate, total page count unverified")
    return records, gaps, clipped or len(pages) >= count


def docx_window(path, request):
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 2048:
            raise ValueError("OOXML entry count limit")
        names = archive.namelist()
        if any("vbaproject" in name.lower() for name in names):
            raise ValueError("macro carrier rejected")
        for name in ("[Content_Types].xml", "word/document.xml"):
            info = archive.getinfo(name)
            if info.file_size > 4 * TEXT_LIMIT or info.flag_bits & 1:
                raise ValueError("OOXML expanded size/encryption limit")
        types = archive.read("[Content_Types].xml")
        if b"macroEnabled" in types:
            raise ValueError("macro content type rejected")
        data = archive.read("word/document.xml")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("DTD/entity forbidden")
    body = ET.fromstring(data).find(NS + "body")
    if body is None:
        raise ValueError("wrong OOXML document type")
    start, stop = (request["first"] - 1) * 100, (request["first"] - 1 + request["count"]) * 100
    records, used = [], 0
    for index, element in enumerate(body):
        if index < start:
            continue
        if index >= stop or used > TEXT_LIMIT:
            break
        if element.tag == NS + "tbl":
            for row_no, row in enumerate(element.findall(NS + "tr")):
                cells = ["".join(t.text or "" for t in cell.iter(NS + "t")) for cell in row.findall(NS + "tc")]
                text = " | ".join(cells)
                records.append(record(text, None, f"body:{index + 1}/row:{row_no + 1}", cells=cells))
                used += len(text.encode("utf-8"))
        else:
            text = "".join(t.text or "" for t in element.iter(NS + "t"))
            records.append(record(text, None, f"body:{index + 1}"))
            used += len(text.encode("utf-8"))
    return records, ["DOCX physical pages/cross-page table alignment unavailable; use body/row positions",
                     "Images, headers/footers, notes, tracked revisions and OCR are not covered"], len(body) > stop


def extract(request):
    function = {"txt": text_window, "md": text_window, "pdf": pdf_window, "docx": docx_window}[request["kind"]]
    try:
        records, gaps, more = function(Path("source"), request)
        return {"status": "partial", "records": records, "uncovered": gaps, "more": more,
                "ocr": {"requested": request["ocr"], "status": "unavailable", "confidence": None,
                        "unrecognized_regions": ["all image regions; bounds unknown"],
                        "reason": "no OCR adapter; confirmed text retained"}}
    except (EngineUnavailable, ValueError, OSError, KeyError, zipfile.BadZipFile, UnicodeError, ET.ParseError) as exc:
        return failure(request, exc)


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(extract(request), ensure_ascii=False))
