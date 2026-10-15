"""Pinned external renderer kit staged into a readonly offline AppContainer."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

from core.tools.document_adapter import fingerprint
from core.tools.report_source import validate_source

ASSETS = Path(__file__).resolve().parents[2] / "skills" / "report-generation" / "assets"
LIMITS = {"input_bytes": 1024 * 1024, "output_bytes": 48 * 1024 * 1024,
          "memory_bytes": 768 * 1024 * 1024, "wall_seconds": 90}


def resources(root=ASSETS):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if Path(name).name != name or hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("受控字体/模板/渲染组件缺失或版本不一致")
    for name, expected in manifest.get("renderer_sources", {}).items():
        code = Path(__file__).with_name(name).read_text(encoding="utf-8").encode()
        if hashlib.sha256(code).hexdigest() != expected:
            raise ValueError("渲染脚本版本不一致")
    return manifest


class ReportPayload:
    private_output = True

    def __init__(self, path, staging):
        self.path, self.staging = Path(path), Path(staging)
        if self.path.stat().st_size > LIMITS["input_bytes"]:
            raise ValueError("报告源超过 1 MiB")
        self.before = fingerprint(path)
        raw = self.path.read_bytes()
        self.staging.mkdir(parents=True, exist_ok=False)
        (self.staging / "source.json").write_bytes(raw)
        (self.staging / "STATUS.txt").write_text("未完成输出\n", encoding="utf-8")
        self.source = validate_source(json.loads(raw))
        self.versions = resources()
        self.manifest = {"source_hash": self.before, "source": str(path), "versions": self.versions,
                         "limits": dict(LIMITS)}
        self.script = ".\\renderer\\python.exe -I -B -X utf8 .\\report_render.py; exit $LASTEXITCODE"
        self.approval_lines = ["只读来源，私有暂存生成；验证通过才发布，不覆盖原件。",
                               "外部依赖、非内置：" + self.versions["renderer_version"],
                               "离线 AppContainer；原始输出不写进程 artifact；真人版式待用户。"]

    def stage(self, profile):
        resources()
        if fingerprint(self.path) != self.before:
            raise ValueError("source changed")
        with zipfile.ZipFile(ASSETS / "renderer-win-x64.zip") as archive:
            archive.extractall(profile.work / "renderer")
        for name in ("NotoSansSC.ttf", "template.json"):
            shutil.copy2(ASSETS / name, profile.work / name)
        for name in ("report_render", "report_docx", "report_layout", "report_source", "report_images"):
            shutil.copy2(Path(__file__).with_name(name + ".py"), profile.work / (name + ".py"))
        (profile.work / "source.json").write_text(json.dumps(self.source, ensure_ascii=False), encoding="utf-8")
        profile.memory_bytes = LIMITS["memory_bytes"]

    def verify_unchanged(self):
        after = fingerprint(self.path)
        if after != self.before:
            raise ValueError("source changed; publication refused")
        return {"before": self.before, "after": after, "unchanged": True}
