"""Byte-offset pagination with complete UTF-8 code points and forward progress."""


def page_end(data: bytes, offset: int, page_size: int, *, text: bool) -> int:
    """Text pages may exceed a tiny requested size up to one full code point (4 bytes)."""
    if not 0 <= offset <= len(data):
        raise ValueError("游标偏移超出产物范围")
    if text and offset < len(data) and data[offset] & 0xC0 == 0x80:
        raise ValueError("游标偏移不在 UTF-8 字符边界")
    end = min(offset + page_size, len(data))
    if not text:
        return end
    while end < len(data) and data[end] & 0xC0 == 0x80:
        end -= 1
    if end == offset and offset < len(data):
        end = offset + 1
        while end < len(data) and data[end] & 0xC0 == 0x80:
            end += 1
    return end
