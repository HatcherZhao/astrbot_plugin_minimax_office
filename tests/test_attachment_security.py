"""验证公网附件下载的 SSRF、重定向、固定 IP 和流式上限。"""

from __future__ import annotations

import io
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import attachments
from runtime_client import RuntimeErrorPublic


class FakeSocket:
    """记录整体时间预算写入的 socket timeout。"""

    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeResponse:
    """提供 http.client 响应所需接口。"""

    def __init__(self, body=b"", status=200, headers=None):
        self.stream = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}

    def getheader(self, name):
        return self.headers.get(name)

    def read1(self, size=-1):
        return self.stream.read(size)


class FakeConnection:
    """模拟直连，并主动调用实现注入的固定 IP 连接器。"""

    def __init__(self, host, port, response, **_kwargs):
        self.host, self.port, self.response = host, port, response
        self.sock = None
        self.request_path = None

    def request(self, _method, path, headers=None):
        self.request_path = path
        self.sock = self._create_connection((self.host, self.port), 60, None)

    def getresponse(self):
        return self.response

    def close(self):
        pass


def answer(ip: str):
    """生成单个 TCP DNS 解析结果。"""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


class AttachmentSecurityTests(unittest.TestCase):
    """所有测试均替换连接器，不访问真实公网或内网。"""

    def test_rejects_non_http_userinfo_and_private_literals(self):
        for url in ("file:///etc/passwd", "http://user:pass@example.com/a"):
            with self.subTest(url=url), self.assertRaises(RuntimeErrorPublic):
                attachments._validated_endpoint(url)
        for ip in (
            "127.0.0.1", "10.0.0.1", "169.254.169.254", "192.0.2.1", "::1",
            "::ffff:8.8.8.8", "2002:0808:0808::", "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
            "64:ff9b::a00:1", "fec0::1", "224.0.0.1",
        ):
            with self.subTest(ip=ip), patch.object(attachments.socket, "getaddrinfo", return_value=answer(ip)):
                with self.assertRaises(RuntimeErrorPublic):
                    attachments._validated_endpoint(f"http://[{ip}]/a" if ":" in ip else f"http://{ip}/a")

    def test_rejects_dns_with_any_private_answer(self):
        mixed = answer("93.184.216.34") + answer("192.168.1.2")
        with patch.object(attachments.socket, "getaddrinfo", return_value=mixed):
            with self.assertRaises(RuntimeErrorPublic):
                attachments._validated_endpoint("https://files.example/a.pdf")

    def test_redirect_target_is_revalidated_and_private_is_rejected(self):
        response = FakeResponse(status=302, headers={"Location": "http://metadata.internal/latest"})
        connection = FakeConnection("files.example", 80, response)
        resolutions = [answer("93.184.216.34"), answer("169.254.169.254")]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(attachments.socket, "getaddrinfo", side_effect=resolutions), \
                patch.object(attachments.socket, "create_connection", return_value=FakeSocket()), \
                patch.object(attachments.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(RuntimeErrorPublic):
                attachments.download_public_url(
                    "http://files.example/a", Path(directory) / "a.pdf", 1024
                )

    def test_public_download_is_streamed_and_pinned(self):
        response = FakeResponse(b"public-data", headers={"Content-Length": "11"})
        connection = FakeConnection("files.example", 80, response)
        fake_socket = FakeSocket()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(attachments.socket, "getaddrinfo", return_value=answer("93.184.216.34")), \
                patch.object(attachments.socket, "create_connection", return_value=fake_socket) as connect, \
                patch.object(attachments.http.client, "HTTPConnection", return_value=connection):
            target = Path(directory) / "a.pdf"
            attachments.download_public_url("http://files.example/a?q=1", target, 32)
            self.assertEqual(target.read_bytes(), b"public-data")
            self.assertEqual(connection.request_path, "/a?q=1")
            self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 80))
            self.assertTrue(fake_socket.timeouts)

    def test_content_length_and_stream_hard_limits_clean_partial(self):
        for response in (
            FakeResponse(b"", headers={"Content-Length": "9"}),
            FakeResponse(b"123456789"),
            FakeResponse(b"12345", headers={"Content-Length": "10"}),
        ):
            with self.subTest(headers=response.headers), tempfile.TemporaryDirectory() as directory, \
                    patch.object(attachments.socket, "getaddrinfo", return_value=answer("93.184.216.34")), \
                    patch.object(attachments.socket, "create_connection", return_value=FakeSocket()), \
                    patch.object(
                        attachments.http.client,
                        "HTTPConnection",
                        return_value=FakeConnection("files.example", 80, response),
                    ):
                target = Path(directory) / "large.pdf"
                with self.assertRaises(RuntimeErrorPublic):
                    attachments.download_public_url("http://files.example/large", target, 8)
                self.assertFalse(target.exists())
                self.assertFalse(target.with_suffix(".pdf.part").exists())

    def test_https_uses_original_hostname_and_pinned_ip(self):
        response = FakeResponse(b"tls", headers={"Content-Length": "3"})
        created = []

        def https_factory(host, port, **kwargs):
            created.append((host, port, kwargs.get("context")))
            return FakeConnection(host, port, response)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(attachments.socket, "getaddrinfo", return_value=answer("93.184.216.34")), \
                patch.object(attachments.socket, "create_connection", return_value=FakeSocket()) as connect, \
                patch.object(attachments.http.client, "HTTPSConnection", side_effect=https_factory):
            attachments.download_public_url(
                "https://cdn.example/document.pdf", Path(directory) / "document.pdf", 16
            )
        self.assertEqual(created[0][0:2], ("cdn.example", 443))
        self.assertIsNotNone(created[0][2])
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))

    def test_overall_deadline_is_checked_before_network_access(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(attachments.time, "monotonic", side_effect=[0, 61]), \
                patch.object(attachments.socket, "getaddrinfo") as resolver:
            with self.assertRaisesRegex(RuntimeErrorPublic, "超时"):
                attachments.download_public_url(
                    "https://files.example/a", Path(directory) / "a.pdf", 1024, timeout=60
                )
            resolver.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
