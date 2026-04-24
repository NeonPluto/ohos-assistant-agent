"""数据库基础配置。

功能包含：
1. 连接池配置（可用于 SQLAlchemy `create_engine`）。
2. 关系型数据库连接串构建（支持 mysql、postgresql、sqlite）。
3. 支持通过证书安全连接数据库，不需要账户密码直接连接
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import quote_plus, urlencode

# 支持的关系型数据库类型
SUPPORTED_DATABASE_TYPES = {"mysql", "postgresql", "sqlite"}


@dataclass(frozen=True)
class TableMeta:
    """统一的表信息结构。"""

    table_name: str
    description: str = ""


TableNameInput = str | TableMeta | Mapping[str, Any]


def resolve_table_name(table: TableNameInput) -> str:
    """
    从 table 入参中解析表名并做合法性校验。

    支持：
    - str: 直接传表名
    - TableMeta: 结构化表信息
    - Mapping: 支持 `table_name` / `table` / `name` 键；
      也兼容历史结构 `{"table_info": {"table_name": "..."}}`
    """
    if isinstance(table, str):
        table_name = table.strip()
    elif isinstance(table, TableMeta):
        table_name = table.table_name.strip()
    elif isinstance(table, Mapping):
        nested = table.get("table_info")
        if isinstance(nested, Mapping):
            table_name = str(
                nested.get("table_name") or nested.get("table") or nested.get("name") or ""
            ).strip()
        else:
            table_name = str(
                table.get("table_name") or table.get("table") or table.get("name") or ""
            ).strip()
    else:
        raise TypeError("table 类型不支持，仅支持 str/TableMeta/Mapping")

    if not table_name:
        raise ValueError("table 中 table_name 不能为空")

    # 仅允许常见 SQL 标识符，避免拼接 SQL 时引入非法字符
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name) is None:
        raise ValueError(f"非法表名: {table_name}")
    return table_name


def _load_env_file(env_file: str = ".env") -> None:
    """从 .env 文件加载环境变量（不覆盖系统已存在变量）。"""
    env_path = Path(env_file)
    if not env_path.exists() or not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # 忽略空行和注释
        if not line or line.startswith("#"):
            continue
        # 支持 `export KEY=VALUE` 语法
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # 去掉包裹值的引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


# 在读取配置前优先加载 .env，保证默认配置可被文件覆盖
_load_env_file()


@dataclass(frozen=True)
class DatabaseConfig:
    """数据库配置对象。"""

    # 数据库类型：mysql / postgresql / sqlite
    db_type: str = os.getenv("DB_TYPE", "sqlite").strip().lower()
    # 数据库主机（sqlite 默认不使用）
    host: str = os.getenv("DB_HOST", "127.0.0.1").strip()
    # 数据库端口（sqlite 默认不使用）
    port: int = int(os.getenv("DB_PORT", "3306"))
    # 数据库用户名（sqlite 默认不使用）
    username: str = os.getenv("DB_USERNAME", "").strip()
    # 数据库密码（sqlite 默认不使用）
    password: str = os.getenv("DB_PASSWORD", "").strip()
    # 数据库名；sqlite 可用作文件名
    database: str = os.getenv("DB_NAME", "app").strip()
    # 是否启用证书模式（适用于 mysql / postgresql）
    use_cert_auth: bool = os.getenv("DB_USE_CERT_AUTH", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # 服务端 CA 证书路径
    db_ssl_ca: str = os.getenv("DB_SSL_CA", "").strip()
    # 客户端证书路径
    db_ssl_cert: str = os.getenv("DB_SSL_CERT", "").strip()
    # 客户端私钥路径
    db_ssl_key: str = os.getenv("DB_SSL_KEY", "").strip()
    # postgresql SSL 模式（证书模式下默认 verify-full）
    db_ssl_mode: str = os.getenv("DB_SSL_MODE", "").strip()
    # sqlite 文件路径；默认落盘到 data/assistant.db，避免重启丢数据
    sqlite_path: str = os.getenv("SQLITE_PATH", "data/assistant.db").strip()
    # 连接池大小（常驻连接数）
    pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    # 连接池允许的额外连接数（峰值时）
    max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    # 从连接池取连接的超时时间（秒）
    pool_timeout: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    # 连接回收时间（秒），避免连接过期失效
    pool_recycle: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    # 借出连接前进行健康检查
    pool_pre_ping: bool = os.getenv("DB_POOL_PRE_PING", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    def __post_init__(self) -> None:
        """初始化后校验配置合法性。"""
        if self.db_type not in SUPPORTED_DATABASE_TYPES:
            raise ValueError(
                f"不支持的数据库类型: {self.db_type}，"
                f"仅支持: {', '.join(sorted(SUPPORTED_DATABASE_TYPES))}"
            )
        if self.use_cert_auth and self.db_type == "sqlite":
            raise ValueError("sqlite 不支持证书连接模式，请关闭 DB_USE_CERT_AUTH")

    def build_database_url(self) -> str:
        """构建数据库连接 URL。"""
        if self.db_type == "sqlite":
            # 显式传入内存库时，仍允许按需使用临时数据库
            if self.sqlite_path in {":memory:", "memory"}:
                return "sqlite:///:memory:"

            # 默认使用文件数据库，保证数据可持久化到磁盘
            sqlite_file = Path(self.sqlite_path).expanduser()
            if not sqlite_file.is_absolute():
                sqlite_file = Path.cwd() / sqlite_file

            # 自动创建目录，避免因目录不存在导致写库失败
            sqlite_file.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{sqlite_file}"

        # mysql / postgresql 均按 username:password@host:port/database 组装
        auth = ""
        if not self.use_cert_auth:
            encoded_user = quote_plus(self.username)
            encoded_password = quote_plus(self.password)
            auth = (
                f"{encoded_user}:{encoded_password}@"
                if self.username or self.password
                else ""
            )

        base_url = f"{self.db_type}://{auth}{self.host}:{self.port}/{self.database}"

        if not self.use_cert_auth:
            return base_url

        ssl_params: dict[str, str] = {}
        if self.db_type == "postgresql":
            ssl_params["sslmode"] = self.db_ssl_mode or "verify-full"
            if self.db_ssl_ca:
                ssl_params["sslrootcert"] = self.db_ssl_ca
            if self.db_ssl_cert:
                ssl_params["sslcert"] = self.db_ssl_cert
            if self.db_ssl_key:
                ssl_params["sslkey"] = self.db_ssl_key
        elif self.db_type == "mysql":
            if self.db_ssl_ca:
                ssl_params["ssl_ca"] = self.db_ssl_ca
            if self.db_ssl_cert:
                ssl_params["ssl_cert"] = self.db_ssl_cert
            if self.db_ssl_key:
                ssl_params["ssl_key"] = self.db_ssl_key

        if not ssl_params:
            return base_url
        return f"{base_url}?{urlencode(ssl_params)}"

    def get_pool_options(self) -> dict[str, int | bool]:
        """获取连接池参数字典。"""
        return {
            "pool_size": self.pool_size,
            "max_overflow": self.max_overflow,
            "pool_timeout": self.pool_timeout,
            "pool_recycle": self.pool_recycle,
            "pool_pre_ping": self.pool_pre_ping,
        }


# 默认数据库配置实例，供业务模块直接导入使用
default_database_config = DatabaseConfig()