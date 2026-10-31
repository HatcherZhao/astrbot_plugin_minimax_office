"""不依赖 AstrBot 运行时的身份、状态和 HTTP 客户端回归测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import owner_key, safe_filename
from policy import can_capture_files, can_execute_tools, has_document_intent
from runtime_client import RuntimeClient
from state import StateStore


class FakeResponse:
    """提供 urllib 响应所需的最小上下文管理接口。"""

    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int = -1):
        return self.body


class IdentityTests(unittest.TestCase):
    def test_owner_is_stable_and_isolates_sender_and_session(self):
        first = owner_key("qq:GroupMessage:1", "100")
        self.assertEqual(first, owner_key("qq:GroupMessage:1", "100"))
        self.assertNotEqual(first, owner_key("qq:GroupMessage:1", "101"))
        self.assertNotEqual(first, owner_key("qq:FriendMessage:100", "100"))
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        with self.assertRaises(ValueError):
            owner_key("", "100")
        with self.assertRaises(ValueError):
            owner_key("qq:GroupMessage:1", "")

    def test_filename_removes_paths_and_controls(self):
        self.assertEqual(safe_filename("../../报告?.docx"), "报告_.docx")
        self.assertEqual(safe_filename("C:\\tmp\\表格.xlsx"), "表格.xlsx")
        self.assertEqual(safe_filename(".."), "document.bin")

    def test_group_file_capture_and_execution_are_separate(self):
        self.assertTrue(can_capture_files(is_group=True, group_enabled=True))
        self.assertFalse(can_execute_tools(
            is_group=True,
            group_enabled=True,
            require_at=True,
            is_at=False,
        ))
        self.assertTrue(can_execute_tools(
            is_group=True,
            group_enabled=True,
            require_at=True,
            is_at=True,
        ))
        self.assertTrue(has_document_intent("帮我改一下 Excel", False, False))
        self.assertTrue(has_document_intent("把刚才那个改一下", False, False))
        self.assertFalse(has_document_intent("今天天气不错", False, False))


class StateTests(unittest.TestCase):
    def test_files_jobs_and_delivery_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            store.add_file("owner-a", {"file_id": "f1", "filename": "a.docx"})
            store.add_job("j1", "owner-a", "qq:FriendMessage:1", "1")
            self.assertEqual(store.owned_file_ids("owner-a"), {"f1"})
            self.assertNotIn("f1", store.owned_file_ids("owner-b"))
            self.assertTrue(store.mark_delivered("j1", "a1"))
            self.assertFalse(store.mark_delivered("j1", "a1"))
            store.mark_artifact_registered("j1", "a1", "generated-f1")
            store.mark_terminal("j1", "succeeded")
            restored = StateStore(path)
            self.assertEqual(restored.job("j1")["delivered"], ["a1"])
            self.assertEqual(
                restored.job("j1")["registered_artifacts"],
                {"a1": "generated-f1"},
            )
            self.assertEqual(restored.pending_jobs(), [])

    def test_prune_files_removes_missing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            existing = Path(directory) / "input.docx"
            existing.write_bytes(b"doc")
            store = StateStore(path)
            store.add_file("owner", {"file_id": "f1", "local_copy": str(existing)})
            store.add_file("owner", {"file_id": "f2", "local_copy": str(Path(directory) / "missing")})
            removed = store.prune_files(lambda item: Path(item["local_copy"]).is_file())
            self.assertEqual(removed, 1)
            self.assertEqual(store.owned_file_ids("owner"), {"f1"})

    def test_terminal_jobs_are_pruned_after_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            store.add_job("old", "owner", "umo", "sender")
            store.mark_terminal("old", "succeeded")
            self.assertEqual(store.prune_terminal_jobs(time.time() + 1), ["old"])
            self.assertIsNone(store.job("old"))


class RuntimeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_request_uses_auth_and_no_proxy(self):
        client = RuntimeClient("http://office-runtime:8030", "secret-token")
        captured = {}

        def fake_open(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(b'{"ok":true}')

        with patch.object(client._opener, "open", side_effect=fake_open):
            result = await client.json_request("GET", "/health")
        self.assertTrue(result["ok"])
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer secret-token")
        # 显式空 ProxyHandler 会阻止 build_opener 注入系统代理；其自身可能被优化掉。
        self.assertFalse(
            any(isinstance(handler, urllib.request.ProxyHandler) for handler in client._opener.handlers)
        )

    async def test_upload_builds_multipart_without_exposing_owner_as_argument(self):
        client = RuntimeClient("http://office-runtime:8030", "token")
        captured = {}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.xlsx"
            source.write_bytes(b"xlsx-data")

            def fake_open(request, timeout):
                captured["body"] = request.data
                captured["type"] = request.get_header("Content-type")
                return FakeResponse(json.dumps({"file_id": "f1", "input_name": "input.xlsx"}).encode())

            with patch.object(client._opener, "open", side_effect=fake_open):
                result = await client.upload("a" * 64, source, "input.xlsx")
        self.assertEqual(result["file_id"], "f1")
        self.assertIn(b'name="owner"', captured["body"])
        self.assertIn(b"xlsx-data", captured["body"])
        self.assertTrue(captured["type"].startswith("multipart/form-data; boundary="))


if __name__ == "__main__":
    unittest.main()


class VideoDocumentPolicyTests(unittest.TestCase):
    """图文要求约束与纯文字请求可以明确区分。"""
    def test_video_images_and_text_only(self):
        from policy import video_images_requested
        self.assertTrue(video_images_requested("把视频重新做成图文PDF"))
        self.assertTrue(video_images_requested("图文PDF", [{"role":"user","content":"https://bilibili.com/video/BV1kERuBYEyy"}]))
        self.assertFalse(video_images_requested("视频做纯文字PDF，不要配图"))
        self.assertFalse(video_images_requested("整理一份销售图文报告"))
        self.assertTrue(video_images_requested("视频做PDF，不要流程图，用截图"))
