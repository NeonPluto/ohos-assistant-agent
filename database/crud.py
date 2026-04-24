"""
数据库基础 CRUD 能力。

能力包含：
1. 创建数据库连接（当前优先支持 sqlite）。
2. 读数据库内容。
3. 写数据库内容。
4. 读写过程提供事务保证。
5. 关键操作记录日志。
6. 支持 Excel 导入。
7. 支持批量读写。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable, Iterator, Sequence

from database.config import (
    DatabaseConfig,
    TableNameInput,
    default_database_config,
    resolve_table_name,
)
from utils.log_utils import get_logger

logger = get_logger("database.crud")


class DatabaseCRUD:
    """数据库 CRUD 封装。"""

    def __init__(self, config: DatabaseConfig | None = None) -> None:
        self.config = config or default_database_config
        self._validate_supported_database()

    def _validate_supported_database(self) -> None:
        if self.config.db_type != "sqlite":
            raise NotImplementedError(
                f"当前仅实现 sqlite，暂不支持 db_type={self.config.db_type}。"
            )

    def _resolve_sqlite_path(self) -> str:
        sqlite_path = self.config.sqlite_path
        if sqlite_path in {":memory:", "memory"}:
            return ":memory:"
        resolved = Path(sqlite_path).expanduser()
        if not resolved.is_absolute():
            resolved = Path.cwd() / resolved
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return str(resolved)

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """创建数据库连接。"""
        db_path = self._resolve_sqlite_path()
        logger.info("创建数据库连接: %s", db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
            logger.debug("数据库连接已关闭: %s", db_path)

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """
        事务上下文。

        成功时 commit，异常时 rollback。
        """
        with self.get_connection() as conn:
            try:
                conn.execute("BEGIN")
                logger.debug("事务开始")
                yield conn
                conn.commit()
                logger.debug("事务提交")
            except Exception:
                conn.rollback()
                logger.exception("事务回滚")
                raise

    def read(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """执行查询语句并返回字典列表。"""
        bound_params = params or ()
        logger.info("执行读操作: %s", sql)
        with self.get_connection() as conn:
            cursor = conn.execute(sql, bound_params)
            rows = cursor.fetchall()
            result = [dict(row) for row in rows]
            logger.info("读操作完成，返回 %s 条记录", len(result))
            return result

    def write(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> int:
        """执行单条写操作，返回受影响行数。"""
        bound_params = params or ()
        logger.info("执行写操作: %s", sql)
        with self.transaction() as conn:
            cursor = conn.execute(sql, bound_params)
            affected = cursor.rowcount
            logger.info("写操作完成，影响 %s 行", affected)
            return affected

    def batch_write(
        self,
        sql: str,
        params_list: Iterable[Sequence[Any]],
        *,
        batch_size: int = 500,
    ) -> int:
        """
        批量写入，返回总影响行数。

        说明：
        - 所有分批在同一事务内执行，保证原子性。
        """
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")

        payload = list(params_list)
        if not payload:
            return 0

        total_affected = 0
        logger.info("批量写入开始，总记录数=%s，batch_size=%s", len(payload), batch_size)
        with self.transaction() as conn:
            for start in range(0, len(payload), batch_size):
                chunk = payload[start : start + batch_size]
                cursor = conn.executemany(sql, chunk)
                chunk_affected = cursor.rowcount if cursor.rowcount != -1 else len(chunk)
                total_affected += chunk_affected
                logger.debug(
                    "批量写入分片完成: start=%s size=%s affected=%s",
                    start,
                    len(chunk),
                    chunk_affected,
                )
        logger.info("批量写入完成，总影响 %s 行", total_affected)
        return total_affected

    def batch_read(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        batch_size: int = 500,
    ) -> Iterator[list[dict[str, Any]]]:
        """
        批量读取（分页）迭代器。

        说明：
        - 传入 SQL 时不要包含 LIMIT/OFFSET，本方法会自动追加。
        """
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")

        base_params = tuple(params or ())
        offset = 0
        logger.info("批量读取开始，batch_size=%s", batch_size)
        while True:
            paged_sql = f"{sql} LIMIT ? OFFSET ?"
            page_params = (*base_params, batch_size, offset)
            with self.get_connection() as conn:
                rows = conn.execute(paged_sql, page_params).fetchall()
            if not rows:
                logger.info("批量读取结束，总读取 %s 条", offset)
                break
            batch = [dict(row) for row in rows]
            logger.debug("批量读取分片: offset=%s size=%s", offset, len(batch))
            yield batch
            offset += len(batch)

    def import_excel(
        self,
        excel_path: str,
        table: TableNameInput | None = None,
        *,
        table_name: str | None = None,
        sheet_name: str | int = 0,
        batch_size: int = 500,
    ) -> int:
        """
        从 Excel 导入到指定数据表，返回导入行数。

        约定：
        - Excel 列名与数据库字段名保持一致。
        - 仅处理非空行。
        """
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover
            raise ImportError("导入 Excel 需要安装 pandas 和 openpyxl") from exc

        path = Path(excel_path).expanduser()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")

        resolved_table_input: TableNameInput
        if table_name is not None:
            resolved_table_input = table_name
        elif table is not None:
            resolved_table_input = table
        else:
            raise ValueError("table 或 table_name 至少需要提供一个")

        resolved_table_name = resolve_table_name(resolved_table_input)
        logger.info("开始导入 Excel: file=%s table=%s", str(path), resolved_table_name)
        df = pd.read_excel(path, sheet_name=sheet_name)
        df = df.dropna(how="all")
        if df.empty:
            logger.warning("Excel 无可导入数据: %s", str(path))
            return 0

        columns = [str(col).strip() for col in df.columns]
        if any(not col for col in columns):
            raise ValueError("Excel 存在空列名，无法映射数据库字段")

        placeholders = ", ".join(["?"] * len(columns))
        quoted_columns = ", ".join(columns)
        insert_sql = (
            f"INSERT INTO {resolved_table_name} ({quoted_columns}) VALUES ({placeholders})"
        )

        values: list[tuple[Any, ...]] = [
            tuple(None if value != value else value for value in row)
            for row in df.itertuples(index=False, name=None)
        ]

        imported = self.batch_write(insert_sql, values, batch_size=batch_size)
        logger.info("Excel 导入完成: table=%s imported=%s", resolved_table_name, imported)
        return imported


default_crud = DatabaseCRUD()