"""MiniMax Office 隔离任务 API 与单并发 Docker 调度器。"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import shutil
import sqlite3
import tarfile
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Callable, Iterator, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

try:
    import docker
except ImportError:  # 测试通过工厂注入客户端，不要求本机安装 Docker SDK。
    docker = None  # type: ignore[assignment]


OWNER_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")
INPUT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".ppt",
    ".pptx",
    ".pdf",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
    ".zip",
}
OUTPUT_EXTENSIONS = {
    ".docx",
    ".xlsx",
    ".xlsm",
    ".pptx",
    ".pdf",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
}
SOURCE_NAMES = {"python": "main.py", "javascript": "main.js", "csharp": "Program.cs"}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_FILES = 10
MAX_LOG_CHARS = 16_000
MAX_QUEUE_DEPTH = 4
MAX_ARCHIVE_BYTES = 65 * 1024 * 1024
DEFAULT_OWNER_QUOTA_BYTES = 200 * 1024 * 1024
DEFAULT_GLOBAL_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class Settings:
    """控制器运行配置，所有 Docker 参数均由服务端固定。"""

    token: str
    data_root: Path
    host_data_root: Path
    worker_image: str
    worker_user: str = "10001:10001"
    owner_quota_bytes: int = DEFAULT_OWNER_QUOTA_BYTES
    global_quota_bytes: int = DEFAULT_GLOBAL_QUOTA_BYTES
    retention_seconds: int = DEFAULT_TTL_SECONDS

    def __post_init__(self) -> None:
        """拒绝无效配额和保留时间，避免配置绕过资源边界。"""
        if self.owner_quota_bytes <= 0 or self.global_quota_bytes <= 0:
            raise ValueError("存储配额必须大于零")
        if self.owner_quota_bytes > self.global_quota_bytes:
            raise ValueError("owner 配额不能超过全局配额")
        if self.retention_seconds < 60:
            raise ValueError("保留时间不能少于 60 秒")

    @property
    def instance_id(self) -> str:
        """按宿主数据根生成稳定实例标识，隔离多套控制器的 worker。"""
        return hashlib.sha256(str(self.host_data_root).encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量读取部署参数，并拒绝弱认证配置。"""
        token = os.environ.get("OFFICE_RUNTIME_TOKEN", "")
        if len(token) < 32:
            raise RuntimeError("OFFICE_RUNTIME_TOKEN 至少需要 32 个字符")
        worker_image = os.environ.get("OFFICE_WORKER_IMAGE", "").strip()
        if not worker_image:
            raise RuntimeError("OFFICE_WORKER_IMAGE 不能为空")
        return cls(
            token=token,
            data_root=Path(os.environ.get("OFFICE_DATA_ROOT", "/data")).resolve(),
            host_data_root=Path(os.environ.get("OFFICE_HOST_DATA_ROOT", "/data")).resolve(),
            worker_image=worker_image,
            worker_user=os.environ.get("OFFICE_WORKER_USER", "10001:10001"),
            owner_quota_bytes=int(
                os.environ.get("OFFICE_OWNER_QUOTA_BYTES", DEFAULT_OWNER_QUOTA_BYTES)
            ),
            global_quota_bytes=int(
                os.environ.get("OFFICE_GLOBAL_QUOTA_BYTES", DEFAULT_GLOBAL_QUOTA_BYTES)
            ),
            retention_seconds=int(
                os.environ.get("OFFICE_RETENTION_SECONDS", DEFAULT_TTL_SECONDS)
            ),
        )


class JobRequest(BaseModel):
    """文档任务请求；不暴露镜像、命令、挂载或 Docker 参数。"""

    model_config = ConfigDict(extra="forbid")
    owner: str
    language: Literal["python", "javascript", "csharp"]
    code: str = Field(min_length=1, max_length=100_000)
    input_file_ids: list[str] = Field(default_factory=list, max_length=10)
    timeout_seconds: int = Field(default=180, ge=10, le=300)

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, value: str) -> str:
        """强制 owner 为插件生成的 SHA-256 十六进制值。"""
        if not OWNER_RE.fullmatch(value):
            raise ValueError("owner 格式无效")
        return value

    @field_validator("input_file_ids")
    @classmethod
    def validate_file_ids(cls, values: list[str]) -> list[str]:
        """拒绝重复或非 UUID 文件标识。"""
        if len(set(values)) != len(values):
            raise ValueError("input_file_ids 不能重复")
        for value in values:
            try:
                uuid.UUID(value)
            except ValueError as exc:
                raise ValueError("input_file_ids 包含无效 UUID") from exc
        return values


