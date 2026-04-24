"""
文件操作工具模块说明。

目标能力:
1. 文件路径安全性判断，避免路径越界访问（例如跳出工作目录）。
2. 列出文件和文件夹；若指定文件类型，仅返回指定类型文件。
3. 提供读文件能力。
4. 提供编辑文件能力。
5. 读写流程参考 `agents/s_full_langgraph_multisession_fs.py` 中已有实现。
6. 支持写文件队列，避免并发写同一目录导致文件锁死。
7. 支持写队列去重与合并：入队时检查是否已存在同目标任务，存在则合并。
8. 支持写队列结果返回，便于后续流程根据执行结果继续处理。
9. 写入前检查文件状态：若文件已存在且内容不一致，可按参数选择追加而非覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Condition, Lock, Thread
from typing import Literal


def _normalize_suffix(suffix: str | None) -> str | None:
    """将文件后缀标准化为 `.xxx` 形式。"""
    if suffix is None:
        return None
    normalized = suffix.strip()
    if not normalized:
        return None
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized.lower()


@dataclass(frozen=True)
class WriteResult:
    """写任务执行结果。"""

    path: str
    status: Literal["success", "skipped", "error"]
    message: str
    bytes_written: int = 0


@dataclass
class WriteTask:
    """写任务定义。"""

    path: str
    content: str
    append_if_exists_and_different: bool = False
    merge_with_newline: bool = True
    result: WriteResult | None = field(default=None, init=False)
    _done: bool = field(default=False, init=False, repr=False)
    _cond: Condition = field(default_factory=Condition, init=False, repr=False)

    def complete(self, result: WriteResult) -> None:
        """标记任务完成并唤醒等待方。"""
        with self._cond:
            self.result = result
            self._done = True
            self._cond.notify_all()

    def wait(self, timeout: float | None = None) -> WriteResult:
        """阻塞等待任务结果。"""
        with self._cond:
            if not self._done:
                self._cond.wait(timeout=timeout)
            if not self.result:
                raise TimeoutError(f"等待写任务超时: {self.path}")
            return self.result


class FileUtils:
    """文件工具，含安全读写与写队列。"""

    def __init__(self, workdir: str | Path | None = None) -> None:
        self.workdir = (Path(workdir) if workdir else Path.cwd()).resolve()
        self._queue_lock = Lock()
        self._queue: list[WriteTask] = []
        self._pending: dict[str, WriteTask] = {}
        self._worker_cond = Condition(self._queue_lock)
        self._worker = Thread(target=self._worker_loop, daemon=True, name="file-write-worker")
        self._worker.start()

    def safe_path(self, relative_path: str | Path) -> Path:
        """将相对路径映射到工作目录，并校验不越界。"""
        resolved = (self.workdir / Path(relative_path)).resolve()
        if not resolved.is_relative_to(self.workdir):
            raise ValueError(f"路径越界: {relative_path}")
        return resolved

    def list_files(self, path: str = ".", suffix: str | None = None) -> list[str]:
        """列出目录下文件（递归），可按后缀过滤。"""
        target = self.safe_path(path)
        if not target.exists():
            raise FileNotFoundError(f"路径不存在: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"不是目录: {path}")

        normalized_suffix = _normalize_suffix(suffix)
        files: list[str] = []
        for entry in sorted(target.rglob("*")):
            if not entry.is_file():
                continue
            if normalized_suffix and entry.suffix.lower() != normalized_suffix:
                continue
            files.append(entry.relative_to(target).as_posix())
        return files

    def read_file(self, path: str, limit: int | None = None) -> str:
        """读取文本文件内容，可限制行数。"""
        target = self.safe_path(path)
        if target.is_dir():
            # 目录按可读文本返回，方便调试和排查
            return "\n".join(sorted(p.name for p in target.iterdir()))

        lines = target.read_text(encoding="utf-8").splitlines()
        if limit is not None:
            lines = lines[: max(limit, 0)]
        return "\n".join(lines)

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """替换文件中的首个匹配文本。"""
        target = self.safe_path(path)
        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            raise ValueError(f"待替换文本不存在: {path}")
        updated = content.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")
        return f"Edited {path}"

    def write_file(
        self,
        path: str,
        content: str,
        *,
        append_if_exists_and_different: bool = False,
    ) -> WriteResult:
        """同步写文件，返回执行结果。"""
        target = self.safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if existing == content:
                return WriteResult(path=path, status="skipped", message="内容一致，跳过写入")
            if append_if_exists_and_different:
                merged = f"{existing}\n{content}" if existing else content
                target.write_text(merged, encoding="utf-8")
                return WriteResult(
                    path=path,
                    status="success",
                    message="文件已存在且内容不同，已追加写入",
                    bytes_written=len(merged.encode("utf-8")),
                )

        target.write_text(content, encoding="utf-8")
        return WriteResult(
            path=path,
            status="success",
            message="写入成功",
            bytes_written=len(content.encode("utf-8")),
        )

    def enqueue_write(
        self,
        path: str,
        content: str,
        *,
        append_if_exists_and_different: bool = False,
        merge_with_newline: bool = True,
    ) -> WriteTask:
        """
        入队写任务。

        若队列中已有同一路径任务，则执行合并:
        - 默认用换行连接旧内容与新内容；
        - 也可直接拼接。
        """
        task = WriteTask(
            path=path,
            content=content,
            append_if_exists_and_different=append_if_exists_and_different,
            merge_with_newline=merge_with_newline,
        )
        with self._queue_lock:
            pending = self._pending.get(path)
            if pending:
                sep = "\n" if pending.merge_with_newline else ""
                pending.content = f"{pending.content}{sep}{content}"
                return pending

            self._queue.append(task)
            self._pending[path] = task
            self._worker_cond.notify()
            return task

    def _worker_loop(self) -> None:
        """后台消费写队列，串行处理，降低并发写冲突风险。"""
        while True:
            with self._queue_lock:
                while not self._queue:
                    self._worker_cond.wait()
                task = self._queue.pop(0)
                self._pending.pop(task.path, None)

            try:
                result = self.write_file(
                    path=task.path,
                    content=task.content,
                    append_if_exists_and_different=task.append_if_exists_and_different,
                )
            except Exception as exc:  # noqa: BLE001
                result = WriteResult(
                    path=task.path,
                    status="error",
                    message=f"写入失败: {exc}",
                )
            task.complete(result)


# 默认实例：直接基于当前工作目录工作
default_file_utils = FileUtils()