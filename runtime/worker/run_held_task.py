#!/usr/bin/env python3
"""执行固定 Office 任务，写入完成标志并短暂保留容器供控制器归档。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

LANGUAGES = frozenset({"python", "javascript", "csharp"})
RUNNER = "/opt/office-worker/run-task"
RESULT_PATH = Path("/tmp/office-worker-result.json")
HOLD_SECONDS = 330
_stopping = False


def _stop(_signum, _frame) -> None:
    """收到控制器停止信号后结束等待。"""
    global _stopping
    _stopping = True


def _write_result(exit_code: int) -> None:
    """原子写入只包含退出码和完成时间的状态文件。"""
    payload = {
        "exit_code": int(exit_code),
        "finished_at_unix": int(time.time()),
    }
    temporary = RESULT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, RESULT_PATH)


def main(argv: list[str] | None = None) -> int:
    """运行受控入口；完成后保留容器，最终返回任务真实退出码。"""
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1 or values[0] not in LANGUAGES:
        print("usage: run-held-task {python|javascript|csharp}", file=sys.stderr)
        return 64
    language = values[0]
    exit_code = 70
    try:
        completed = subprocess.run([RUNNER, language], check=False)
        exit_code = int(completed.returncode)
    except OSError as exc:
        print(f"worker launch failed: {exc}", file=sys.stderr)
    try:
        _write_result(exit_code)
    except OSError as exc:
        print(f"worker result write failed: {exc}", file=sys.stderr)
        return 71

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    deadline = time.monotonic() + HOLD_SECONDS
    while not _stopping and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