class Store:
    """使用 SQLite 持久保存文件、任务和产物归属。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "state.db"
        self.lock = threading.RLock()
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """建立带行字典与外键约束的短连接。"""
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        """创建持久状态表和必要索引。"""
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    file_id TEXT PRIMARY KEY, owner TEXT NOT NULL, filename TEXT NOT NULL,
                    input_name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    path TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, owner TEXT NOT NULL, language TEXT NOT NULL,
                    code_path TEXT NOT NULL, input_file_ids TEXT NOT NULL, timeout_seconds INTEGER NOT NULL,
                    state TEXT NOT NULL, stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
                    error TEXT, created_at REAL NOT NULL, started_at REAL, finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_owner_state ON jobs(owner, state);
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, owner TEXT NOT NULL,
                    filename TEXT NOT NULL, size INTEGER NOT NULL, mime_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL, path TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                """
            )

    def recover(self) -> list[str]:
        """将中断的运行任务标记失败，并返回需要继续调度的排队任务。"""
        now = time.time()
        with self.lock, self.connect() as connection:
            interrupted = connection.execute(
                "SELECT job_id,code_path FROM jobs WHERE state='running'"
            ).fetchall()
            connection.execute(
                "UPDATE jobs SET state='failed', error=?, finished_at=? WHERE state='running'",
                ("控制器重启，中断的任务未重复执行", now),
            )
            rows = connection.execute(
                "SELECT job_id FROM jobs WHERE state='queued' ORDER BY created_at"
            ).fetchall()
        for row in interrupted:
            self.cleanup_job_staging(str(row["job_id"]), str(row["code_path"]))
        return [str(row["job_id"]) for row in rows]

    def create_job(self, request: JobRequest, code_path: Path, files: list[sqlite3.Row]) -> str:
        """在事务内检查 owner 与全局队列限制并创建任务。"""
        job_id = str(uuid.uuid4())
        with self.lock, self.connect() as connection:
            owner_count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner=? AND state IN ('queued','running')",
                (request.owner,),
            ).fetchone()[0]
            if owner_count:
                raise HTTPException(429, "当前用户已有未完成任务")
            counts = dict(
                connection.execute(
                    "SELECT state, COUNT(*) count FROM jobs WHERE state IN ('queued','running') GROUP BY state"
                ).fetchall()
            )
            if int(counts.get("queued", 0)) >= MAX_QUEUE_DEPTH:
                raise HTTPException(429, "任务队列已满")
            if any(str(row["owner"]) != request.owner for row in files):
                raise HTTPException(404, "输入文件不存在")
            connection.execute(
                "INSERT INTO jobs(job_id,owner,language,code_path,input_file_ids,timeout_seconds,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    request.owner,
                    request.language,
                    str(code_path),
                    json.dumps(request.input_file_ids),
                    request.timeout_seconds,
                    "queued",
                    time.time(),
                ),
            )
        return job_id

    def job(self, job_id: str, owner: str) -> sqlite3.Row | None:
        """按任务和 owner 联合查询，防止仅凭 ID 越权。"""
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM jobs WHERE job_id=? AND owner=?", (job_id, owner)
            ).fetchone()

    def files(self, file_ids: list[str]) -> list[sqlite3.Row]:
        """按请求顺序读取文件记录，缺失时直接拒绝。"""
        if not file_ids:
            return []
        with self.connect() as connection:
            found = {
                str(row["file_id"]): row
                for row in connection.execute(
                    f"SELECT * FROM files WHERE file_id IN ({','.join('?' for _ in file_ids)})",
                    file_ids,
                ).fetchall()
            }
        if set(found) != set(file_ids):
            raise HTTPException(404, "输入文件不存在")
        return [found[file_id] for file_id in file_ids]

    def artifacts(self, job_id: str, owner: str) -> list[sqlite3.Row]:
        """按任务与 owner 查询已登记产物。"""
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM artifacts WHERE job_id=? AND owner=? ORDER BY filename",
                (job_id, owner),
            ).fetchall()

    def storage_usage(self, owner: str | None = None) -> tuple[int, int]:
        """返回已登记上传和产物的全局用量及指定 owner 用量。"""
        with self.connect() as connection:
            global_files = connection.execute("SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]
            global_artifacts = connection.execute(
                "SELECT COALESCE(SUM(size),0) FROM artifacts"
            ).fetchone()[0]
            owner_usage = 0
            if owner is not None:
                owner_files = connection.execute(
                    "SELECT COALESCE(SUM(size),0) FROM files WHERE owner=?", (owner,)
                ).fetchone()[0]
                owner_artifacts = connection.execute(
                    "SELECT COALESCE(SUM(size),0) FROM artifacts WHERE owner=?", (owner,)
                ).fetchone()[0]
                owner_usage = int(owner_files) + int(owner_artifacts)
        return int(global_files) + int(global_artifacts), owner_usage

    def cleanup_expired(self, retention_seconds: int) -> dict[str, int]:
        """清理过期终态任务及未被活动任务引用的过期上传。"""
        cutoff = time.time() - max(60, retention_seconds)
        with self.lock, self.connect() as connection:
            jobs = connection.execute(
                "SELECT job_id,code_path FROM jobs WHERE state IN ('succeeded','failed','timed_out') AND finished_at IS NOT NULL AND finished_at<?",
                (cutoff,),
            ).fetchall()
            active_rows = connection.execute(
                "SELECT input_file_ids FROM jobs WHERE state IN ('queued','running')"
            ).fetchall()
            active_file_ids: set[str] = set()
            for row in active_rows:
                active_file_ids.update(json.loads(str(row["input_file_ids"])))
            expired_files = connection.execute(
                "SELECT file_id,path FROM files WHERE created_at<?", (cutoff,)
            ).fetchall()
            removable_files = [row for row in expired_files if row["file_id"] not in active_file_ids]
            if jobs:
                connection.executemany(
                    "DELETE FROM jobs WHERE job_id=?", [(row["job_id"],) for row in jobs]
                )
            if removable_files:
                connection.executemany(
                    "DELETE FROM files WHERE file_id=?",
                    [(row["file_id"],) for row in removable_files],
                )
        for row in jobs:
            shutil.rmtree(self.root / "jobs" / str(row["job_id"]), ignore_errors=True)
            Path(str(row["code_path"])).unlink(missing_ok=True)
        for row in removable_files:
            path = Path(str(row["path"]))
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
            parent = path.parent
            try:
                parent.rmdir()
            except OSError:
                pass
        return {"jobs": len(jobs), "files": len(removable_files)}

    def cleanup_job_staging(self, job_id: str, code_path_value: str) -> None:
        """删除终态任务的受控临时副本，不触及原上传和已登记产物。"""
        try:
            uuid.UUID(job_id)
        except ValueError:
            return
        jobs_root = (self.root / "jobs").resolve()
        job_root = self.root / "jobs" / job_id
        if job_root.is_symlink() or job_root.resolve().parent != jobs_root:
            return
        for name in ("source", "input"):
            target = job_root / name
            if target.is_symlink():
                continue
            if target.exists() and target.resolve().parent == job_root.resolve():
                shutil.rmtree(target, ignore_errors=True)
        code_path = Path(code_path_value)
        submitted_root = (self.root / "submitted").resolve()
        if (
            not code_path.is_symlink()
            and code_path.name == job_id
            and code_path.resolve().parent == submitted_root
        ):
            code_path.unlink(missing_ok=True)


class Runner:
    """单线程调度固定配置的短生命周期文档 worker。"""

    def __init__(self, settings: Settings, store: Store, docker_factory: Callable[[], Any]):
        self.settings = settings
        self.store = store
        self.docker_factory = docker_factory
        self.pending: queue.Queue[str | None] = queue.Queue(maxsize=MAX_QUEUE_DEPTH + 1)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active_job_id: str | None = None

    def start(self) -> None:
        """清理遗留容器并恢复排队任务。"""
        self._cleanup_orphan_containers()
        queued = self.store.recover()
        self.store.cleanup_expired(self.settings.retention_seconds)
        self.thread = threading.Thread(target=self._loop, name="office-worker-queue", daemon=True)
        self.thread.start()
        for job_id in queued:
            self.pending.put_nowait(job_id)

    def close(self) -> None:
        """通知调度线程停止，不强制杀死当前请求线程。"""
        self.stop_event.set()
        try:
            self.pending.put_nowait(None)
        except queue.Full:
            pass
        if self.thread:
            self.thread.join(timeout=5)

    def submit(self, job_id: str) -> None:
        """把已持久化任务加入有限内存队列。"""
        try:
            self.pending.put_nowait(job_id)
        except queue.Full as exc:
            raise HTTPException(429, "任务队列已满") from exc

    def _loop(self) -> None:
        """逐个执行任务，保证全局最多一个运行容器。"""
        while not self.stop_event.is_set():
            job_id = self.pending.get()
            if job_id is None:
                return
            self.active_job_id = job_id
            try:
                self._run_job(job_id)
            finally:
                self.active_job_id = None

    def _cleanup_orphan_containers(self) -> None:
        """删除本服务上次退出后遗留的 worker，避免孤儿继续执行。"""
        client = self.docker_factory()
        try:
            for container in client.containers.list(
                all=True,
                filters={
                    "label": [
                        "minimax_office.managed=true",
                        f"minimax_office.instance={self.settings.instance_id}",
                    ]
                },
            ):
                try:
                    container.remove(force=True)
                except Exception:
                    continue
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _run_job(self, job_id: str) -> None:
        """使用固定命令和固定安全参数启动单个 worker。"""
        with self.store.connect() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job or job["state"] != "queued":
                return
            connection.execute(
                "UPDATE jobs SET state='running', started_at=? WHERE job_id=?",
                (time.time(), job_id),
            )
        job_root = self.settings.data_root / "jobs" / job_id
        host_job_root = self.settings.host_data_root / "jobs" / job_id
        source_dir, input_dir = job_root / "source", job_root / "input"
        output_dir = job_root / "output"
        for path in (source_dir, input_dir):
            path.mkdir(parents=True, exist_ok=True)
        language = str(job["language"])
        source = source_dir / SOURCE_NAMES[language]
        shutil.copyfile(str(job["code_path"]), source)
        source.chmod(0o444)
        file_ids = json.loads(str(job["input_file_ids"]))
        for row in self.store.files(file_ids):
            target = input_dir / str(row["input_name"])
            shutil.copyfile(str(row["path"]), target)
            target.chmod(0o444)
        client, container = None, None
        state, error, stdout, stderr = "failed", None, "", ""
        stage = "启动隔离 worker"
        try:
            client = self.docker_factory()
            container = client.containers.run(
                self.settings.worker_image,
                command=["/opt/office-worker/run-held-task", language],
                name=f"minimax-office-{self.settings.instance_id}-{job_id}",
                detach=True,
                network_mode="none",
                init=True,
                read_only=True,
                user=self.settings.worker_user,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                nano_cpus=2_000_000_000,
                mem_limit="2g",
                memswap_limit="2g",
                pids_limit=256,
                environment={"LANG": "C.UTF-8", "HOME": "/tmp", "TMPDIR": "/tmp"},
                volumes={
                    str(host_job_root / "source"): {"bind": "/work/source", "mode": "ro"},
                    str(host_job_root / "input"): {"bind": "/work/input", "mode": "ro"},
                },
                tmpfs={
                    "/tmp": "rw,nosuid,size=512m,mode=1777",
                    "/work/output": "rw,nosuid,nodev,noexec,size=64m,nr_inodes=1024,uid=10001,gid=10001,mode=0770",
                },
                log_config=self._log_config(),
                labels={
                    "minimax_office.managed": "true",
                    "minimax_office.instance": self.settings.instance_id,
                    "minimax_office.job_id": job_id,
                },
            )
            stage = "等待 worker 完成标志"
            try:
                marker = self._wait_for_result_marker(
                    container, timeout_seconds=int(job["timeout_seconds"])
                )
                stdout, stderr = self._collect_logs_safely(container)
                exit_code = int(marker.get("exit_code", 1))
                if exit_code == 0:
                    stage = "收集 worker 产物"
                    self._capture_artifacts(container, job_id, str(job["owner"]), output_dir)
                    state = "succeeded"
                else:
                    error = f"执行文档脚本失败：worker 退出码 {exit_code}"
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    state, error = "timed_out", f"{stage}超时"
                    try:
                        container.kill()
                    except Exception:
                        pass
                    stdout, stderr = self._collect_logs_safely(container)
                else:
                    stdout, stderr = self._collect_logs_safely(container)
                    if isinstance(exc, ValueError):
                        error = f"{stage}失败：{str(exc)}"
                    else:
                        error = f"{stage}失败：{type(exc).__name__}"
        except Exception as exc:
            error = f"{stage}失败：{type(exc).__name__}"
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE jobs SET state=?, stdout=?, stderr=?, error=?, finished_at=? WHERE job_id=?",
                (state, stdout[-MAX_LOG_CHARS:], stderr[-MAX_LOG_CHARS:], error, time.time(), job_id),
            )
        self.store.cleanup_job_staging(job_id, str(job["code_path"]))
        if state != "succeeded":
            shutil.rmtree(output_dir, ignore_errors=True)
        self.store.cleanup_expired(self.settings.retention_seconds)

    @staticmethod
    def _container_logs(container: Any, *, stdout: bool, stderr: bool) -> str:
        """读取并限制 worker 日志，避免持久状态无限增长。"""
        value = container.logs(stdout=stdout, stderr=stderr, tail=1000)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return str(value)[-MAX_LOG_CHARS:]

    def _collect_logs_safely(self, container: Any) -> tuple[str, str]:
        """在删除容器前尽力读取受限日志，不覆盖原始失败原因。"""
        try:
            stdout = self._container_logs(container, stdout=True, stderr=False)
        except Exception as exc:
            stdout = f"[无法读取 stdout: {type(exc).__name__}]"
        try:
            stderr = self._container_logs(container, stdout=False, stderr=True)
        except Exception as exc:
            stderr = f"[无法读取 stderr: {type(exc).__name__}]"
        return stdout[-MAX_LOG_CHARS:], stderr[-MAX_LOG_CHARS:]

    @staticmethod
    def _log_config() -> Any:
        """限制每个 worker 的 Docker json-file 日志占用。"""
        config = {"max-size": "1m", "max-file": "1"}
        if docker is not None:
            return docker.types.LogConfig(type="json-file", config=config)
        return {"type": "json-file", "config": config}

    def _wait_for_result_marker(self, container: Any, timeout_seconds: int) -> dict[str, Any]:
        """轮询 worker 完成标志；worker 保持存活以便复制 tmpfs 产物。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            marker = self._read_marker(container)
            if marker is not None:
                if not isinstance(marker.get("exit_code"), int):
                    raise ValueError("worker 结果标志无效")
                return marker
            reload_method = getattr(container, "reload", None)
            if callable(reload_method):
                reload_method()
            if getattr(container, "status", "running") not in {
                "created",
                "running",
                "restarting",
            }:
                raise RuntimeError("worker 在写入结果标志前退出")
            time.sleep(0.25)
        raise TimeoutError(timeout_seconds)

    def _read_marker(self, container: Any) -> dict[str, Any] | None:
        """用固定 head 命令读取 tmpfs JSON 标志；不存在时继续轮询。"""
        result = container.exec_run(
            ["/usr/bin/head", "-c", "16385", "/tmp/office-worker-result.json"],
            stdout=True,
            stderr=False,
            user=self.settings.worker_user,
            privileged=False,
        )
        exit_code = int(result.exit_code if hasattr(result, "exit_code") else result[0])
        if exit_code != 0:
            return None
        output = result.output if hasattr(result, "output") else result[1]
        if not isinstance(output, bytes) or len(output) > 16_384:
            raise ValueError("worker 结果标志无效")
        marker = json.loads(output.decode("utf-8"))
        if not isinstance(marker, dict):
            raise ValueError("worker 结果标志无效")
        return marker

    def _capture_artifacts(
        self, container: Any, job_id: str, owner: str, output_dir: Path
    ) -> None:
        """从仍存活的 worker 流式获取 tmpfs 产物，校验后写入私有宿主目录。"""
        archive_path = self.settings.data_root / "jobs" / job_id / "output.tar"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._stream_output_archive(container, archive_path)
            self._validate_and_copy_archive(archive_path, job_id, owner, output_dir)
        finally:
            archive_path.unlink(missing_ok=True)

    def _stream_output_archive(self, container: Any, archive_path: Path) -> None:
        """用固定 tar 命令流式读取 tmpfs 输出，并检查 exec 退出码。"""
        api = container.client.api
        created = api.exec_create(
            container.id,
            ["/usr/bin/tar", "-C", "/work/output", "-cf", "-", "."],
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            privileged=False,
            user=self.settings.worker_user,
        )
        exec_id = created["Id"]
        stream = api.exec_start(exec_id, stream=True, demux=True)
        archive_size, error_bytes = 0, bytearray()
        with archive_path.open("xb") as archive_file:
            for stdout_chunk, stderr_chunk in stream:
                if stdout_chunk:
                    archive_size += len(stdout_chunk)
                    if archive_size > MAX_ARCHIVE_BYTES:
                        raise ValueError("产物 archive 超过限制")
                    archive_file.write(stdout_chunk)
                if stderr_chunk and len(error_bytes) < MAX_LOG_CHARS:
                    error_bytes.extend(stderr_chunk[: MAX_LOG_CHARS - len(error_bytes)])
        inspected = api.exec_inspect(exec_id)
        if int(inspected.get("ExitCode", 1)) != 0:
            detail = error_bytes.decode("utf-8", errors="replace").strip()
            raise ValueError(f"打包产物失败：{detail[-1000:] or 'tar 退出非零'}")

    def _validate_and_copy_archive(
        self, archive_path: Path, job_id: str, owner: str, output_dir: Path
    ) -> None:
        """只接受 output 根目录下的普通白名单文件，并在配额锁内落盘登记。"""
        candidates: list[tarfile.TarInfo] = []
        total = 0
        with tarfile.open(archive_path, mode="r:*") as tar:
            for member in tar.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("产物 archive 存在路径穿越")
                parts = tuple(part for part in path.parts if part not in {"", "."})
                if member.isdir() and parts in {(), ("output",)}:
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    raise ValueError("产物 archive 只允许普通文件")
                if len(parts) == 2 and parts[0] == "output":
                    filename = parts[1]
                elif len(parts) == 1:
                    filename = parts[0]
                else:
                    raise ValueError("产物只能位于 output 根目录")
                if Path(filename).name != filename or Path(filename).suffix.lower() not in OUTPUT_EXTENSIONS:
                    raise ValueError("产物名称或格式不允许")
                total += int(member.size)
                if total > MAX_OUTPUT_BYTES:
                    raise ValueError("产物总大小超过限制")
                candidates.append(member)
            if len(candidates) > MAX_OUTPUT_FILES:
                raise ValueError("产物文件数量超过限制")
            with self.store.lock:
                global_usage, owner_usage = self.store.storage_usage(owner)
                if global_usage + total > self.settings.global_quota_bytes:
                    raise ValueError("全局文档存储配额不足")
                if owner_usage + total > self.settings.owner_quota_bytes:
                    raise ValueError("当前用户文档存储配额不足")
                output_dir.mkdir(parents=True, mode=0o700)
                records = []
                try:
                    for member in candidates:
                        filename = PurePosixPath(member.name).name
                        source = tar.extractfile(member)
                        if source is None:
                            raise ValueError("产物不可读")
                        target = output_dir / filename
                        temporary = output_dir / f".{filename}.{uuid.uuid4().hex}.part"
                        digest, copied = hashlib.sha256(), 0
                        with temporary.open("xb") as output:
                            while chunk := source.read(1024 * 1024):
                                copied += len(chunk)
                                if copied > member.size:
                                    raise ValueError("产物大小与 archive 不一致")
                                digest.update(chunk)
                                output.write(chunk)
                        if copied != member.size:
                            raise ValueError("产物大小与 archive 不一致")
                        temporary.replace(target)
                        target.chmod(0o600)
                        records.append(
                            (
                                str(uuid.uuid4()),
                                job_id,
                                owner,
                                filename,
                                copied,
                                mimetypes.guess_type(filename)[0]
                                or "application/octet-stream",
                                digest.hexdigest(),
                                str(target),
                            )
                        )
                    with self.store.connect() as connection:
                        connection.executemany(
                            "INSERT INTO artifacts(artifact_id,job_id,owner,filename,size,mime_type,sha256,path) VALUES(?,?,?,?,?,?,?,?)",
                            records,
                        )
                except Exception:
                    shutil.rmtree(output_dir, ignore_errors=True)
                    raise


def _safe_filename(filename: str) -> str:
    """将上传文件名压缩为单个安全路径组件。"""
    name = Path(filename or "file").name.strip().replace("\x00", "")
    name = SAFE_NAME_RE.sub("_", name).strip(" ._")
    if not name or len(name) > 180:
        raise HTTPException(400, "文件名无效或过长")
    if Path(name).suffix.lower() not in INPUT_EXTENSIONS:
        raise HTTPException(400, "不支持的输入文件格式")
    return name


def _validate_owner(owner: str) -> str:
    """校验查询或表单中的 owner。"""
    if not OWNER_RE.fullmatch(owner):
        raise HTTPException(422, "owner 格式无效")
    return owner


def _sha256(path: Path) -> str:
    """分块计算文件摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_docker_factory():
    """连接宿主 Docker；socket 只挂载给控制器。"""
    if docker is None:
        raise RuntimeError("docker SDK 未安装")
    return docker.from_env()


