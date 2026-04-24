"""
日志配置与读取工具。

能力包含：
1. 从 `.env` 读取日志相关配置，未配置时使用默认值。
2. 支持多级别日志输出（DEBUG/INFO/WARNING/ERROR/CRITICAL）和可配置日志格式。
3. 支持控制台与文件双输出。
4. 支持日志滚动与压缩归档。
5. 支持日志读取：按行读取、按时间范围读取、按级别读取。
6. 考虑并发写文件场景，使用线程安全机制避免重复初始化与冲突。
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

# 支持的日志级别映射，便于从字符串配置转换为 logging 常量
LOG_LEVEL_MAP: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 解析日志行时间戳时支持的格式，尽量兼容常见日志布局
TIMESTAMP_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S,%f",
    "%Y-%m-%d %H:%M:%S",
)

# 根 logger 初始化锁，防止多线程重复注册 handler 导致日志重复输出
_LOGGER_INIT_LOCK = threading.Lock()
_LOGGER_INITIALIZED = False


def _load_env_file(env_file: str = ".env") -> None:
    """从 .env 文件加载环境变量（不覆盖已有系统变量）。"""
    env_path = Path(env_file)
    if not env_path.exists() or not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # 忽略空行与注释
        if not line or line.startswith("#"):
            continue
        # 兼容 export KEY=VALUE 语法
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # 去掉引号包裹
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


# 优先加载 .env，保证配置可直接通过环境变量生效
_load_env_file()


def _to_bool(raw: str | None, default: bool = False) -> bool:
    """将环境变量字符串转换为布尔值。"""
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_level(level_name: str) -> int:
    """将级别字符串转换为 logging level。"""
    return LOG_LEVEL_MAP.get(level_name.strip().upper(), logging.INFO)


@dataclass(frozen=True)
class LogConfig:
    """日志配置对象。"""

    # 日志器名称
    logger_name: str = os.getenv("LOG_NAME", "ohos-assistant").strip()
    # 全局日志级别
    level: str = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    # 日志格式
    fmt: str = os.getenv(
        "LOG_FORMAT",
        "%(asctime)s | %(levelname)s | %(name)s | %(threadName)s | %(message)s",
    ).strip()
    # 时间格式
    datefmt: str = os.getenv("LOG_DATE_FORMAT", "%Y-%m-%d %H:%M:%S").strip()
    # 是否启用控制台输出
    enable_console: bool = _to_bool(os.getenv("LOG_ENABLE_CONSOLE"), default=True)
    # 是否启用文件输出
    enable_file: bool = _to_bool(os.getenv("LOG_ENABLE_FILE"), default=True)
    # 日志文件路径
    file_path: str = os.getenv("LOG_FILE_PATH", "logs/assistant.log").strip()
    # 单个日志文件最大大小（字节）
    max_bytes: int = int(os.getenv("LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024)))
    # 轮转保留数量
    backup_count: int = int(os.getenv("LOG_FILE_BACKUP_COUNT", "5"))
    # 是否压缩轮转后的日志文件
    compress_rotated: bool = _to_bool(os.getenv("LOG_FILE_COMPRESS"), default=True)
    # 读取日志时默认返回行数上限（0 或负数表示不限制）
    read_default_limit: int = int(os.getenv("LOG_READ_DEFAULT_LIMIT", "200"))

    def resolved_level(self) -> int:
        """返回解析后的日志级别常量。"""
        return _parse_level(self.level)


def _build_rotating_handler(config: LogConfig) -> RotatingFileHandler:
    """创建支持滚动与压缩的文件 handler。"""
    log_file = Path(config.file_path).expanduser()
    if not log_file.is_absolute():
        log_file = Path.cwd() / log_file
    # 自动创建日志目录，避免目录不存在导致初始化失败
    log_file.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=max(config.max_bytes, 1),
        backupCount=max(config.backup_count, 0),
        encoding="utf-8",
        delay=True,
    )

    if config.compress_rotated:
        # 设置轮转目标文件命名规则：*.log.N.gz
        handler.namer = lambda name: f"{name}.gz"

        # 设置轮转压缩逻辑，避免归档占用过多磁盘
        def _gzip_rotator(source: str, dest: str) -> None:
            with open(source, "rb") as src, gzip.open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(source)

        handler.rotator = _gzip_rotator

    return handler


def configure_logging(config: LogConfig | None = None) -> logging.Logger:
    """
    初始化并返回日志器。

    说明：
    - 线程安全：通过全局锁防止重复初始化。
    - 幂等：重复调用不会重复添加 handler。
    """
    global _LOGGER_INITIALIZED  # noqa: PLW0603
    cfg = config or LogConfig()

    with _LOGGER_INIT_LOCK:
        logger = logging.getLogger(cfg.logger_name)
        logger.setLevel(cfg.resolved_level())
        logger.propagate = False

        if _LOGGER_INITIALIZED:
            return logger

        formatter = logging.Formatter(fmt=cfg.fmt, datefmt=cfg.datefmt)

        if cfg.enable_console:
            # 控制台输出，适合本地调试观察
            console_handler = logging.StreamHandler()
            console_handler.setLevel(cfg.resolved_level())
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        if cfg.enable_file:
            # 文件输出，适合保留审计与离线排障信息
            file_handler = _build_rotating_handler(cfg)
            file_handler.setLevel(cfg.resolved_level())
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        _LOGGER_INITIALIZED = True
        return logger


def get_logger(name: str | None = None, config: LogConfig | None = None) -> logging.Logger:
    """获取日志器；若未初始化会先执行默认配置初始化。"""
    base_logger = configure_logging(config=config)
    if not name:
        return base_logger
    # 子 logger 继承父级 handler，便于按模块区分 name
    return base_logger.getChild(name)


def _parse_log_datetime(log_line: str) -> datetime | None:
    """尝试从日志行开头解析时间戳。"""
    # 默认日志格式以 `%(asctime)s | ...` 开头，先取第一个字段
    ts = log_line.split("|", 1)[0].strip()
    if not ts:
        return None
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _contains_level(log_line: str, levels: set[str] | None) -> bool:
    """按级别过滤日志行。"""
    if not levels:
        return True
    upper_line = log_line.upper()
    return any(f"| {level} |" in upper_line for level in levels)


def _slice_lines(lines: list[str], limit: int | None) -> list[str]:
    """统一处理读取行数限制。"""
    if limit is None:
        return lines
    if limit <= 0:
        return lines
    return lines[-limit:]


def iter_log_files(log_file_path: str) -> Iterable[Path]:
    """返回日志文件与其轮转文件（按时间从新到旧排序）。"""
    target = Path(log_file_path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target

    candidates = [target] + sorted(
        target.parent.glob(f"{target.name}*"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )

    # 去重，避免主文件被重复包含
    seen: set[Path] = set()
    for item in candidates:
        if item in seen or not item.exists() or item.is_dir():
            continue
        seen.add(item)
        yield item


def _read_single_log_file(path: Path) -> list[str]:
    """读取单个日志文件（支持 .gz）。"""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def read_logs(
    *,
    log_file_path: str | None = None,
    limit: int | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    levels: list[str] | None = None,
) -> list[str]:
    """
    读取日志内容，支持按行数、时间范围、级别过滤。

    参数：
    - log_file_path: 日志文件路径，默认使用配置中的 `file_path`。
    - limit: 返回的最大行数（取最后 N 行），不传则使用配置默认值。
    - start_time/end_time: 日志时间过滤范围（闭区间）。
    - levels: 级别过滤，如 ["ERROR", "WARNING"]。
    """
    cfg = LogConfig()
    target_file = log_file_path or cfg.file_path
    normalized_levels = {level.strip().upper() for level in levels} if levels else None
    final_limit = cfg.read_default_limit if limit is None else limit

    matched: list[str] = []
    for path in iter_log_files(target_file):
        for line in _read_single_log_file(path):
            if not _contains_level(line, normalized_levels):
                continue

            if start_time or end_time:
                line_dt = _parse_log_datetime(line)
                # 无法解析时间戳时，时间过滤模式下跳过，避免误选
                if not line_dt:
                    continue
                if start_time and line_dt < start_time:
                    continue
                if end_time and line_dt > end_time:
                    continue

            matched.append(line)

    return _slice_lines(matched, final_limit)


# 默认日志器：模块导入后可直接使用
logger = get_logger()