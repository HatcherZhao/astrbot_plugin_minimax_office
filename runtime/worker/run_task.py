#!/usr/bin/env python3
"""以固定入口运行一项隔离 Office 任务。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path("/work/source")
OUTPUT_ROOT = Path("/work/output")
TEMP_ROOT = Path("/tmp/office-task")
DOTNET_ROOT = Path("/usr/share/dotnet")
DOTNET_LIB_ROOT = Path("/opt/office-worker/dotnet-libs")

SOURCE_NAMES = {
    "python": "main.py",
    "javascript": "main.js",
    "csharp": "Program.cs",
}


def _regular_source(language: str) -> Path:
    """返回固定源码路径并拒绝符号链接或非常规文件。"""
    source = SOURCE_ROOT / SOURCE_NAMES[language]
    if not source.exists() or source.is_symlink() or not source.is_file():
        raise RuntimeError(f"missing regular source file: {source}")
    return source


def _base_environment() -> dict[str, str]:
    """构造不含控制器密钥且适合只读根文件系统的环境。"""
    env = {
        "PATH": "/usr/share/dotnet:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/tmp/home",
        "TMPDIR": "/tmp",
        "PYTHONPATH": "/opt/skills",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DOTNET_ROOT": str(DOTNET_ROOT),
        "DOTNET_CLI_HOME": "/tmp/dotnet-home",
        "DOTNET_NOLOGO": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "NUGET_PACKAGES": "/opt/office-worker/nuget-packages",
        "NODE_PATH": "/opt/office-worker/node_modules",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/office-worker/ms-playwright",
        "FONTCONFIG_PATH": "/etc/fonts",
        "MPLCONFIGDIR": "/tmp/matplotlib",
        "SAL_USE_VCLPLUGIN": "svp",
    }
    # 不继承代理、凭据和 Docker 相关变量；任务本身处于 network=none。
    return env


def _prepare_directories() -> None:
    """验证输出挂载并创建只位于 tmpfs 的运行目录。"""
    if not OUTPUT_ROOT.is_dir() or OUTPUT_ROOT.is_symlink():
        raise RuntimeError("/work/output must be a regular mounted directory")
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    for name in ("home", "dotnet-home", "matplotlib"):
        (Path("/tmp") / name).mkdir(parents=True, exist_ok=True)


def _dotnet_version_dir(parent: Path) -> Path:
    """选择镜像内最新的 .NET 8 目录。"""
    candidates = sorted(
        (path for path in parent.glob("8.0.*") if path.is_dir()),
        key=lambda path: tuple(int(part) for part in path.name.split(".")),
    )
    if not candidates:
        raise RuntimeError(f".NET 8 directory not found under {parent}")
    return candidates[-1]


def _write_runtime_config(path: Path) -> None:
    """生成离线运行所需的固定 .NET runtimeconfig。"""
    version = _dotnet_version_dir(DOTNET_ROOT / "shared/Microsoft.NETCore.App").name
    payload = {
        "runtimeOptions": {
            "tfm": "net8.0",
            "framework": {"name": "Microsoft.NETCore.App", "version": version},
            "rollForward": "LatestPatch",
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _has_dynamic_reference_directive(text: str) -> bool:
    """只识别 C# Script 行首的 #r/#load，不误伤 #region 等预处理指令。"""
    return re.search(r"(?m)^\s*#(?:r|load)\b", text) is not None


def _compile_csharp(source: Path, env: dict[str, str]) -> Path:
    """使用 SDK 自带编译器和固定程序集离线编译 Console 程序。"""
    text = source.read_text(encoding="utf-8-sig")
    if _has_dynamic_reference_directive(text):
        raise RuntimeError("C# task may not use #r or #load; fixed OpenXML references are preinstalled")
    sdk = _dotnet_version_dir(DOTNET_ROOT / "sdk")
    compiler = sdk / "Roslyn/bincore/csc.dll"
    reference_pack = _dotnet_version_dir(
        DOTNET_ROOT / "packs/Microsoft.NETCore.App.Ref"
    ) / "ref/net8.0"
    framework_refs = sorted(reference_pack.glob("*.dll"))
    openxml_refs = sorted(DOTNET_LIB_ROOT.glob("*.dll"))
    if not compiler.is_file() or not framework_refs or not openxml_refs:
        raise RuntimeError("fixed C# compiler references are incomplete")
    runtime_refs: list[Path] = []
    for library in openxml_refs:
        copied = TEMP_ROOT / library.name
        shutil.copy2(library, copied)
        runtime_refs.append(copied)
    prelude = TEMP_ROOT / "CSharpPrelude.cs"
    prelude.write_text(
        "global using System;\n"
        "global using System.Collections.Generic;\n"
        "global using System.IO;\n"
        "global using System.Linq;\n"
        "global using System.Text;\n",
        encoding="utf-8",
    )
    output = TEMP_ROOT / "task.dll"
    command = [
        "dotnet",
        str(compiler),
        "-noconfig",
        "-nostdlib+",
        "-langversion:latest",
        "-nullable:enable",
        "-target:exe",
        f"-out:{output}",
        *(f"-reference:{path}" for path in framework_refs),
        *(f"-reference:{path}" for path in runtime_refs),
        str(prelude),
        str(source),
    ]
    completed = subprocess.run(command, cwd=SOURCE_ROOT, env=env, check=False)
    if completed.returncode:
        raise RuntimeError(f"C# compilation failed with exit code {completed.returncode}")
    _write_runtime_config(TEMP_ROOT / "task.runtimeconfig.json")
    return output


def _command(language: str, source: Path, env: dict[str, str]) -> list[str]:
    """生成不经过 shell 的固定子进程参数。"""
    if language == "python":
        return [sys.executable, "-I", str(source)]
    if language == "javascript":
        return ["node", "--disable-proto=throw", str(source)]
    assembly = _compile_csharp(source, env)
    return ["dotnet", str(assembly)]


def main(argv: list[str] | None = None) -> int:
    """校验语言并以前台进程执行，退出码原样返回控制器。"""
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1 or values[0] not in SOURCE_NAMES:
        print("usage: run-task {python|javascript|csharp}", file=sys.stderr)
        return 64
    language = values[0]
    try:
        _prepare_directories()
        env = _base_environment()
        source = _regular_source(language)
        command = _command(language, source, env)
        return subprocess.run(command, cwd=SOURCE_ROOT, env=env, check=False).returncode
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"worker setup failed: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
