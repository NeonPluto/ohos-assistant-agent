"""
    用户API接口:
        1. 支持用户登陆注册能力
        2. 支持管理员批量导入删除
        3. 支持修改密码的操作
        4. 支持给原始密码非对称加密
        5. 支持session管理
        6. 支持token获取用户信息
        7. 支持session池管理(使用队列管理session,避免并发对同一个用户操作,出现session冲突)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from auth.encryption_and_decryption import SecurityEncryption
from auth.user import UserManager, default_user_manager
from utils.log_utils import get_logger

logger = get_logger("auth.user_api")


@dataclass
class SessionInfo:
    """会话信息对象。"""

    session_id: str
    username: str
    created_at: int
    last_active_at: int
    is_active: bool = True


class SessionPool:
    """
    基于队列的 session 池。

    设计目标：
    - 每个用户持有自己的 session 队列，避免并发时对同一用户 session 产生覆盖冲突。
    - 通过全局锁保证结构增删查改线程安全。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queues: dict[str, queue.Queue[SessionInfo]] = {}
        self._active_session_map: dict[str, SessionInfo] = {}

    def create_session(self, username: str) -> SessionInfo:
        """创建并入池 session。"""
        with self._lock:
            if username not in self._queues:
                self._queues[username] = queue.Queue()
            now = int(time.time())
            session = SessionInfo(
                session_id=secrets.token_urlsafe(24),
                username=username,
                created_at=now,
                last_active_at=now,
            )
            self._queues[username].put(session)
            self._active_session_map[session.session_id] = session
            return session

    def get_latest_session(self, username: str) -> SessionInfo | None:
        """获取指定用户最近活跃的 session。"""
        with self._lock:
            user_queue = self._queues.get(username)
            if not user_queue or user_queue.empty():
                return None

            latest = None
            sessions: list[SessionInfo] = []
            while not user_queue.empty():
                item = user_queue.get()
                sessions.append(item)
                if item.is_active:
                    latest = item
            for item in sessions:
                user_queue.put(item)

            if latest:
                latest.last_active_at = int(time.time())
            return latest

    def get_session(self, session_id: str) -> SessionInfo | None:
        """按 session_id 查询会话。"""
        with self._lock:
            session = self._active_session_map.get(session_id)
            if session and session.is_active:
                session.last_active_at = int(time.time())
                return session
            return None

    def deactivate_user_sessions(self, username: str) -> int:
        """将用户所有 session 标记为失效。"""
        with self._lock:
            user_queue = self._queues.get(username)
            if not user_queue:
                return 0
            affected = 0
            sessions: list[SessionInfo] = []
            while not user_queue.empty():
                item = user_queue.get()
                if item.is_active:
                    item.is_active = False
                    affected += 1
                self._active_session_map.pop(item.session_id, None)
                sessions.append(item)
            for item in sessions:
                user_queue.put(item)
            return affected

    def deactivate_session(self, session_id: str) -> bool:
        """将单个 session 置为失效。"""
        with self._lock:
            session = self._active_session_map.pop(session_id, None)
            if not session:
                return False
            session.is_active = False
            return True


