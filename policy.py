"""群聊附件捕获、工具执行和文档任务识别策略。"""

from __future__ import annotations

import re

_DOCUMENT_INTENT = re.compile(
    r"(?:word|excel|pptx?|powerpoint|pdf|docx|xlsx|xlsm|文档|表格|幻灯片|演示稿|电子表格|公文|合同|报告|"
    r"(?:刚才|这个|那个).{0,10}(?:改|修改|处理|整理|转换|生成))",
    re.IGNORECASE,
)


def can_capture_files(*, is_group: bool, group_enabled: bool) -> bool:
    """群文件可能无法同条 @，只要群功能开启就允许静默登记。"""
    return not is_group or group_enabled


def can_execute_tools(
    *,
    is_group: bool,
    group_enabled: bool,
    require_at: bool,
    is_at: bool,
) -> bool:
    """执行和文件查询仍遵守群聊 @ 约束。"""
    if not is_group:
        return True
    if not group_enabled:
        return False
    return is_at or not require_at


def has_document_intent(text: str | None, has_files: bool, tool_flow: bool) -> bool:
    """只在当前轮明确文档任务时屏蔽机器人本机执行工具。"""
    return bool(has_files or tool_flow or _DOCUMENT_INTENT.search(str(text or "")))
