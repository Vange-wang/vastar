"""Isolated external renderer entrypoint; all generated files remain in memory."""
import base64
import io
import json
import sys
from pathlib import Path


def render(source, template):
    import pypdfium2 as pdfium
    from report_docx import make_docx
    from report_layout import make_pdf
    from report_source import reader_safe, validate_source

    validate_source(source)
    pdf, docx = make_pdf(source, template), make_docx(source)
    document = pdfium.PdfDocument(pdf)
    if not 1 <= len(document) <= 40:
        raise ValueError("页数超限")
    pages, text = [], []
    for index in range(len(document)):
        page = document[index]
        textpage = page.get_textpage()
        extracted = textpage.get_text_range()
        if not extracted.strip():
            raise ValueError("发现未预期空页")
        text.append(extracted)
        image = page.render(scale=1).to_pil().convert("RGB")
        # Check actual rendered edge strips, separately from text geometry.
        import PIL.ImageChops
        import PIL.ImageStat
        for box in [(0, 0, image.width, 10), (0, 0, 10, image.height),
                    (image.width - 10, 0, image.width, image.height), (0, image.height - 10, image.width, image.height)]:
            if min(PIL.ImageStat.Stat(image.crop(box)).extrema[i][0] for i in range(3)) < 240:
                raise ValueError("页边出现裁切风险")
        target = io.BytesIO()
        image.save(target, format="PNG")
        pages.append(base64.b64encode(target.getvalue()).decode())
        textpage.close()
        page.close()
    reader_safe(text)
    metadata = document.get_metadata_dict()
    reader_safe(metadata)
    document.close()
    return {"status": "validated", "pdf": base64.b64encode(pdf).decode(),
            "docx": base64.b64encode(docx).decode(), "pages": pages,
            "structure": {"page_count": len(pages), "text": text, "metadata": metadata},
            "visual": {"engine": "PDFium actual PDF raster", "pages_checked": list(range(1, len(pages) + 1)),
                       "edge_check": "passed", "human_layout": "pending user"}}


def main():
    sys.path.insert(0, str(Path.cwd()))
    try:
        source = json.loads(Path("source.json").read_text(encoding="utf-8"))
        template = json.loads(Path("template.json").read_text(encoding="utf-8"))
        result = render(source, template)
    except Exception as exc:
        result = {"status": "incomplete", "message": "未完成输出：受控生成或渲染检查失败。",
                  "diagnostics": {"type": type(exc).__name__, "error": str(exc)}}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