def create_app(
    settings: Settings | None = None,
    docker_factory: Callable[[], Any] | None = None,
    *,
    start_runner: bool = True,
) -> FastAPI:
    """创建可注入 Docker 客户端的 FastAPI 应用。"""
    configured = settings or Settings.from_env()
    store = Store(configured.data_root)
    runner = Runner(configured, store, docker_factory or _default_docker_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_runner:
            runner.start()
        yield
        if start_runner:
            runner.close()

    api = FastAPI(title="MiniMax Office Runtime", lifespan=lifespan)
    api.state.settings, api.state.store, api.state.runner = configured, store, runner

    def authenticate(authorization: Annotated[str | None, Header()] = None) -> None:
        """使用恒定时间比较共享 Bearer token。"""
        expected = f"Bearer {configured.token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "认证失败", headers={"WWW-Authenticate": "Bearer"})

    @api.get("/health")
    def health() -> dict[str, Any]:
        """返回非敏感运行状态。"""
        with store.connect() as connection:
            queue_depth = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state='queued'"
            ).fetchone()[0]
            active_jobs = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state='running'"
            ).fetchone()[0]
        return {
            "ok": True,
            "worker_image": configured.worker_image,
            "queue_depth": queue_depth,
            "active_jobs": active_jobs,
        }

    @api.post("/v1/files", dependencies=[Depends(authenticate)])
    async def upload_file(
        owner: Annotated[str, Form()], file: Annotated[UploadFile, File()]
    ) -> dict[str, Any]:
        """流式保存单个输入文件，不解析压缩包或 Office 内容。"""
        owner = _validate_owner(owner)
        filename = _safe_filename(file.filename or "")
        file_id = str(uuid.uuid4())
        input_name = f"{file_id[:8]}-{filename}"
        target_dir = configured.data_root / "files" / owner / file_id
        target_dir.mkdir(parents=True, mode=0o700)
        target = target_dir / filename
        size, digest = 0, hashlib.sha256()
        try:
            with target.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(413, "单文件超过 25 MiB")
                    digest.update(chunk)
                    output.write(chunk)
            target.chmod(0o600)
            # 与产物登记共用进程锁，在最终落库时重新计算，避免并发超配。
            with store.lock:
                global_usage, owner_usage = store.storage_usage(owner)
                if global_usage + size > configured.global_quota_bytes:
                    raise HTTPException(413, "全局文档存储配额不足")
                if owner_usage + size > configured.owner_quota_bytes:
                    raise HTTPException(413, "当前用户文档存储配额不足")
                with store.connect() as connection:
                    connection.execute(
                        "INSERT INTO files(file_id,owner,filename,input_name,size,sha256,path,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (file_id, owner, filename, input_name, size, digest.hexdigest(), str(target), time.time()),
                    )
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
        finally:
            await file.close()
        return {
            "file_id": file_id,
            "filename": filename,
            "input_name": input_name,
            "size": size,
            "sha256": digest.hexdigest(),
        }

    @api.post("/v1/jobs", dependencies=[Depends(authenticate)])
    def create_job(request: JobRequest) -> dict[str, str]:
        """创建任务并将代码写入服务端固定 source 文件。"""
        files = store.files(request.input_file_ids)
        if sum(int(row["size"]) for row in files) > MAX_INPUT_BYTES:
            raise HTTPException(413, "任务输入文件总量超过 50 MiB")
        if any(str(row["owner"]) != request.owner for row in files):
            raise HTTPException(404, "输入文件不存在")
        source_root = configured.data_root / "submitted"
        source_root.mkdir(parents=True, exist_ok=True)
        temporary_id = str(uuid.uuid4())
        code_path = source_root / temporary_id
        code_path.write_text(request.code, encoding="utf-8")
        code_path.chmod(0o600)
        try:
            job_id = store.create_job(request, code_path, files)
            final_path = source_root / job_id
            code_path.replace(final_path)
            with store.connect() as connection:
                connection.execute(
                    "UPDATE jobs SET code_path=? WHERE job_id=?", (str(final_path), job_id)
                )
            runner.submit(job_id)
        except Exception:
            code_path.unlink(missing_ok=True)
            raise
        return {"job_id": job_id, "state": "queued"}

    @api.get("/v1/jobs/{job_id}", dependencies=[Depends(authenticate)])
    def get_job(job_id: str, owner: Annotated[str, Query()]) -> dict[str, Any]:
        """返回属于指定 owner 的任务和已登记产物。"""
        owner = _validate_owner(owner)
        row = store.job(job_id, owner)
        if not row:
            raise HTTPException(404, "任务不存在")
        artifacts = [
            {key: artifact[key] for key in ("artifact_id", "filename", "size", "mime_type", "sha256")}
            for artifact in store.artifacts(job_id, owner)
        ]
        result = {
            "job_id": job_id,
            "state": row["state"],
            "stdout": row["stdout"],
            "stderr": row["stderr"],
            "artifacts": artifacts,
        }
        if row["error"]:
            result["error"] = row["error"]
        return result

    @api.get(
        "/v1/jobs/{job_id}/artifacts/{artifact_id}", dependencies=[Depends(authenticate)]
    )
    def download_artifact(
        job_id: str, artifact_id: str, owner: Annotated[str, Query()]
    ) -> FileResponse:
        """下载已登记且归属一致的普通文件。"""
        owner = _validate_owner(owner)
        if not store.job(job_id, owner):
            raise HTTPException(404, "任务不存在")
        with store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? AND job_id=? AND owner=?",
                (artifact_id, job_id, owner),
            ).fetchone()
        if not row:
            raise HTTPException(404, "产物不存在")
        path = Path(str(row["path"]))
        expected_parent = configured.data_root / "jobs" / job_id / "output"
        if (
            path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != expected_parent.resolve()
            or path.name != row["filename"]
        ):
            raise HTTPException(410, "产物文件已失效")
        return FileResponse(path, media_type=row["mime_type"], filename=row["filename"])

    return api


app = create_app()
