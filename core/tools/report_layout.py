"""Single controlled A4 report layout, rendered only in the external kit."""
from html import escape
from io import BytesIO


def make_pdf(source, template):
    from report_images import caption, png
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(TTFont("ReportFont", "NotoSansSC.ttf"))
    face = pdfmetrics.getFont("ReportFont").face
    text = "".join(texts(source))
    missing = sorted({c for c in text if not c.isspace() and ord(c) not in face.charToGlyph})
    if missing:
        raise ValueError("字体缺字：" + "".join(missing[:20]))
    style = ParagraphStyle("Body", fontName="ReportFont", fontSize=10, leading=16,
                           wordWrap="CJK", spaceAfter=8)
    title = ParagraphStyle("Title", parent=style, fontSize=19, leading=27, spaceAfter=18)
    def p(s):
        return Paragraph(escape(s).replace("\n", "<br/>"), style)
    flow = [Paragraph(escape(source.get("title") or "信息待补充"), title)]
    flow.extend(p(item["text"] + " [" + item["source"] + "]") for item in source.get("paragraphs", []))
    if not source.get("paragraphs") and not source.get("tables"):
        flow.append(p("信息不足：尚无可引用的正文或表格。"))
    for table in source.get("tables", []):
        flow.append(p("单位：" + (table["unit"] or "未提供") + "；来源：[" + table["source"] + "]"))
        rows = [[p(cell) for cell in row] for row in [table["headers"], *table["rows"]]]
        block = Table(rows, colWidths=[483 / len(table["headers"])] * len(table["headers"]), repeatRows=1)
        block.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .35, "#CBD5E1"),
                                   ("BACKGROUND", (0, 0), (-1, 0), "#EDF2F7"),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 6),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
        flow.extend([block, Spacer(1, 12)])
    for item in source.get("images", []):
        data, width, height = png(item)
        flow.extend([Image(BytesIO(data), width, height), p(caption(item))])
    flow.append(p("参考来源"))
    flow.extend(p("[" + ref["id"] + "] " + ref["citation"]) for ref in source.get("references", []))
    def header(canvas, doc):
        canvas.saveState()
        canvas.setTitle(source.get("title") or "报告")
        canvas.setAuthor("")
        canvas.setSubject("")
        canvas.setCreator("")
        canvas.setProducer("")
        canvas.setFont("ReportFont", 9)
        canvas.setFillColorRGB(0, 0, 0)
        canvas.drawString(56, 807, source.get("header") or "报告")
        canvas.drawRightString(539, 30, str(doc.page))
        canvas.restoreState()
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=tuple(template["page_points"]), leftMargin=56, rightMargin=56,
                            topMargin=62, bottomMargin=50, pageCompression=1)
    doc.build(flow, onFirstPage=header, onLaterPages=header)
    return output.getvalue()


def texts(source):
    yield source.get("title", "")
    yield source.get("header", "")
    yield "报告信息待补充不足尚无可引用的正文或表格单位未提供来源参考图片授权；：[]0123456789"
    for item in source.get("paragraphs", []):
        yield item["text"] + item["source"]
    for table in source.get("tables", []):
        yield table["unit"] + table["source"]
        for row in [table["headers"], *table["rows"]]:
            yield "".join(row)
    for ref in source.get("references", []):
        yield ref["id"] + ref["citation"]
    for image in source.get("images", []):
        yield image["caption"] + image["license"] + image["source"]
