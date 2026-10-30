"""MiniMax Office 控制器的权限、队列与 Docker 隔离回归测试。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("OFFICE_RUNTIME_TOKEN", "t" * 40)
os.environ.setdefault("OFFICE_WORKER_IMAGE", "office-worker:test")
os.environ.setdefault("OFFICE_DATA_ROOT", "/tmp/minimax-office-import-data")
os.environ.setdefault("OFFICE_HOST_DATA_ROOT", "/tmp/minimax-office-import-data")
os.environ.setdefault("OFFICE_HOST_SKILLS_ROOT", "/tmp/minimax-office-import-skills")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from service.app import JobRequest, Runner, Settings, Store, create_app


TOKEN = "Bearer " + "t" * 40
OWNER_A = hashlib.sha256(b"session-a:user-a").hexdigest()
OWNER_B = hashlib.sha256(b"session-b:user-b").hexdigest()


class FakeContainer:
    """记录容器调用并模拟成功、超时和日志。"""

    def __init__(self, kwargs: dict[str, Any], *, timeout: bool = False):
        self.kwargs = kwargs
        self.timeout = timeout
        self.status = "running"
        self.killed = False
        self.removed = False

        self.id = "fake-container"
        self.client = SimpleNamespace(api=FakeExecApi(self))

    @staticmethod
    def _archive(name: str, content: bytes) -> list[bytes]:
        """构造 Docker exec tar 风格的字节流。"""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        return [buffer.getvalue()]

    def exec_run(self, cmd: list[str], **kwargs):
        """固定 head 命令返回结果标志；超时场景立即触发测试超时。"""
        assert cmd == ["/usr/bin/head", "-c", "16385", "/tmp/office-worker-result.json"]
        if self.timeout:
            raise TimeoutError(cmd)
        return SimpleNamespace(exit_code=0, output=b'{"exit_code":0}')

    def logs(self, *, stdout: bool, stderr: bool, tail: int):
        """返回可区分的标准输出和错误输出。"""
        return b"done" if stdout else b""

    def reload(self):
        """保留当前模拟状态。"""

    def kill(self):
        """记录超时清理。"""
        self.killed = True
        self.status = "exited"

    def remove(self, force: bool):
        """记录容器删除。"""
        self.removed = force


class FakeExecApi:
    """模拟低层 Docker exec 流式 tar 接口。"""

    def __init__(self, container: FakeContainer):
        self.container = container
        self.command: list[str] | None = None

    def exec_create(self, container_id: str, cmd: list[str], **kwargs):
        """记录受控 tar 命令。"""
        assert container_id == self.container.id
        self.command = cmd
        return {"Id": "archive-exec"}

    def exec_start(self, exec_id: str, *, stream: bool, demux: bool):
        """流式返回包含一个 DOCX 的 tar stdout。"""
        assert exec_id == "archive-exec" and stream and demux
        assert self.command == ["/usr/bin/tar", "-C", "/work/output", "-cf", "-", "."]
        archive = FakeContainer._archive("./result.docx", b"docx")[0]
        return iter([(archive, None)])

    def exec_inspect(self, exec_id: str):
        """报告 tar 命令成功。"""
        assert exec_id == "archive-exec"
        return {"ExitCode": 0}


class FakeContainers:
    """模拟 Docker containers 集合。"""

    def __init__(self, timeout: bool = False):
        self.timeout = timeout
        self.created: list[FakeContainer] = []
        self.list_filters: list[dict[str, Any]] = []

    def list(self, all: bool, filters: dict[str, str]):
        """启动恢复测试中默认没有孤儿容器。"""
        self.list_filters.append(filters)
        return []

    def run(self, image: str, **kwargs):
        """捕获固定镜像和全部隔离参数。"""
        kwargs["image"] = image
        container = FakeContainer(kwargs, timeout=self.timeout)
        self.created.append(container)
        return container


class FakeDocker:
    """提供最小 Docker 客户端接口。"""

    def __init__(self, timeout: bool = False):
        self.containers = FakeContainers(timeout=timeout)
        self.closed = False

    def close(self):
        """记录客户端关闭。"""
        self.closed = True


def settings(tmp_path: Path) -> Settings:
    """构造控制器路径与宿主路径一致的测试配置。"""
    return Settings(
        token="t" * 40,
        data_root=tmp_path / "data",
        host_data_root=tmp_path / "data",
        worker_image="office-worker:test",
    )


def auth() -> dict[str, str]:
    """返回有效测试认证头。"""
    return {"Authorization": TOKEN}


def test_health_and_authentication(tmp_path: Path):
    """健康接口匿名可用，业务接口必须使用精确 Bearer token。"""
    app = create_app(settings(tmp_path), lambda: FakeDocker(), start_runner=False)
    with TestClient(app) as client:
        assert client.get("/health").json() == {
            "ok": True,
            "worker_image": "office-worker:test",
            "queue_depth": 0,
            "active_jobs": 0,
        }
        assert client.post("/v1/files", data={"owner": OWNER_A}).status_code == 401
        assert client.post(
            "/v1/files",
            data={"owner": OWNER_A},
            headers={"Authorization": TOKEN + "x"},
        ).status_code == 401


def test_upload_and_owner_isolation(tmp_path: Path):
    """上传返回安全 input_name，其他 owner 不能把文件加入任务。"""
    app = create_app(settings(tmp_path), lambda: FakeDocker(), start_runner=False)
    with TestClient(app) as client:
        uploaded = client.post(
            "/v1/files",
            headers=auth(),
            data={"owner": OWNER_A},
            files={"file": ("../../预算 表.xlsx", b"xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert uploaded.status_code == 200
        item = uploaded.json()
        assert item["filename"] == "预算_表.xlsx"
        assert item["input_name"].endswith("-预算_表.xlsx")
        assert "/" not in item["input_name"]
        denied = client.post(
            "/v1/jobs",
            headers=auth(),
            json={
                "owner": OWNER_B,
                "language": "python",
                "code": "print('x')",
                "input_file_ids": [item["file_id"]],
            },
        )
        assert denied.status_code == 404
        rejected = client.post(
            "/v1/files",
            headers=auth(),
            data={"owner": OWNER_A},
            files={"file": ("payload.exe", b"x", "application/octet-stream")},
        )
        assert rejected.status_code == 400


def test_upload_quota_rejects_without_persisting(tmp_path: Path):
    """上传超过 owner 剩余配额时确定性拒绝并移除临时文件。"""
    configured = replace(settings(tmp_path), owner_quota_bytes=3, global_quota_bytes=100)
    app = create_app(configured, lambda: FakeDocker(), start_runner=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/files",
            headers=auth(),
            data={"owner": OWNER_A},
            files={"file": ("input.txt", b"four", "text/plain")},
        )
        assert response.status_code == 413
    with Store(configured.data_root).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert not any((configured.data_root / "files").rglob("input.txt"))


def test_job_request_forbids_docker_parameters(tmp_path: Path):
    """API 拒绝用户传入镜像、命令或其他额外 Docker 参数。"""
    app = create_app(settings(tmp_path), lambda: FakeDocker(), start_runner=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/jobs",
            headers=auth(),
            json={
                "owner": OWNER_A,
                "language": "python",
                "code": "print('ok')",
                "image": "evil:latest",
                "command": ["sh"],
            },
        )
        assert response.status_code == 422


def test_per_owner_and_global_queue_limits(tmp_path: Path):
    """每个 owner 只有一个未完成任务，全局最多四个排队任务。"""
    app = create_app(settings(tmp_path), lambda: FakeDocker(), start_runner=False)
    payload = {"language": "python", "code": "print('ok')"}
    with TestClient(app) as client:
        first = client.post("/v1/jobs", headers=auth(), json={"owner": OWNER_A, **payload})
        assert first.status_code == 200
        duplicate = client.post("/v1/jobs", headers=auth(), json={"owner": OWNER_A, **payload})
        assert duplicate.status_code == 429
        for index in range(3):
            owner = hashlib.sha256(f"owner-{index}".encode()).hexdigest()
            assert client.post(
                "/v1/jobs", headers=auth(), json={"owner": owner, **payload}
            ).status_code == 200
        overflow_owner = hashlib.sha256(b"overflow-owner").hexdigest()
        assert client.post(
            "/v1/jobs", headers=auth(), json={"owner": overflow_owner, **payload}
        ).status_code == 429


def _persist_job(
    store: Store,
    configured: Settings,
    *,
    input_file_ids: list[str] | None = None,
) -> str:
    """直接持久化一个供 Runner 单元测试使用的任务。"""
    submitted = configured.data_root / "submitted"
    submitted.mkdir(parents=True, exist_ok=True)
    temporary = submitted / f"temp-{uuid.uuid4()}"
    temporary.write_text("print('ok')")
    file_ids = input_file_ids or []
    request = JobRequest(
        owner=OWNER_A,
        language="python",
        code="print('ok')",
        input_file_ids=file_ids,
    )
    job_id = store.create_job(request, temporary, store.files(file_ids))
    final = submitted / job_id
    temporary.replace(final)
    with store.connect() as connection:
        connection.execute("UPDATE jobs SET code_path=? WHERE job_id=?", (str(final), job_id))
    return job_id


def test_runner_uses_fixed_isolation_and_registers_artifact(tmp_path: Path):
    """worker 使用固定资源、无网络和最小挂载，成功产物才被登记。"""
    configured, fake = settings(tmp_path), FakeDocker()
    store = Store(configured.data_root)
    job_id = _persist_job(store, configured)
    runner = Runner(configured, store, lambda: fake)
    runner._run_job(job_id)
    row = store.job(job_id, OWNER_A)
    assert row and row["state"] == "succeeded" and row["stdout"] == "done"
    artifacts = store.artifacts(job_id, OWNER_A)
    assert len(artifacts) == 1 and artifacts[0]["filename"] == "result.docx"
    container = fake.containers.created[0]
    args = container.kwargs
    assert args["image"] == "office-worker:test"
    assert args["command"] == ["/opt/office-worker/run-held-task", "python"]
    assert args["network_mode"] == "none" and args["init"] is True
    assert args["read_only"] is True and args["user"] == "10001:10001"
    assert args["cap_drop"] == ["ALL"]
    assert args["security_opt"] == ["no-new-privileges:true"]
    assert args["nano_cpus"] == 2_000_000_000
    assert args["mem_limit"] == "2g" and args["memswap_limit"] == "2g"
    assert args["pids_limit"] == 256
    assert args["labels"]["minimax_office.instance"] == configured.instance_id
    assert set(spec["bind"] for spec in args["volumes"].values()) == {
        "/work/source",
        "/work/input",
    }
    assert args["tmpfs"]["/work/output"].startswith("rw,nosuid,nodev,noexec,size=64m")
    assert "nr_inodes=1024" in args["tmpfs"]["/work/output"]
    assert args["log_config"]
    assert container.removed and fake.closed


def test_orphan_cleanup_is_scoped_to_controller_instance(tmp_path: Path):
    """不同 host_data_root 的控制器不会清理彼此 worker。"""

    class Orphan:
        """带实例标签的遗留容器。"""

        def __init__(self, instance_id: str):
            self.instance_id = instance_id
            self.removed = False

        def remove(self, force: bool):
            """记录是否被清理。"""
            self.removed = force

    class ScopedContainers(FakeContainers):
        """按 Docker label AND 语义返回遗留容器。"""

        def __init__(self, orphans: list[Orphan]):
            super().__init__()
            self.orphans = orphans

        def list(self, all: bool, filters: dict[str, Any]):
            """只返回同时满足 managed 与 instance 标签的容器。"""
            self.list_filters.append(filters)
            labels = filters["label"]
            instance = next(value.split("=", 1)[1] for value in labels if value.startswith("minimax_office.instance="))
            return [orphan for orphan in self.orphans if orphan.instance_id == instance]

    configured_a = settings(tmp_path / "a")
    configured_b = settings(tmp_path / "b")
    own, foreign = Orphan(configured_a.instance_id), Orphan(configured_b.instance_id)
    client = FakeDocker()
    client.containers = ScopedContainers([own, foreign])
    Runner(configured_a, Store(configured_a.data_root), lambda: client)._cleanup_orphan_containers()
    assert own.removed is True
    assert foreign.removed is False
    assert client.containers.list_filters == [
        {
            "label": [
                "minimax_office.managed=true",
                f"minimax_office.instance={configured_a.instance_id}",
            ]
        }
    ]


def test_runner_timeout_kills_and_removes_worker(tmp_path: Path):
    """执行超时必须结束并删除 worker，状态持久化为 timed_out。"""
    configured, fake = settings(tmp_path), FakeDocker(timeout=True)
    store = Store(configured.data_root)
    job_id = _persist_job(store, configured)
    Runner(configured, store, lambda: fake)._run_job(job_id)
    row = store.job(job_id, OWNER_A)
    container = fake.containers.created[0]
    assert row and row["state"] == "timed_out"
    assert row["stdout"] == "done" and row["stderr"] == ""
    assert container.killed and container.removed


def test_terminal_job_removes_copies_but_preserves_upload_and_artifact(tmp_path: Path):
    """任务结束清理临时输入与代码，原上传和成功产物继续可用。"""
    configured, fake = settings(tmp_path), FakeDocker()
    store = Store(configured.data_root)
    file_id = str(uuid.uuid4())
    upload = configured.data_root / "files" / OWNER_A / file_id / "source.xlsx"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"original")
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?,?,?)",
            (file_id, OWNER_A, "source.xlsx", "source.xlsx", 8, "sha", str(upload), time.time()),
        )
    job_id = _persist_job(store, configured, input_file_ids=[file_id])
    code_path = configured.data_root / "submitted" / job_id
    Runner(configured, store, lambda: fake)._run_job(job_id)
    job_root = configured.data_root / "jobs" / job_id
    assert upload.read_bytes() == b"original"
    assert not (job_root / "source").exists()
    assert not (job_root / "input").exists()
    assert not code_path.exists()
    artifacts = store.artifacts(job_id, OWNER_A)
    assert len(artifacts) == 1
    assert Path(artifacts[0]["path"]).read_bytes() == b"docx"


def test_artifact_quota_marks_job_failed_and_cleans_output(tmp_path: Path):
    """worker 产物超过 owner 配额时不登记，并删除未受控输出。"""
    configured = replace(settings(tmp_path), owner_quota_bytes=3, global_quota_bytes=100)
    fake, store = FakeDocker(), Store(configured.data_root)
    job_id = _persist_job(store, configured)
    Runner(configured, store, lambda: fake)._run_job(job_id)
    row = store.job(job_id, OWNER_A)
    assert row and row["state"] == "failed"
    assert "存储配额不足" in row["error"] and "超时" not in row["error"]
    assert row["stdout"] == "done" and row["stderr"] == ""
    assert store.artifacts(job_id, OWNER_A) == []
    assert not (configured.data_root / "jobs" / job_id / "output").exists()


def test_output_archive_rejects_nested_and_symlink_members(tmp_path: Path):
    """产物 archive 禁止嵌套路径与符号链接，且不会写入宿主输出。"""
    configured = settings(tmp_path)
    store = Store(configured.data_root)
    runner = Runner(configured, store, lambda: FakeDocker())
    archive = configured.data_root / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        nested = tarfile.TarInfo("output/nested/result.docx")
        nested.size = 1
        tar.addfile(nested, io.BytesIO(b"x"))
    output = configured.data_root / "jobs" / "job" / "output"
    try:
        runner._validate_and_copy_archive(archive, "job", OWNER_A, output)
        raise AssertionError("嵌套路径应被拒绝")
    except ValueError as exc:
        assert "根目录" in str(exc)
    with tarfile.open(archive, "w") as tar:
        link = tarfile.TarInfo("output/result.docx")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    try:
        runner._validate_and_copy_archive(archive, "job", OWNER_A, output)
        raise AssertionError("符号链接应被拒绝")
    except ValueError as exc:
        assert "普通文件" in str(exc)
    assert not output.exists()


def test_restart_marks_running_failed_and_preserves_queued(tmp_path: Path):
    """控制器重启不重复执行 running，仍返回原 queued 任务。"""
    configured = settings(tmp_path)
    store = Store(configured.data_root)
    running = _persist_job(store, configured)
    with store.connect() as connection:
        connection.execute("UPDATE jobs SET state='running' WHERE job_id=?", (running,))
        connection.execute(
            "INSERT INTO jobs(job_id,owner,language,code_path,input_file_ids,timeout_seconds,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (str(__import__('uuid').uuid4()), OWNER_B, "python", "/code", "[]", 30, "queued", 2.0),
        )
    queued = store.recover()
    assert len(queued) == 1
    assert store.job(running, OWNER_A)["state"] == "failed"


def test_cleanup_only_removes_expired_terminal_and_unreferenced_upload(tmp_path: Path):
    """过期清理保留活动任务输入，只删除过期终态任务与无活动引用上传。"""
    configured = settings(tmp_path)
    store = Store(configured.data_root)
    old = time.time() - 3600
    active_file, unused_file = str(uuid.uuid4()), str(uuid.uuid4())
    active_path = configured.data_root / "files" / "active.txt"
    unused_path = configured.data_root / "files" / "unused.txt"
    active_path.parent.mkdir(parents=True, exist_ok=True)
    active_path.write_text("active")
    unused_path.write_text("unused")
    terminal_job, active_job = str(uuid.uuid4()), str(uuid.uuid4())
    code_path = configured.data_root / "submitted" / terminal_job
    code_path.parent.mkdir(parents=True, exist_ok=True)
    code_path.write_text("code")
    (configured.data_root / "jobs" / terminal_job).mkdir(parents=True)
    with store.connect() as connection:
        connection.executemany(
            "INSERT INTO files VALUES(?,?,?,?,?,?,?,?)",
            [
                (active_file, OWNER_A, "active.txt", "active.txt", 6, "a", str(active_path), old),
                (unused_file, OWNER_A, "unused.txt", "unused.txt", 6, "b", str(unused_path), old),
            ],
        )
        connection.executemany(
            "INSERT INTO jobs(job_id,owner,language,code_path,input_file_ids,timeout_seconds,state,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (terminal_job, OWNER_B, "python", str(code_path), "[]", 30, "failed", old, old),
                (active_job, OWNER_A, "python", "/code", json.dumps([active_file]), 30, "queued", old, None),
            ],
        )
    result = store.cleanup_expired(60)
    assert result == {"jobs": 1, "files": 1}
    assert active_path.exists() and not unused_path.exists()
    assert store.job(active_job, OWNER_A)["state"] == "queued"
    assert not code_path.exists()


def test_artifact_download_rejects_cross_owner_and_symlink(tmp_path: Path):
    """下载同时校验任务 owner、产物 owner 和真实普通文件路径。"""
    configured = settings(tmp_path)
    store = Store(configured.data_root)
    job_id = _persist_job(store, configured)
    output = configured.data_root / "jobs" / job_id / "output"
    output.mkdir(parents=True)
    real = output / "result.pdf"
    real.write_bytes(b"pdf")
    artifact_id = "artifact"
    with store.connect() as connection:
        connection.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (job_id,))
        connection.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
            (artifact_id, job_id, OWNER_A, real.name, 3, "application/pdf", "sha", str(real)),
        )
    app = create_app(configured, lambda: FakeDocker(), start_runner=False)
    with TestClient(app) as client:
        assert client.get(
            f"/v1/jobs/{job_id}/artifacts/{artifact_id}", headers=auth(), params={"owner": OWNER_B}
        ).status_code == 404
        success = client.get(
            f"/v1/jobs/{job_id}/artifacts/{artifact_id}", headers=auth(), params={"owner": OWNER_A}
        )
        assert success.status_code == 200 and success.content == b"pdf"
        real.unlink()
        real.symlink_to(tmp_path / "outside.pdf")
        assert client.get(
            f"/v1/jobs/{job_id}/artifacts/{artifact_id}", headers=auth(), params={"owner": OWNER_A}
        ).status_code == 410
