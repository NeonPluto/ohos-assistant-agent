"""
    用户管理类:
        1. 对接数据库,能够读写用户数据,用户表为ohos_user_info
        2. 用户表包含字段:
            - id: 用户ID
            - username: 用户名
            - password: 密码(加密后的密码)
            - create_time: 创建时间(时间戳)
            - update_time: 更新时间(时间戳)
            - is_deleted: 是否删除(0:否,1:是)
            - is_active: 是否在线(0:否,1:是)
            - role (0: admin, 1: developer, 3: operation, 4: viewer)
        3. 支持用户注册、登陆、注销(逻辑删除)、删除(逻辑删除)
        4. 支持并发处理,需要考虑线程安全
        5. 支持批量删除,通过管理员角色操作
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from typing import Any

from database.crud import DatabaseCRUD, default_crud
from utils.log_utils import get_logger

logger = get_logger("auth.user")


class UserManager:
    """用户管理能力封装。"""

    TABLE_NAME = "ohos_user_info"
    ROLE_ADMIN = 0
    ROLE_DEVELOPER = 1
    ROLE_OPERATION = 3
    ROLE_VIEWER = 4
    VALID_ROLES = {ROLE_ADMIN, ROLE_DEVELOPER, ROLE_OPERATION, ROLE_VIEWER}

    def __init__(self, crud: DatabaseCRUD | None = None) -> None:
        self.crud = crud or default_crud
        # 保护关键读写路径，避免多线程并发导致竞态（如重复注册、状态覆盖）
        self._lock = threading.RLock()
        self._ensure_table()

    def _ensure_table(self) -> None:
        """确保用户表存在。"""
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            create_time INTEGER NOT NULL,
            update_time INTEGER NOT NULL,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 0,
            role INTEGER NOT NULL DEFAULT 4
        )
        """
        with self._lock:
            self.crud.write(create_sql)
            self._ensure_role_column()

    def _ensure_role_column(self) -> None:
        """兼容旧表结构：若缺失 role 字段则自动补齐。"""
        columns_sql = f"PRAGMA table_info({self.TABLE_NAME})"
        columns = self.crud.read(columns_sql)
        column_names = {item["name"] for item in columns}
        if "role" in column_names:
            return
        self.crud.write(
            f"ALTER TABLE {self.TABLE_NAME} ADD COLUMN role INTEGER NOT NULL DEFAULT 4"
        )
        logger.info("用户表已补充 role 字段")

    def _hash_password(self, password: str) -> str:
        """对密码进行 SHA-256 摘要。"""
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def _now_ts(self) -> int:
        return int(time.time())

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """按用户名查询有效用户。"""
        sql = f"""
        SELECT id, username, password, create_time, update_time, is_deleted, is_active, role
        FROM {self.TABLE_NAME}
        WHERE username = ? AND is_deleted = 0
        LIMIT 1
        """
        rows = self.crud.read(sql, (username,))
        return rows[0] if rows else None

    def register(self, username: str, password: str, role: int = ROLE_VIEWER) -> bool:
        """注册用户。用户名已存在（含已删除）时返回 False。"""
        if not username or not password:
            raise ValueError("username 和 password 不能为空")
        if role not in self.VALID_ROLES:
            raise ValueError(f"非法角色: {role}")

        with self._lock:
            check_sql = f"SELECT id FROM {self.TABLE_NAME} WHERE username = ? LIMIT 1"
            exists = self.crud.read(check_sql, (username,))
            if exists:
                logger.warning("注册失败，用户名已存在: %s", username)
                return False

            now_ts = self._now_ts()
            insert_sql = f"""
            INSERT INTO {self.TABLE_NAME}
            (username, password, create_time, update_time, is_deleted, is_active, role)
            VALUES (?, ?, ?, ?, 0, 0, ?)
            """
            try:
                self.crud.write(
                    insert_sql,
                    (username, self._hash_password(password), now_ts, now_ts, role),
                )
            except sqlite3.IntegrityError:
                # 并发场景下可能同时抢注同一用户名，依赖唯一索引进行最终裁决
                logger.warning("注册失败，用户名并发冲突: %s", username)
                return False

            logger.info("用户注册成功: username=%s role=%s", username, role)
            return True

    def login(self, username: str, password: str) -> bool:
        """用户登录，成功后将 is_active 置为 1。"""
        if not username or not password:
            raise ValueError("username 和 password 不能为空")

        with self._lock:
            user = self.get_user_by_username(username)
            if not user:
                logger.warning("登录失败，用户不存在或已删除: %s", username)
                return False

            if user["password"] != self._hash_password(password):
                logger.warning("登录失败，密码错误: %s", username)
                return False

            update_sql = (
                f"UPDATE {self.TABLE_NAME} SET is_active = 1, update_time = ? WHERE id = ?"
            )
            self.crud.write(update_sql, (self._now_ts(), user["id"]))
            logger.info("用户登录成功: %s", username)
            return True

    def logout(self, username: str) -> bool:
        """用户登出（下线），将 is_active 置为 0。"""
        with self._lock:
            user = self.get_user_by_username(username)
            if not user:
                logger.warning("登出失败，用户不存在或已删除: %s", username)
                return False

            update_sql = (
                f"UPDATE {self.TABLE_NAME} SET is_active = 0, update_time = ? WHERE id = ?"
            )
            self.crud.write(update_sql, (self._now_ts(), user["id"]))
            logger.info("用户登出成功: %s", username)
            return True

    def deactivate(self, username: str) -> bool:
        """用户注销（逻辑删除）。"""
        with self._lock:
            user = self.get_user_by_username(username)
            if not user:
                logger.warning("注销失败，用户不存在或已删除: %s", username)
                return False

            update_sql = f"""
            UPDATE {self.TABLE_NAME}
            SET is_deleted = 1, is_active = 0, update_time = ?
            WHERE id = ?
            """
            self.crud.write(update_sql, (self._now_ts(), user["id"]))
            logger.info("用户注销成功: %s", username)
            return True

    def delete(self, username: str) -> bool:
        """删除用户（逻辑删除）。"""
        return self.deactivate(username)

    def batch_delete(self, admin_username: str, usernames: list[str]) -> int:
        """管理员批量逻辑删除用户，返回成功删除数量。"""
        if not usernames:
            return 0

        cleaned_usernames = [name.strip() for name in usernames if name and name.strip()]
        if not cleaned_usernames:
            return 0

        with self._lock:
            admin = self.get_user_by_username(admin_username)
            if not admin or admin["role"] != self.ROLE_ADMIN:
                logger.warning("批量删除失败，操作者不是管理员: %s", admin_username)
                raise PermissionError("仅管理员可执行批量删除")

            # 避免管理员误删自己，且去重减少无效更新
            target_usernames = sorted(set(cleaned_usernames) - {admin_username})
            if not target_usernames:
                return 0

            placeholders = ", ".join(["?"] * len(target_usernames))
            update_sql = f"""
            UPDATE {self.TABLE_NAME}
            SET is_deleted = 1, is_active = 0, update_time = ?
            WHERE is_deleted = 0
              AND username IN ({placeholders})
            """
            params = (self._now_ts(), *target_usernames)
            affected = self.crud.write(update_sql, params)
            logger.info(
                "管理员批量删除完成: admin=%s target_count=%s affected=%s",
                admin_username,
                len(target_usernames),
                affected,
            )
            return max(affected, 0)


default_user_manager = UserManager()