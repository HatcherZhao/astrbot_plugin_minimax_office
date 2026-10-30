"""用真实 AstrBot 组件和真实 Runtime 捕获验证普通成员 Office 链路。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.core.platform.message_type import MessageType

PLUGIN = "astrbot_plugin_minimax_office"
TERMINAL = {"succeeded", "failed", "timed_out"}


class CaptureContext:
    """阻止后台路由接触真实平台。"""

    async def send_message(self, _umo, _chain):
        return True


class CaptureEvent:
    """模拟非管理员群成员，发送只记录到内存。"""

    def __init__(self, component=None, sender="member-100", at=False):
        self.unified_msg_origin = "qq:GroupMessage:office-capture"
        self.message_obj = SimpleNamespace(
            type=MessageType.GROUP_MESSAGE,
            message=[] if component is None else [component],
        )
        self.is_at_or_wake_command = at
        self.sender, self.sent, self.extra = sender, [], {}

    def get_sender_id(self):
        return self.sender

    def set_extra(self, key, value):
        self.extra[key] = value

    async def send(self, chain):
        self.sent.append(chain)


def env_value(path: Path, key: str) -> str:
    """只读取所需 .env 字段，不输出秘密。"""
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def runtime_config(args) -> dict:
    """优先使用覆盖地址，再读取生产插件配置或私有 .env。"""
    stored = {}
    if args.plugin_config.is_file():
        stored = json.loads(args.plugin_config.read_text(encoding="utf-8-sig"))
        stored = stored.get("config", stored) if isinstance(stored, dict) else {}
    token = str(stored.get("runtime_token") or env_value(args.runtime_env_file, "OFFICE_RUNTIME_TOKEN"))
    if len(token) < 32:
        raise RuntimeError("未找到有效 OFFICE_RUNTIME_TOKEN")
    return {
        "runtime_url": args.runtime_url or stored.get("runtime_url") or "http://office-runtime:8030",
        "runtime_token": token,
        "enable_in_group": True,
        "require_at_in_group": True,
        "max_upload_mb": 25,
        "run_wait_seconds": 45,
        "poll_interval_seconds": 1,
        "artifact_retention_days": 7,
    }


def create_code(input_name: str) -> str:
    """读取普通成员文本并生成最小 DOCX。"""
    types = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    rels = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    doc = '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body></w:document>'
    return f'''import html,pathlib,zipfile
text=(pathlib.Path('/work/input')/{input_name!r}).read_text()
with zipfile.ZipFile('/work/output/普通成员报告.docx','w',zipfile.ZIP_DEFLATED) as z:
 z.writestr('[Content_Types].xml',{types!r});z.writestr('_rels/.rels',{rels!r});z.writestr('word/document.xml',{doc!r}.format(text=html.escape(text)))
'''


def edit_code(input_name: str) -> str:
    """读取上一轮生成文件并输出修改版。"""
    return f'''import pathlib,zipfile
src=pathlib.Path('/work/input')/{input_name!r}
with zipfile.ZipFile(src) as a,zipfile.ZipFile('/work/output/普通成员报告-已修改.docx','w',zipfile.ZIP_DEFLATED) as b:
 for item in a.infolist():
  data=a.read(item.filename)
  if item.filename=='word/document.xml':data=data.replace('第一版'.encode(),'第二版'.encode())
  b.writestr(item,data)
'''


async def finish(plugin, event, result: str) -> dict:
    """同步窗口未完成时继续走插件状态工具。"""
    value = json.loads(result)
    deadline = time.monotonic() + 360
    while value.get("state") not in TERMINAL and time.monotonic() < deadline:
        await asyncio.sleep(1)
        value = json.loads(await plugin.minimax_office_status(event, value["job_id"]))
    assert value.get("state") == "succeeded", value
    return value


async def verify(args) -> dict:
    """验证上传、两轮 DOCX、真实 File 回传和跨成员隔离。"""
    sys.path.insert(0, str(args.plugin_dir.parent))
    module = importlib.import_module(f"{PLUGIN}.main")
    with tempfile.TemporaryDirectory(prefix="office-capture-") as temporary:
        data = Path(temporary)
        with patch.object(module.StarTools, "get_data_dir", return_value=data):
            plugin = module.MiniMaxOfficePlugin(CaptureContext(), runtime_config(args))
        source = data / "输入.txt"
        source.write_text("普通成员生成的第一版文档", encoding="utf-8")
        upload = CaptureEvent(module.Comp.File(name=source.name, file=str(source)), at=False)
        await plugin.capture_files(upload)
        assert not upload.sent
        event = CaptureEvent(at=True)
        listed = json.loads(await plugin.minimax_office_files(event))["files"]
        first = await finish(plugin, event, await plugin.minimax_office_run(
            event, "python", create_code(listed[0]["input_name"]), [listed[0]["file_id"]], 120
        ))
        sent = [c for chain in event.sent for c in chain.chain if isinstance(c, module.Comp.File)]
        assert sent
        generated = [x for x in plugin.store.files(plugin._owner(event)) if x.get("generated")]
        assert generated and generated[-1].get("reusable") is True
        edited_event = CaptureEvent(at=True)
        second = await finish(plugin, edited_event, await plugin.minimax_office_run(
            edited_event, "python", edit_code(generated[-1]["input_name"]), [generated[-1]["file_id"]], 120
        ))
        edited = [c for chain in edited_event.sent for c in chain.chain if isinstance(c, module.Comp.File)]
        assert edited
        edited_path = Path(await edited[-1].get_file())
        with zipfile.ZipFile(edited_path) as archive:
            xml = archive.read("word/document.xml").decode()
        assert "第二版" in xml and "第一版" not in xml
        other = CaptureEvent(sender="member-200", at=True)
        assert "没有找到属于当前用户" in await plugin.minimax_office_status(other, first["job_id"])
        assert "没有可用文档" in await plugin.minimax_office_files(other)
        await plugin.terminate()
        return {
            "passed": True, "real_runtime": True, "real_astrbot_components": True,
            "ordinary_group_member": True, "real_platform_sends": 0,
            "first_job_state": first["state"], "second_job_state": second["state"],
            "owner_isolation": True,
            "modified_docx_sha256": hashlib.sha256(edited_path.read_bytes()).hexdigest(),
        }


def main() -> None:
    """解析真实插件、配置和 Runtime 覆盖参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--plugin-config", type=Path, default=Path("/tmp/office-integration-no-config.json"))
    parser.add_argument("--runtime-url", default="")
    parser.add_argument("--runtime-env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/minimax-office-runtime-integration.json"))
    args = parser.parse_args()
    report = asyncio.run(verify(args))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
