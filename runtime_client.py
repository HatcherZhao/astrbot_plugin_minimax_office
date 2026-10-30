"""MiniMax Office 隔离执行服务 HTTP 客户端。"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class RuntimeErrorPublic(RuntimeError):
    """可安全显示给用户的执行服务错误。"""


class RuntimeClient:
    """使用无代理直连 Docker 内部服务，令牌只放请求头。"""

    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _request_sync(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
        timeout: int | None = None,
    ) -> tuple[bytes, str]:
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=self._headers(content_type),
            method=method,
        )
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as response:
                return response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            body = exc.read(2048).decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("detail") or body
            except json.JSONDecodeError:
                message = body
            if exc.code == 429:
                raise RuntimeErrorPublic("Office 任务队列已满，请稍后再试。") from exc
            raise RuntimeErrorPublic(f"Office 执行服务拒绝请求（HTTP {exc.code}）：{message}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeErrorPublic("Office 执行服务暂不可达，请稍后再试。") from exc

    async def json_request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        """异步发送 JSON 请求并验证对象响应。"""
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw, _ = await asyncio.to_thread(
            self._request_sync,
            method,
            path,
            data=data,
            content_type="application/json" if data is not None else None,
            timeout=timeout,
        )
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeErrorPublic("Office 执行服务返回了无效数据。") from exc
        if not isinstance(value, dict):
            raise RuntimeErrorPublic("Office 执行服务响应格式不正确。")
        return value

    async def upload(self, owner: str, path: Path, filename: str) -> dict:
        """以 multipart 上传一个已落盘的可信附件。"""
        boundary = "----minimaxoffice" + secrets.token_hex(16)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"owner\"\r\n\r\n{owner}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{filename.replace(chr(34), '_')}\"\r\nContent-Type: {mime}\r\n\r\n"
            ).encode("utf-8"),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        raw, _ = await asyncio.to_thread(
            self._request_sync,
            "POST",
            "/v1/files",
            data=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        value = json.loads(raw)
        if not isinstance(value, dict) or not value.get("file_id"):
            raise RuntimeErrorPublic("Office 执行服务没有返回文件 ID。")
        return value

    async def submit_job(self, owner: str, language: str, code: str, file_ids: list[str], timeout: int) -> dict:
        """提交固定语言任务，不接收镜像、挂载或 shell 参数。"""
        return await self.json_request("POST", "/v1/jobs", {
            "owner": owner,
            "language": language,
            "code": code,
            "input_file_ids": file_ids,
            "timeout_seconds": timeout,
        })

    async def job(self, owner: str, job_id: str) -> dict:
        """查询 owner 对应任务。"""
        query = urllib.parse.urlencode({"owner": owner})
        return await self.json_request("GET", f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}?{query}")

    async def artifact(self, owner: str, job_id: str, artifact_id: str) -> tuple[bytes, str]:
        """下载已登记产物，路径段全部 URL 编码。"""
        query = urllib.parse.urlencode({"owner": owner})
        path = (
            f"/v1/jobs/{urllib.parse.quote(job_id, safe='')}/artifacts/"
            f"{urllib.parse.quote(artifact_id, safe='')}?{query}"
        )
        return await asyncio.to_thread(self._request_sync, "GET", path, timeout=120)
