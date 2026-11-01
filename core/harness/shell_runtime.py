"""Resolve and validate the selected PowerShell, including Windows App Execution Aliases."""

import json
import os
import shutil
import subprocess
from pathlib import Path


def _failure(source, real, target, exc):
    return OSError(
        f"PowerShell runtime unavailable: source={source}; realpath={real}; target={target}; "
        f"errno={getattr(exc, 'errno', None)}; reason={exc}. "
        "Set VASTAR_PWSH_PATH to a working physical PowerShell 7 pwsh.exe; no fallback used."
    )


def select_shell(target="preflight"):
    source = os.environ.get("VASTAR_PWSH_PATH") or shutil.which("pwsh")
    real = "unresolved"
    try:
        if not source:
            raise FileNotFoundError(2, "PowerShell 7 not found on PATH")
        real = Path(source).resolve()
        real.lstat()  # AppExecLink supports lstat even when normal stat/open fails.
        return str(source), real
    except OSError as exc:
        raise _failure(source, real, target, exc) from None


def resolve_shell(target="preflight"):
    source, real = select_shell(target)
    try:
        # AppExecLink is not a normal symlink: Python realpath leaves it intact.
        # Ask the explicitly selected process for its own executable, never another shell.
        probe = subprocess.run(
            [str(real), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             "@{major=$PSVersionTable.PSVersion.Major;path=(Get-Process -Id $PID).Path}"
             " | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
        )
        info = json.loads(probe.stdout.strip().lstrip("\ufeff"))
        if info["major"] != 7:
            raise ValueError("PowerShell 7 required")
        physical = Path(info["path"]).resolve(strict=True)
        if physical.name.casefold() != "pwsh.exe" or not physical.is_file():
            raise ValueError("selected process did not identify a physical pwsh.exe")
        # Verify copyability: the alias itself reports a zero-length file and cannot be read.
        with physical.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError("selected executable is not a Windows PE image")
        return str(source), physical
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise _failure(source, real, target, exc) from None


def install_shell(target):
    source, real = resolve_shell(target)
    try:
        shutil.copytree(real.parent, target)
    except (OSError, shutil.Error) as exc:
        raise _failure(source, real, target, exc) from None
