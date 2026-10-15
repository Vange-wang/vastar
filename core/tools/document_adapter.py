"""Trusted, bounded staging into B27's private readonly package; no host parser."""
import hashlib
import shutil
import sys
from pathlib import Path

MAX_INPUT = 64 * 1024 * 1024
LIMITS = {"input_bytes": MAX_INPUT, "output_bytes": 4 * 1024 * 1024,
          "memory_bytes": 512 * 1024 * 1024, "wall_seconds": 30}


def fingerprint(path, target=None):
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as source:
        while chunk := source.read(65536):
            size += len(chunk)
            if size > MAX_INPUT:
                raise ValueError("document exceeds 64 MiB input limit; select a smaller source")
            digest.update(chunk)
            if target is not None:
                target.write(chunk)
    return digest.hexdigest()


def capabilities():
    pdf = shutil.which("pdftotext")
    return {"txt": {"level": "partial", "engine": "isolated stdlib UTF-8, bounded window"},
            "md": {"level": "partial", "engine": "isolated stdlib UTF-8, bounded window"},
            "docx": {"level": "partial", "engine": "isolated stdlib OOXML; no layout renderer"},
            "pdf": {"level": "partial" if pdf else "needs_external_tool",
                    "engine": "依赖外部程序、非内置：Poppler pdftotext", "executable": pdf},
            **{ext: {"level": "unsupported", "engine": "no OCR or legacy/macro parser"}
               for ext in ("doc", "docm", "xlsm", "png", "jpg", "jpeg", "tiff", "unknown")}}


class DocumentPayload:
    def __init__(self, path, *, first=1, count=5, ocr=False):
        if type(first) is not int or first < 1 or type(count) is not int or not 1 <= count <= 10:
            raise ValueError("first >= 1 and count in [1,10] required")
        if type(ocr) is not bool:
            raise ValueError("ocr must be boolean")
        self.path, self.kind = Path(path), Path(path).suffix.lower().lstrip(".")
        self.capability = capabilities().get(self.kind, capabilities()["unknown"])
        self.before = fingerprint(self.path)
        self.manifest = {"source": str(self.path), "source_hash": self.before, "kind": self.kind,
                         "first": first, "count": count, "ocr": ocr, "limits": dict(LIMITS),
                         "capability": self.capability}
        self.script = ".\\python\\python.exe -I -B -X utf8 .\\worker.py .\\request.json; exit $LASTEXITCODE"
        self.approval_lines = ["只读提取输入：" + str(self.path), "SHA256: " + self.before,
                               "隔离副本；无网络/凭据；512 MiB Job 总内存、30 秒、4 MiB 输出。",
                               str(self.capability)]

    def stage(self, profile):
        import json

        work = profile.work
        with (work / "source").open("wb") as target:
            if fingerprint(self.path, target) != self.before:
                raise ValueError("source changed before isolated parsing")
        base, runtime = Path(sys.base_prefix), work / "python"
        runtime.mkdir()
        for child in base.iterdir():
            if child.name == "python.exe" or child.suffix.lower() == ".dll":
                shutil.copy2(child, runtime / child.name)
        for name in ("DLLs", "Lib"):
            shutil.copytree(base / name, runtime / name,
                            ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test", "tests"))
        shutil.copy2(Path(__file__).with_name("document_worker.py"), work / "worker.py")
        (work / "request.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        if self.kind == "pdf":
            binary = Path(self.capability["executable"])
            dest = work / "poppler"
            dest.mkdir()
            for child in binary.parent.iterdir():
                if child.name.casefold() == binary.name.casefold() or child.suffix.lower() == ".dll":
                    shutil.copy2(child, dest / child.name)
        profile.memory_bytes = self.manifest["limits"]["memory_bytes"]

    def verify_unchanged(self):
        after = fingerprint(self.path)
        if after != self.before:
            raise ValueError("source hash changed; extraction evidence rejected")
        return {"before": self.before, "after": after, "unchanged": True}
