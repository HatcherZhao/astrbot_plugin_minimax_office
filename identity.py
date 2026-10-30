"""会话身份和文件名的确定性处理。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")


def owner_key(unified_msg_origin: str, sender_id: str) -> str:
    """使用真实会话和发送者生成不可逆 owner，不接受模型传参。"""
    if not str(unified_msg_origin).strip() or not str(sender_id).strip():
        raise ValueError("owner identity requires both UMO and sender ID")
    source = f"{unified_msg_origin}\0{sender_id}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def safe_filename(name: str | None, fallback: str = "document.bin") -> str:
    """去除路径和控制字符，生成单层安全文件名。"""
    candidate = Path(str(name or "").replace("\\", "/")).name.replace("\x00", "").strip()
    candidate = _SAFE_NAME.sub("_", candidate).strip(" .")
    if candidate in {"", ".", ".."}:
        candidate = fallback
    return candidate[:180]
