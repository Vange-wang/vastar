"""Worker-only literal classification; never evaluate spreadsheet expressions."""
import re
from decimal import Decimal

NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
MONEY = re.compile(r"(?P<currency>[$¥￥€£]|USD|CNY|EUR|GBP)?\s*(?P<n>[+-]?[\d,]+(?:\.\d+)?)\s*(?P<scale>千|万|亿)?\Z")


def number(raw):
    if not NUMBER.fullmatch(raw) or len(raw) > 80:
        return None
    value = Decimal(raw)
    return value if value.is_finite() and abs(value.adjusted()) <= 50 and len(value.as_tuple().digits) <= 30 else None


def cell(raw, kind=None, **extra):
    raw = str(raw)
    if len(raw) > 4096:
        raise ValueError("单元格超过 4096 字符")
    inferred = ("blank" if raw == "" else "formula" if raw.lstrip().startswith(("=", "@", "+"))
                and number(raw) is None else "number" if number(raw) is not None else "text")
    return {"raw": raw, "value": raw, "kind": kind or inferred, **extra}


def characteristics(item):
    raw = item["value"]
    money = MONEY.fullmatch(raw.strip())
    units = []
    if money and (money["currency"] or money["scale"]):
        units = [(money["currency"] or "货币未声明") + (money["scale"] or "基本单位")]
    ambiguous = bool(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw))
    timezone = (re.search(r"(?:Z|[+-]\d\d:\d\d)$", raw).group()
                if "T" in raw and re.search(r"(?:Z|[+-]\d\d:\d\d)$", raw) else
                "未声明" if re.match(r"\d{4}-\d\d-\d\d[ T]", raw) else None)
    return {"units": units, "ambiguous_date": ambiguous, "timezone": timezone}


def identity(row):
    return tuple((c["kind"], c["value"], c.get("formula"), c.get("cached_value")) for c in row["cells"])
