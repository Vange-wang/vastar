"""Reader content is literal, bounded and source-linked; engineering data stays outside."""
import json
import re

INTERNAL = re.compile(r"(?:[A-Za-z]:[\\/]|(?:^|\s)/(?:home|tmp|Users|etc)/|core[/\\.]|"
                      r"Traceback|WinError|artifact://|(?:report|document|process)_(?:service|tools|render)|"
                      r"execute_planned|provider\.stream|ArtifactStore|ToolExecutor|ExecutionSandbox|"
                      r"start_process|generate_report|read_artifact|[\w-]+\.py\b)", re.I)


def reader_safe(value):
    if INTERNAL.search(json.dumps(value, ensure_ascii=False)):
        raise ValueError("正文含内部工程信息；请提供面向读者的来源文本")


def validate_source(data):
    if not isinstance(data, dict) or set(data) - {"title", "header", "paragraphs", "tables", "images", "references"}:
        raise ValueError("报告源格式不支持")
    reader_safe(data)
    for key in ("title", "header"):
        if not isinstance(data.get(key, ""), str) or len(data.get(key, "")) > 100:
            raise ValueError("标题或页眉过长")
    references = data.get("references", [])
    if not isinstance(references, list) or len(references) > 30:
        raise ValueError("来源列表超限")
    for ref in references:
        if not isinstance(ref, dict) or set(ref) != {"id", "citation"}:
            raise ValueError("每条来源需 id 与原样 citation")
        if not all(isinstance(v, str) and 0 < len(v) <= 500 for v in ref.values()):
            raise ValueError("来源内容不完整")
    ids = [r["id"] for r in references]
    if len(set(ids)) != len(ids):
        raise ValueError("来源编号重复")
    paragraphs, tables = data.get("paragraphs", []), data.get("tables", [])
    if not isinstance(paragraphs, list) or not isinstance(tables, list) or len(paragraphs) > 60 or len(tables) > 8:
        raise ValueError("段落或表格超限")
    for item in paragraphs:
        if not isinstance(item, dict) or set(item) != {"text", "source"}:
            raise ValueError("段落需原文与来源")
        if not isinstance(item["text"], str) or len(item["text"]) > 4000 or item["source"] not in ids:
            raise ValueError("段落缺来源或超限")
    for table in tables:
        if not isinstance(table, dict) or set(table) != {"headers", "rows", "source", "unit"}:
            raise ValueError("表格需表头、数据、来源与原单位")
        if table["source"] not in ids or not isinstance(table["unit"], str) or len(table["unit"]) > 40:
            raise ValueError("表格缺来源或单位")
        headers, rows = table["headers"], table["rows"]
        if not isinstance(headers, list) or not 1 <= len(headers) <= 5 or not isinstance(rows, list) or len(rows) > 150:
            raise ValueError("表格尺寸超限")
        for row in [headers, *rows]:
            if not isinstance(row, list) or len(row) != len(headers):
                raise ValueError("表格列数不一致")
            if any(not isinstance(c, str) or len(c) > 160 for c in row):
                raise ValueError("表格只接受原样文本单元格")
    images = data.get("images", [])
    if not isinstance(images, list) or len(images) > 6:
        raise ValueError("图片数量超限")
    for image in images:
        fields = {"png_base64", "caption", "source", "license", "allow_report"}
        if not isinstance(image, dict) or set(image) != fields or image["allow_report"] is not True:
            raise ValueError("图片缺失或未明确授权用于报告；未完成输出")
        if image["source"] not in ids or not all(isinstance(image[k], str) and image[k]
                                                 for k in ("png_base64", "caption", "license")):
            raise ValueError("图片缺来源或授权原文")
        if len(image["caption"]) > 300 or len(image["license"]) > 300:
            raise ValueError("图片说明过长")
    return data
