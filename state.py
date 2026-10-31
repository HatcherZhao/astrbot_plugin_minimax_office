"""插件持久状态，记录 owner 文件、任务和幂等交付。"""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from pathlib import Path


class StateStore:
    """用原子替换保存小型 JSON 状态，所有访问受同一把锁保护。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"owners": {}, "jobs": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"owners": {}, "jobs": {}}
        if not isinstance(value, dict):
            return {"owners": {}, "jobs": {}}
        value.setdefault("owners", {})
        value.setdefault("jobs", {})
        return value

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def add_file(self, owner: str, record: dict, keep: int = 20) -> None:
        """登记上传结果，每个 owner 只保留最近有限条记录。"""
        with self._lock:
            files = self._data["owners"].setdefault(owner, {}).setdefault("files", [])
            files[:] = [item for item in files if item.get("file_id") != record.get("file_id")]
            files.append(deepcopy(record))
            del files[:-keep]
            self._save()

    def files(self, owner: str) -> list[dict]:
        """返回当前 owner 的文件副本。"""
        with self._lock:
            return deepcopy(self._data["owners"].get(owner, {}).get("files", []))

    def prune_files(self, keep_record) -> int:
        """按调用方文件存在性与保留期过滤记录，返回删除数量。"""
        removed = 0
        with self._lock:
            for value in self._data["owners"].values():
                files = value.get("files", [])
                kept = [item for item in files if keep_record(item)]
                removed += len(files) - len(kept)
                value["files"] = kept
            if removed:
                self._save()
        return removed

    def owned_file_ids(self, owner: str) -> set[str]:
        """返回可提交给任务的服务端文件 ID。"""
        return {str(item.get("file_id")) for item in self.files(owner) if item.get("file_id")}

    def add_job(self, job_id: str, owner: str, umo: str, sender_id: str, delivery_mode: str = "legacy_auto") -> None:
        """登记任务路由，支持重启后继续查询和交付。"""
        with self._lock:
            self._data["jobs"][job_id] = {
                "owner": owner,
                "umo": umo,
                "sender_id": sender_id,
                "delivery_mode": delivery_mode,
                "publication": None,
                "delivered": [],
                "registered_artifacts": {},
                "pending_registrations": {},
                "delivery_pending": False,
            }
            self._save()

    def authorize_publication(self, job_id: str, artifact_ids: list[str]) -> bool:
        """持久化唯一最终产物选择，重启与发送重试沿用相同选择。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            if not value:
                return False
            selection = list(dict.fromkeys(artifact_ids))
            previous = value.get("publication")
            if previous is not None and set(previous) != set(selection):
                return False
            value["publication"] = selection
            self._save()
            return True

    def set_video_contract(self, job_id: str, frames: list[dict]) -> None:
        """记录来自可信截图工具的输入证据，后续轮次和重启仍须校验。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            if value is not None:
                value["video_frames"] = deepcopy(frames)
                self._save()

    def set_video_inspection(self, job_id: str, inspection: dict) -> None:
        """保存隔离验证任务，查询超时后复用而非重复创建。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            if value is not None:
                value["video_inspection"] = deepcopy(inspection)
                self._save()

    def job(self, job_id: str) -> dict | None:
        """读取任务路由副本。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            return deepcopy(value) if value else None

    def pending_jobs(self) -> list[tuple[str, dict]]:
        """返回仍可能需要交付的任务。"""
        with self._lock:
            return [
                (job_id, deepcopy(value))
                for job_id, value in self._data["jobs"].items()
                if not value.get("terminal", False) or value.get("delivery_pending", False)
            ]

    def mark_terminal(self, job_id: str, state: str) -> None:
        """记录终态，失败任务不再无限轮询。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            if not value:
                return
            value["terminal"] = True
            value["state"] = state
            value["terminal_at"] = time.time()
            value["delivery_pending"] = False
            self._save()

    def set_delivery_pending(self, job_id: str, pending: bool) -> None:
        """记录终态结果是否仍待发送，供重启后恢复。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            if not value:
                return
            value["delivery_pending"] = bool(pending)
            self._save()

    def prune_terminal_jobs(self, terminal_before: float) -> list[str]:
        """删除超过保留期且没有待交付内容的终态任务。"""
        removed = []
        with self._lock:
            for job_id, value in list(self._data["jobs"].items()):
                if not value.get("terminal") or value.get("delivery_pending"):
                    continue
                if float(value.get("terminal_at") or 0) >= terminal_before:
                    continue
                removed.append(job_id)
                del self._data["jobs"][job_id]
            if removed:
                self._save()
        return removed

    def mark_delivered(self, job_id: str, artifact_id: str) -> bool:
        """原子标记产物；已标记时返回 False，避免重复发送。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            if not value:
                return False
            delivered = value.setdefault("delivered", [])
            if artifact_id in delivered:
                return False
            delivered.append(artifact_id)
            self._save()
            return True

    def mark_artifact_registered(self, job_id: str, artifact_id: str, file_id: str) -> None:
        """记录生成产物已登记为输入，发送重试时不重复上传。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            if not value:
                return
            value.setdefault("registered_artifacts", {})[artifact_id] = file_id
            value.setdefault("pending_registrations", {}).pop(artifact_id, None)
            self._save()

    def mark_registration_pending(self, job_id: str, artifact_id: str, record: dict) -> None:
        """记录已交付但尚未登记为后续输入的生成文件。"""
        with self._lock:
            value = self._data["jobs"].get(job_id)
            if not value:
                return
            value.setdefault("pending_registrations", {})[artifact_id] = deepcopy(record)
            self._save()

    def pending_registrations(self, owner: str) -> list[tuple[str, str, dict]]:
        """列出指定 owner 可重试的生成文件登记。"""
        with self._lock:
            result = []
            for job_id, value in self._data["jobs"].items():
                if value.get("owner") != owner:
                    continue
                for artifact_id, record in value.get("pending_registrations", {}).items():
                    result.append((job_id, artifact_id, deepcopy(record)))
            return result

    def prune_pending_registrations(self, keep_record) -> int:
        """清理本地文件已不存在的生成物登记重试。"""
        removed = 0
        with self._lock:
            for value in self._data["jobs"].values():
                pending = value.get("pending_registrations", {})
                kept = {
                    artifact_id: record
                    for artifact_id, record in pending.items()
                    if keep_record(record)
                }
                removed += len(pending) - len(kept)
                value["pending_registrations"] = kept
            if removed:
                self._save()
        return removed
