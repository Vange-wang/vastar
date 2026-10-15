"""Bounded PNG bytes with literal source and permission captions."""
import base64
import io


def png(item):
    from PIL import Image

    data = base64.b64decode(item["png_base64"], validate=True)
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 512 * 1024:
        raise ValueError("图片缺失、格式不是 PNG 或超过限额")
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 4000000:
            raise ValueError("图片像素超限")
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    ratio = min(1, 460 / width, 260 / height)
    return data, width * ratio, height * ratio


def caption(item):
    return item["caption"] + ' [' + item["source"] + ']；图片授权：' + item["license"]
