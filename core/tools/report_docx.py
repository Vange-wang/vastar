"""Controlled OOXML package: real tables, repeating headers and embedded font."""
import io
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def paragraph(text):
    return '<w:p><w:r><w:rPr><w:rFonts w:ascii="Report Sans SC" w:eastAsia="Report Sans SC" w:hAnsi="Report Sans SC"/>' \
           '<w:sz w:val="20"/></w:rPr><w:t xml:space="preserve">' + escape(text) + '</w:t></w:r></w:p>'


def table_xml(table):
    rows = []
    for index, row in enumerate([table["headers"], *table["rows"]]):
        props = '<w:trPr><w:cantSplit/>' + ('<w:tblHeader/>' if index == 0 else '') + '</w:trPr>'
        cells = ''.join('<w:tc><w:tcPr><w:tcW w:w="' + str(9660 // len(row)) +
                        '" w:type="dxa"/></w:tcPr>' + paragraph(c) + '</w:tc>' for c in row)
        rows.append('<w:tr>' + props + cells + '</w:tr>')
    grid = '<w:tblGrid>' + ''.join('<w:gridCol w:w="' + str(9660 // len(table["headers"])) + '"/>'
                                    for _ in table["headers"]) + '</w:tblGrid>'
    return '<w:tbl><w:tblPr><w:tblW w:w="9660" w:type="dxa"/></w:tblPr>' + grid + ''.join(rows) + '</w:tbl>'


def make_docx(source):
    from report_images import caption, png

    images, image_rels = {}, []
    body = paragraph(source.get("title") or "信息待补充")
    body += ''.join(paragraph(p["text"] + ' [' + p["source"] + ']') for p in source.get("paragraphs", []))
    if not source.get("paragraphs") and not source.get("tables"):
        body += paragraph("信息不足：尚无可引用的正文或表格。")
    for table in source.get("tables", []):
        body += paragraph("单位：" + (table["unit"] or "未提供") + "；来源：[" + table["source"] + "]")
        body += table_xml(table)
    for index, item in enumerate(source.get("images", []), 1):
        data, width, height = png(item)
        images[f'word/media/image{index}.png'] = data
        image_rels.append((f'image{index}', 'image', f'media/image{index}.png'))
        body += drawing(index, width, height) + paragraph(caption(item))
    body += paragraph("参考来源")
    body += ''.join(paragraph('[' + r["id"] + '] ' + r["citation"]) for r in source.get("references", []))
    body += '<w:sectPr><w:headerReference w:type="default" r:id="header"/>' \
            '<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1240" w:right="1120" ' \
            'w:bottom="1000" w:left="1120" w:header="400" w:footer="400"/></w:sectPr>'
    key = uuid.UUID('12345678-1234-4321-9876-123456789abc')
    font = bytearray(Path('NotoSansSC.ttf').read_bytes())
    mask = key.bytes[::-1]
    for i in range(32):
        font[i] ^= mask[i % 16]
    types = [('document.xml', 'document.main'), ('header1.xml', 'header'), ('fontTable.xml', 'fontTable')]
    entries = {'[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="odttf" ContentType="application/vnd.openxmlformats-officedocument.obfuscatedFont"/>'
               '<Default Extension="png" ContentType="image/png"/>' +
               ''.join('<Override PartName="/word/' + n + '" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.' + t + '+xml"/>' for n, t in types) + '</Types>',
               '_rels/.rels': '<Relationships xmlns="' + REL + '"><Relationship Id="doc" Type="' + R + '/officeDocument" Target="word/document.xml"/></Relationships>',
               'word/document.xml': '<w:document xmlns:w="' + W + '" xmlns:r="' + R + '"><w:body>' + body + '</w:body></w:document>',
               'word/header1.xml': '<w:hdr xmlns:w="' + W + '">' + paragraph(source.get('header') or '报告') + '</w:hdr>',
               'word/fontTable.xml': '<w:fonts xmlns:w="' + W + '" xmlns:r="' + R + '"><w:font w:name="Report Sans SC"><w:embedRegular r:id="font" w:fontKey="{' + str(key).upper() + '}"/></w:font></w:fonts>',
               'word/_rels/document.xml.rels': '<Relationships xmlns="' + REL + '">' + ''.join('<Relationship Id="' + ident + '" Type="' + R + '/' + kind + '" Target="' + target + '"/>' for ident, kind, target in [('header', 'header', 'header1.xml'), ('fonts', 'fontTable', 'fontTable.xml'), *image_rels]) + '</Relationships>',
               'word/_rels/fontTable.xml.rels': '<Relationships xmlns="' + REL + '"><Relationship Id="font" Type="' + R + '/font" Target="fonts/font.odttf"/></Relationships>',
               'word/fonts/font.odttf': bytes(font)}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in {**entries, **images}.items():
            archive.writestr(name, data)
    return output.getvalue()


def drawing(index, width, height):
    size = f'cx="{int(width * 12700)}" cy="{int(height * 12700)}"'
    return ('<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
            f'<wp:extent {size}/><wp:docPr id="{index}" name="Image {index}"/>'
            '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:nvPicPr><pic:cNvPr id="{index}" name="Image {index}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="image{index}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext {size}/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
            '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
