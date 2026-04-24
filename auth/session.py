"""
    session管理
        1. 支持session的创建、查询、更新、删除
        2. 支持session有效性校验(权限内容访问、有效期校验)
        3. 支持session的持久化存储(数据库)
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from database.crud import DatabaseCRUD, default_crud
from utils.log_utils import get_logger

logger = get_logger("auth.session")

_USER_TABLE = "ohos_user_info"


@dataclass(frozen=True)
class ValidatedSession:
    """通过校验的会话上下文。"""

    session_id: int
    session_token: str
    user_id: int
    permissions: tuple[str, ...]
    expires_at: int


class SessionManager:
    """会话管理能力封装（SQLite 持久化）。"""

    TABLE_NAME = "ohos_user_session"

    def __init__(self, crud: DatabaseCRUD | None = None) -> None:
        self.crud = crud or default_crud
        self._lock = threading.RLock()
        self._default_ttl_seconds = int(os.getenv("SESSION_TTL_SECONDS", "7200"))
        self._ensure_table()

    def _ensure_table(self) -> None:
        """确保会话表存在。"""
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            permissions TEXT,
            expires_at INTEGER NOT NULL,
            create_time INTEGER NOT NULL,
            update_time INTEGER NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0
        )
        """
        with self._lock:
            self.crud.write(create_sql)

    def _now_ts(self) -> int:
        return int(time.time())

    def _encode_permissions(self, permissions: Iterable[str] | None) -> str | None:
        if permissions is None:
            return None
        items = [str(p).strip() for p in permissions if str(p).strip()]
        if not items:
            return None
        return json.dumps(items, ensure_ascii=False, separators=(",", ":"))

    def _decode_permissions(self, raw: str | None) -> tuple[str, ...]:
        if not raw:
            return ()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("会话 permissions 字段非合法 JSON，按空权限处理")
            return ()
        if isinstance(data, list):
            return tuple(str(x).strip() for x in data if str(x).strip())
        if isinstance(data, dict):
            return tuple(str(k).strip() for k in data.keys() if str(k).strip())
        return ()

    def _row_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "session_token": str(row["session_token"]),
            "user_id": int(row["user_id"]),
            "permissions": list(self._decode_permissions(row.get("permissions"))),
            "expires_at": int(row["expires_at"]),
            "create_time": int(row["create_time"]),
            "update_time": int(row["update_time"]),
            "is_revoked": int(row["is_revoked"]),
        }

    def create_session(
        self,
        user_id: int,
        *,
        ttl_seconds: int | None = None,
        permissions: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """创建会话并落库，返回会话记录字典。"""
        if user_id <= 0:
            raise ValueError("user_id 非法")

        ttl = self._default_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds 必须大于 0")

        token = secrets.token_urlsafe(32)
        now_ts = self._now_ts()
        expires_at = now_ts + ttl
        perm_blob = self._encode_permissions(permissions)

        insert_sql = f"""
        INSERT INTO {self.TABLE_NAME}
        (session_token, user_id, permissions, expires_at, create_time, update_time, is_revoked)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        """
        with self._lock:
            if not self._is_user_accessible_unlocked(user_id):
                raise ValueError("用户不存在或已删除，无法创建会话")
            try:
                self.crud.write(
                    insert_sql,
                    (token, user_id, perm_blob, expires_at, now_ts, now_ts),
                )
            except sqlite3.IntegrityError as exc:
                logger.exception("创建会话失败，token 冲突（极低概率）")
                raise RuntimeError("创建会话失败，请重试") from exc

            row = self._fetch_row_by_token_unlocked(token)
            if not row:
                raise RuntimeError("创建会话后查询失败")

        logger.info("会话创建成功: user_id=%s expires_at=%s", user_id, expires_at)
        return self._row_to_dict(row)

    def get_session_by_token(self, session_token: str) -> dict[str, Any] | None:
        """按 token 查询会话（不区分是否撤销/过期）。"""
        if not session_token:
            return None
        with self._lock:
            row = self._fetch_row_by_token_unlocked(session_token)
        return self._row_to_dict(row) if row else None

    def get_session_by_id(self, session_id: int) -> dict[str, Any] | None:
        """按主键查询会话。"""
        if session_id <= 0:
            return None
        sql = f"""
        SELECT id, session_token, user_id, permissions, expires_at,
               create_time, update_time, is_revoked
        FROM {self.TABLE_NAME}
        WHERE id = ?
        LIMIT 1
        """
        with self._lock:
            rows = self.crud.read(sql, (session_id,))
        return self._row_to_dict(rows[0]) if rows else None

    def list_sessions_by_user(
        self,
        user_id: int,
        *,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        """按用户列出会话。"""
        if user_id <= 0:
            return []

        if include_revoked:
            sql = f"""
            SELECT id, session_token, user_id, permissions, expires_at,
                   create_time, update_time, is_revoked
            FROM {self.TABLE_NAME}
            WHERE user_id = ?
            ORDER BY id DESC
            """
            params: tuple[Any, ...] = (user_id,)
        else:
            sql = f"""
            SELECT id, session_token, user_id, permissions, expires_at,
                   create_time, update_time, is_revoked
            FROM {self.TABLE_NAME}
            WHERE user_id = ? AND is_revoked = 0
            ORDER BY id DESC
            """
            params = (user_id,)

        rows = self.crud.read(sql, params)
        return [self._row_to_dict(row) for row in rows]

    def update_session(
        self,
        session_token: str,
        *,
        ttl_seconds: int | None = None,
        permissions: Iterable[str] | None | object = ...,
        extend_seconds: int | None = None,
    ) -> bool:
        """
        更新会话。

        - ttl_seconds: 将过期时间设置为「当前时间 + ttl_seconds」
        - extend_seconds: 在当前 expires_at 基础上延长
        - permissions: 传入可迭代对象则覆盖；不传则保持不变
        """
        if not session_token:
            return False

        sets: list[str] = []
        params: list[Any] = []

        now_ts = self._now_ts()
        sets.append("update_time = ?")
        params.append(now_ts)

        if ttl_seconds is not None:
            if int(ttl_seconds) <= 0:
                raise ValueError("ttl_seconds 必须大于 0")
            sets.append("expires_at = ?")
            params.append(now_ts + int(ttl_seconds))

        if extend_seconds is not None:
            if int(extend_seconds) <= 0:
                raise ValueError("extend_seconds 必须大于 0")
            sets.append("expires_at = expires_at + ?")
            params.append(int(extend_seconds))

        if permissions is not ...:
            sets.append("permissions = ?")
            params.append(self._encode_permissions(permissions))  # type: ignore[arg-type]

        if len(params) == 1:
            # 仅有 update_time
            return False

        params.append(session_token)
        update_sql = f"""
        UPDATE {self.TABLE_NAME}
        SET {", ".join(sets)}
        WHERE session_token = ? AND is_revoked = 0
        """

        with self._lock:
            affected = self.crud.write(update_sql, tuple(params))

        if affected > 0:
            logger.info("会话更新成功: token_suffix=%s", session_token[-8:])
        return affected > 0

    def revoke_session(self, session_token: str) -> bool:
        """撤销会话（软删除）。"""
        if not session_token:
            return False
        now_ts = self._now_ts()
        sql = f"""
        UPDATE {self.TABLE_NAME}
        SET is_revoked = 1, update_time = ?
        WHERE session_token = ? AND is_revoked = 0
        """
        with self._lock:
            affected = self.crud.write(sql, (now_ts, session_token))
        if affected > 0:
            logger.info("会话已撤销: token_suffix=%s", session_token[-8:])
        return affected > 0

    def revoke_sessions_for_user(self, user_id: int) -> int:
        """撤销某用户全部会话，返回影响行数。"""
        if user_id <= 0:
            return 0
        now_ts = self._now_ts()
        sql = f"""
        UPDATE {self.TABLE_NAME}
        SET is_revoked = 1, update_time = ?
        WHERE user_id = ? AND is_revoked = 0
        """
        with self._lock:
            affected = self.crud.write(sql, (now_ts, user_id))
        if affected > 0:
            logger.info("批量撤销用户会话: user_id=%s affected=%s", user_id, affected)
        return max(int(affected), 0)

    def delete_session(self, session_token: str) -> bool:
        """物理删除会话记录。"""
        if not session_token:
            return False
        sql = f"DELETE FROM {self.TABLE_NAME} WHERE session_token = ?"
        with self._lock:
            affected = self.crud.write(sql, (session_token,))
        if affected > 0:
            logger.info("会话物理删除: token_suffix=%s", session_token[-8:])
        return affected > 0

    def validate_session(
        self,
        session_token: str,
        *,
        required_permission: str | None = None,
    ) -> ValidatedSession:
        """
        校验会话：未撤销、未过期、用户有效，并按需校验权限点。

        permissions 约定：
        - 空：不限制内容权限（仍要求用户未被删除）
        - 非空：required_permission 必须命中集合；支持通配符 "*"
        """
        if not session_token:
            raise PermissionError("session_token 不能为空")

        with self._lock:
            row = self._fetch_row_by_token_unlocked(session_token)
            if not row:
                raise PermissionError("会话不存在")

            if int(row["is_revoked"]) != 0:
                raise PermissionError("会话已撤销")

            now_ts = self._now_ts()
            expires_at = int(row["expires_at"])
            if now_ts > expires_at:
                raise PermissionError("会话已过期")

            user_id = int(row["user_id"])
            user_ok = self._is_user_accessible_unlocked(user_id)
            if not user_ok:
                raise PermissionError("用户不可用或已删除")

            perms = self._decode_permissions(row.get("permissions"))

        if required_permission:
            req = required_permission.strip()
            if not req:
                raise ValueError("required_permission 不能为空字符串")
            if perms and "*" not in perms and req not in perms:
                raise PermissionError("会话权限不足")

        # 校验通过后刷新活跃时间，便于审计与排查
        self.crud.write(
            f"UPDATE {self.TABLE_NAME} SET update_time = ? WHERE session_token = ?",
            (self._now_ts(), session_token),
        )

        return ValidatedSession(
            session_id=int(row["id"]),
            session_token=str(row["session_token"]),
            user_id=user_id,
            permissions=perms,
            expires_at=expires_at,
        )

    def _fetch_row_by_token_unlocked(self, session_token: str) -> dict[str, Any] | None:
        sql = f"""
        SELECT id, session_token, user_id, permissions, expires_at,
               create_time, update_time, is_revoked
        FROM {self.TABLE_NAME}
        WHERE session_token = ?
        LIMIT 1
        """
        rows = self.crud.read(sql, (session_token,))
        return rows[0] if rows else None

    def _is_user_accessible_unlocked(self, user_id: int) -> bool:
        sql = f"""
        SELECT id
        FROM {_USER_TABLE}
        WHERE id = ? AND is_deleted = 0
        LIMIT 1
        """
        rows = self.crud.read(sql, (user_id,))
        return bool(rows)


default_session_manager = SessionManager()
