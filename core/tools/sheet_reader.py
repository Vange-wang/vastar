"""Worker-only bounded CSV/OOXML reader; hidden sheets and formula caches stay explicit."""
import csv
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from sheet_cells import cell

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
MAX_ROWS, MAX_COLS, MAX_CELLS = 10000, 64, 100000


def xml(archive, name):
    raw = archive.read(name)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("禁止 XML 实体")
    return ET.fromstring(raw)


def check_budget(row_count, width, total_cells):
    if row_count > MAX_ROWS or total_cells > MAX_CELLS:
        raise ValueError("表格超过行数/单元格内存预算；原始副本保留")
    if width > MAX_COLS:
        raise ValueError("表格超过 64 列预算")


def read_csv(path):
    csv.field_size_limit(4096)
    rows = []
    total_cells = 0
    with open(path, encoding="utf-8-sig", newline="") as stream:
        for index, values in enumerate(csv.reader(stream), 1):
            total_cells += len(values)
            check_budget(index, len(values), total_cells)
            rows.append({"source_row": index, "cells": [cell(v) for v in values]})
    return [{"name": "CSV", "visibility": "visible", "rows": rows}], []


def xlsx_rows(archive, target, strings, date_styles):
    rows, seen = [], set()
    total_cells, previous_row = 0, 0
    root = xml(archive, target)
    for row in root.findall("s:sheetData/s:row", NS):
        declared = row.get("r")
        if declared is None:
            first = row.find("s:c", NS)
            first_ref = re.fullmatch(r"[A-Z]+([1-9]\d*)", first.get("r", "")) if first is not None else None
            declared = first_ref[1] if first_ref else previous_row + 1
        row_id = int(declared)
        if row_id in seen or row_id <= 0:
            raise ValueError("行坐标无效或重复")
        seen.add(row_id)
        previous_row = row_id
        values = {}
        for item in row.findall("s:c", NS):
            match = re.fullmatch(r"([A-Z]+)([1-9]\d*)", item.get("r", ""))
            if match is None:
                raise ValueError("缺少单元格坐标")
            if int(match[2]) != row_id:
                raise ValueError("单元格行号与所在行不一致")
            col = 0
            for ch in match[1]:
                col = col * 26 + ord(ch) - 64
            if col > MAX_COLS or col in values:
                raise ValueError("列越界或坐标重复")
            raw = item.findtext("s:v", "", NS)
            form = item.find("s:f", NS)
            kind = item.get("t", "n")
            if form is not None:
                value = cell("=" + (form.text or ""), "formula", formula=form.text or "",
                             cached_value=raw, formula_attributes=dict(form.attrib))
            elif kind == "s":
                if not raw.isdigit() or int(raw) >= len(strings):
                    raise ValueError('共享字符串索引无效')
                value = cell(strings[int(raw)], "text")
            elif kind == "inlineStr":
                value = cell(''.join(e.text or "" for e in item.findall("s:is//s:t", NS)), "text")
            else:
                tag = "error" if kind == "e" else "boolean" if kind == "b" else "date" if kind == "d" else None
                if int(item.get("s", "0")) in date_styles and raw:
                    tag = "formatted_value"
                value = cell(raw, tag, style=item.get("s", "0"))
            values[col] = value
        width = max(values, default=0)
        total_cells += width
        check_budget(len(rows) + 1, width, total_cells)
        cells = [values[c] if c in values else cell("") for c in range(1, width + 1)]
        rows.append({"source_row": row_id, "cells": cells})
    return rows


def read_xlsx(path):
    sheets, limitations = [], ["不计算公式；不使用缓存值作统计；格式化金额/日期序列不自动换算或聚合；图表/透视表/格式语义不解析"]
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 512 or len({i.filename for i in infos}) != len(infos):
            raise ValueError("OOXML 项目过多或重名")
        if sum(i.file_size for i in infos) > 16 * 1024 * 1024:
            raise ValueError("OOXML 解压内存预算超限")
        if any("vbaproject" in i.filename.lower() for i in infos):
            raise ValueError("含宏工作簿不支持")
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = [''.join(e.text or "" for e in s.findall(".//s:t", NS))
                       for s in xml(archive, "xl/sharedStrings.xml").findall("s:si", NS)]
        dates = set()
        if "xl/styles.xml" in archive.namelist():
            styles = xml(archive, "xl/styles.xml")
            # Custom number formats are conservatively excluded from arithmetic too.
            dates = {i for i, s in enumerate(styles.findall("s:cellXfs/s:xf", NS))
                     if int(s.get("numFmtId", "0")) not in (0, 1, 2)}
        rels = {r.get("Id"): r for r in xml(archive, "xl/_rels/workbook.xml.rels")}
        book = xml(archive, "xl/workbook.xml")
        if len(book.findall("s:sheets/s:sheet", NS)) > 16:
            raise ValueError("最多 16 个 sheet")
        for sheet in book.findall("s:sheets/s:sheet", NS):
            record = {"name": sheet.get("name"), "visibility": sheet.get("state", "visible"), "rows": []}
            try:
                rel = rels[sheet.get('{' + NS['r'] + '}id')]
                target = rel.get("Target", "")
                target = posixpath.normpath(target.lstrip("/") if target.startswith("/xl/") else posixpath.join("xl", target))
                if rel.get("TargetMode") == "External" or not target.startswith("xl/worksheets/"):
                    raise ValueError("外链或非法 sheet 目标禁止读取")
                record["rows"] = xlsx_rows(archive, target, strings, dates)
            except (ValueError, KeyError, IndexError, ET.ParseError) as exc:
                record["error"] = str(exc)
            sheets.append(record)
        if any("externalLinks" in n for n in archive.namelist()):
            limitations.append("外链工作簿存在，未打开、未获取")
    return sheets, limitations


def read(path, kind):
    return read_csv(path) if kind == "csv" else read_xlsx(path)
