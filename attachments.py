"""平台附件公网 URL 的无代理、限流和 SSRF 安全下载。"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import urllib.parse
from pathlib import Path

try:
    from .runtime_client import RuntimeErrorPublic
except ImportError:  # 支持直接运行本地测试。
    from runtime_client import RuntimeErrorPublic

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3
NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")


def _validated_endpoint(url: str) -> tuple[urllib.parse.ParseResult, str, int]:
    """验证 URL 和全部 DNS 结果，并返回首个已固定公网 IP。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        raise RuntimeErrorPublic("附件下载地址无效或不允许访问。")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise RuntimeErrorPublic("附件下载地址端口无效。") from exc
    host = parsed.hostname.encode("idna").decode("ascii")
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise RuntimeErrorPublic("附件下载地址无法解析。") from exc
    addresses = []
    for family, _, _, _, sockaddr in answers:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise RuntimeErrorPublic("附件下载地址解析结果无效。")
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise RuntimeErrorPublic("附件下载地址解析结果无效。") from exc
        if address.version == 6 and address.ipv4_mapped is not None:
            raise RuntimeErrorPublic("附件下载地址不能使用 IPv4 映射 IPv6。")
        if address.version == 6 and (address.sixtofour is not None or address.teredo is not None):
            raise RuntimeErrorPublic("附件下载地址不能使用 IPv6 过渡地址。")
        if address.version == 6 and address in NAT64_WELL_KNOWN:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            if not embedded.is_global:
                raise RuntimeErrorPublic("附件下载地址不能通过 NAT64 访问非公网地址。")
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or getattr(address, "is_site_local", False)
        ):
            raise RuntimeErrorPublic("附件下载地址不是公网地址。")
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise RuntimeErrorPublic("附件下载地址没有可用公网地址。")
    return parsed, addresses[0], port


def _pinned_connection(
    parsed: urllib.parse.ParseResult,
    pinned_ip: str,
    port: int,
    timeout: int,
) -> http.client.HTTPConnection:
    """连接固定到已验证 IP；HTTPS 仍用原域名完成 SNI 和证书校验。"""
    host = (parsed.hostname or "").encode("idna").decode("ascii")
    if parsed.scheme == "https":
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)

    def create_connection(_address, connect_timeout=timeout, source_address=None):
        """忽略连接阶段再次解析出的主机名，始终使用已验证 IP。"""
        return socket.create_connection((pinned_ip, port), connect_timeout, source_address)

    connection._create_connection = create_connection
    return connection


def download_public_url(url: str, target: Path, max_bytes: int, timeout: int = 60) -> None:
    """最多跟随三次公网重定向并流式保存附件。"""
    temporary = target.with_suffix(target.suffix + ".part")
    current = url
    total = 0
    deadline = time.monotonic() + timeout
    try:
        for redirects in range(MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeErrorPublic("附件下载超时。")
            parsed, pinned_ip, port = _validated_endpoint(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeErrorPublic("附件下载超时。")
            connection = _pinned_connection(parsed, pinned_ip, port, remaining)
            path = urllib.parse.urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
            try:
                connection.request("GET", path, headers={"User-Agent": "AstrBot-MiniMax-Office/1.0"})
                response = connection.getresponse()
                if response.status in REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    if not location or redirects >= MAX_REDIRECTS:
                        raise RuntimeErrorPublic("附件下载重定向无效或次数过多。")
                    current = urllib.parse.urljoin(current, location)
                    continue
                if not 200 <= response.status < 300:
                    raise RuntimeErrorPublic(f"附件下载失败（HTTP {response.status}）。")
                length = response.getheader("Content-Length")
                declared = None
                if length:
                    try:
                        declared = int(length)
                    except ValueError as exc:
                        raise RuntimeErrorPublic("附件下载响应长度无效。") from exc
                    if declared < 0 or declared > max_bytes:
                        raise RuntimeErrorPublic(f"附件超过 {max_bytes // 1024 // 1024} MiB，已停止下载。")
                with temporary.open("wb") as output:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeErrorPublic("附件下载超时。")
                        if connection.sock is not None:
                            connection.sock.settimeout(remaining)
                        chunk = response.read1(min(64 * 1024, max_bytes + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeErrorPublic(
                                f"附件超过 {max_bytes // 1024 // 1024} MiB，已停止下载。"
                            )
                        output.write(chunk)
                if total <= 0:
                    raise RuntimeErrorPublic("附件内容为空。")
                if declared is not None and total != declared:
                    raise RuntimeErrorPublic("附件下载内容长度与响应声明不一致。")
                temporary.replace(target)
                return
            except (http.client.HTTPException, TimeoutError, OSError) as exc:
                raise RuntimeErrorPublic("附件公网下载失败，请重新上传后再试。") from exc
            finally:
                connection.close()
        raise RuntimeErrorPublic("附件下载重定向次数过多。")
    finally:
        temporary.unlink(missing_ok=True)
