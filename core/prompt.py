"""Vastar runtime component."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.errors import ErrorCategory, ErrorCode, VastarError


@dataclass
class PromptBundle:
    version: str
    system_text: str
    task_text: str
    examples_text: str   # 不参与渲染（仅入 content_hash/files——examples 阶段一为占位）
    files: dict[str, str] = field(default_factory=dict)   # 文件名 -> sha256

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [self.version, self.system_text, self.task_text, self.examples_text],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def rendered_system(self) -> str:
        """阶段一 system 提示 = system.md 原文；task.md 剥注释后非占位时以空行拼接，
        examples.md 不参与渲染（examples_text 仅计入 content_hash，设计内）。"""
        parts = [self.system_text]
        if self.task_text.strip() and not _is_placeholder(self.task_text):
            parts.append(self.task_text)
        return "\n\n".join(p for p in parts if p.strip())


_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def strip_comments(text: str) -> str:
    """Vastar runtime component."""
    return _COMMENT_RE.sub("", text)


def _is_placeholder(text: str) -> bool:
    """占位文件剥离注释后不含正文，不参与拼接。"""
    return strip_comments(text).strip() == ""


def build_prompt(prompt_cfg: dict, base_dir: Path) -> PromptBundle:
    """Vastar runtime component."""
    dir_path = base_dir / prompt_cfg.get("dir", "prompts/")
    files: dict[str, str] = {}
    texts: dict[str, str] = {}
    for key in ("system", "task", "examples"):
        name = prompt_cfg.get(key, f"{key}.md")
        path = dir_path / name
        if not path.exists():
            
            raise VastarError(
                code=ErrorCode.PARAM_ERROR, category=ErrorCategory.PARAM,
                message=(f"prompt 缺失：{path}（{key} 件必须存在；"
                         "空占位文件合法，文件缺失不合法）"))
        text = path.read_text(encoding="utf-8")
        text = strip_comments(text).strip()   # 剥离元数据 + 首尾空白（行为一致）
        texts[key] = text
        if text:
            files[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PromptBundle(
        version=prompt_cfg.get("version", "v0"),
        system_text=texts["system"],
        task_text=texts["task"],
        examples_text=texts["examples"],
        files=files,
    )