class UserAPI:
    """用户 API 层能力封装。"""

    def __init__(self, user_manager: UserManager | None = None) -> None:
        self.user_manager = user_manager or default_user_manager
        self.session_pool = SessionPool()
        self._token_secret = os.getenv("USER_TOKEN_SECRET", "ohos-assistant-token-secret")
        self._token_ttl_seconds = int(os.getenv("USER_TOKEN_TTL_SECONDS", "7200"))
        self._token_lock = threading.RLock()
        self._rsa_private_key, self._rsa_public_key = SecurityEncryption.generate_rsa_key_pair()

    def register_user(self, username: str, password: str, role: int = UserManager.ROLE_VIEWER) -> bool:
        """注册用户。"""
        return self.user_manager.register(username=username, password=password, role=role)

    def login_user(self, username: str, password: str) -> dict[str, Any]:
        """
        用户登录。

        登录成功后返回：
        - token
        - session_id
        - user_info
        """
        success = self.user_manager.login(username=username, password=password)
        if not success:
            raise PermissionError("用户名或密码错误")

        user = self.user_manager.get_user_by_username(username)
        if not user:
            raise LookupError("用户不存在或已删除")

        session = self.session_pool.create_session(username)
        token = self._generate_token(
            {
                "user_id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "session_id": session.session_id,
            }
        )
        return {
            "token": token,
            "session_id": session.session_id,
            "user_info": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "is_active": user["is_active"],
            },
        }

    def logout_user(self, username: str, session_id: str | None = None) -> bool:
        """用户退出登录，支持退出指定 session 或用户全部 session。"""
        self.user_manager.logout(username)
        if session_id:
            return self.session_pool.deactivate_session(session_id)
        self.session_pool.deactivate_user_sessions(username)
        return True

    def update_password(self, username: str, old_password: str, new_password: str) -> bool:
        """修改密码。"""
        if not old_password or not new_password:
            raise ValueError("old_password 和 new_password 不能为空")
        if old_password == new_password:
            raise ValueError("新密码不能与旧密码相同")

        if not self.user_manager.login(username, old_password):
            raise PermissionError("旧密码验证失败")

        hashed_new = hashlib.sha256(new_password.encode("utf-8")).hexdigest()
        update_sql = f"""
        UPDATE {self.user_manager.TABLE_NAME}
        SET password = ?, update_time = ?
        WHERE username = ? AND is_deleted = 0
        """
        affected = self.user_manager.crud.write(
            update_sql, (hashed_new, int(time.time()), username)
        )
        if affected <= 0:
            return False

        self.session_pool.deactivate_user_sessions(username)
        return True

    def admin_batch_import_users(self, admin_username: str, users: list[dict[str, Any]]) -> int:
        """管理员批量导入用户，返回成功导入数量。"""
        admin = self.user_manager.get_user_by_username(admin_username)
        if not admin or admin["role"] != UserManager.ROLE_ADMIN:
            raise PermissionError("仅管理员可执行批量导入")

        success_count = 0
        for item in users:
            username = str(item.get("username", "")).strip()
            password = str(item.get("password", "")).strip()
            role = int(item.get("role", UserManager.ROLE_VIEWER))
            if not username or not password:
                logger.warning("跳过无效用户导入项: %s", item)
                continue
            if self.user_manager.register(username=username, password=password, role=role):
                success_count += 1
        return success_count

    def admin_batch_delete_users(self, admin_username: str, usernames: list[str]) -> int:
        """管理员批量删除用户。"""
        return self.user_manager.batch_delete(admin_username=admin_username, usernames=usernames)

    def encrypt_raw_password(self, raw_password: str) -> str:
        """使用 RSA 公钥对原始密码进行非对称加密。"""
        if not raw_password:
            raise ValueError("raw_password 不能为空")
        return SecurityEncryption.encrypt(
            plaintext=raw_password,
            algorithm="RSA",
            key=self._rsa_public_key,
        )

    def decrypt_encrypted_password(self, encrypted_password: str) -> str:
        """使用 RSA 私钥解密密码密文（仅服务端内部使用）。"""
        if not encrypted_password:
            raise ValueError("encrypted_password 不能为空")
        return SecurityEncryption.decrypt(
            ciphertext=encrypted_password,
            algorithm="RSA",
            key=self._rsa_private_key,
        )

    def get_public_key_pem(self) -> str:
        """获取 RSA 公钥（PEM），用于客户端加密。"""
        public_bytes = self._rsa_public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return public_bytes.decode("utf-8")

    def get_user_info_from_token(self, token: str) -> dict[str, Any]:
        """通过 token 获取用户信息，并校验 session 是否仍然有效。"""
        payload = self._verify_token(token)
        session_id = str(payload.get("session_id", ""))
        if not session_id:
            raise PermissionError("token 缺少 session 信息")

        session = self.session_pool.get_session(session_id)
        if not session:
            raise PermissionError("session 已失效")

        user = self.user_manager.get_user_by_username(session.username)
        if not user:
            raise LookupError("用户不存在或已删除")

        return {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "is_active": user["is_active"],
            "session_id": session.session_id,
        }

    def _generate_token(self, claims: dict[str, Any]) -> str:
        """生成 HMAC 签名 token。"""
        now = int(time.time())
        payload = {
            **claims,
            "iat": now,
            "exp": now + self._token_ttl_seconds,
            "nonce": secrets.token_hex(8),
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        body_b64 = base64.urlsafe_b64encode(body).decode("utf-8").rstrip("=")

        with self._token_lock:
            signature = hmac.new(
                self._token_secret.encode("utf-8"),
                body_b64.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        sig_b64 = base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
        return f"{body_b64}.{sig_b64}"

    def _verify_token(self, token: str) -> dict[str, Any]:
        """校验 token 签名与有效期。"""
        if not token or "." not in token:
            raise PermissionError("token 格式非法")

        body_b64, sig_b64 = token.rsplit(".", 1)
        expected_sig = hmac.new(
            self._token_secret.encode("utf-8"),
            body_b64.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")
        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            raise PermissionError("token 签名非法")

        padded = body_b64 + "=" * (-len(body_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
        exp = int(payload.get("exp", 0))
        if int(time.time()) > exp:
            raise PermissionError("token 已过期")
        return payload


default_user_api = UserAPI()