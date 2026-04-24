"""用户鉴权相关 FastAPI 路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from auth.user import UserManager
from auth.user_api import default_user_api

BASE_API_PREFIX = "/ohos/assistant"
user_router = APIRouter(prefix=f"{BASE_API_PREFIX}/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, description="用户名")
    password: str = Field(min_length=1, description="密码")
    role: int = Field(default=UserManager.ROLE_VIEWER, description="角色")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, description="用户名")
    password: str = Field(min_length=1, description="密码")


class LogoutRequest(BaseModel):
    username: str = Field(min_length=1, description="用户名")
    session_id: str | None = Field(default=None, description="可选: 指定退出的 session")


class UpdatePasswordRequest(BaseModel):
    username: str = Field(min_length=1, description="用户名")
    old_password: str = Field(min_length=1, description="旧密码")
    new_password: str = Field(min_length=1, description="新密码")


class BatchImportUserItem(BaseModel):
    username: str = Field(min_length=1, description="用户名")
    password: str = Field(min_length=1, description="密码")
    role: int = Field(default=UserManager.ROLE_VIEWER, description="角色")


class AdminBatchImportRequest(BaseModel):
    admin_username: str = Field(min_length=1, description="管理员用户名")
    users: list[BatchImportUserItem] = Field(default_factory=list, description="待导入用户列表")


class AdminBatchDeleteRequest(BaseModel):
    admin_username: str = Field(min_length=1, description="管理员用户名")
    usernames: list[str] = Field(default_factory=list, description="待删除用户名列表")


class EncryptPasswordRequest(BaseModel):
    raw_password: str = Field(min_length=1, description="原始密码")


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Authorization 请求头",
        )
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization 格式必须为 Bearer <token>",
        )
    return credentials.strip()


@user_router.post("/register")
def register_user(request: RegisterRequest) -> dict[str, Any]:
    try:
        success = default_user_api.register_user(
            username=request.username,
            password=request.password,
            role=request.role,
        )
        return {"success": success}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@user_router.post("/login")
def login_user(request: LoginRequest) -> dict[str, Any]:
    try:
        return default_user_api.login_user(username=request.username, password=request.password)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@user_router.post("/logout")
def logout_user(request: LogoutRequest) -> dict[str, Any]:
    try:
        success = default_user_api.logout_user(
            username=request.username,
            session_id=request.session_id,
        )
        return {"success": success}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@user_router.post("/password/update")
def update_password(request: UpdatePasswordRequest) -> dict[str, Any]:
    try:
        success = default_user_api.update_password(
            username=request.username,
            old_password=request.old_password,
            new_password=request.new_password,
        )
        return {"success": success}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@user_router.post("/admin/users/import")
def admin_batch_import_users(request: AdminBatchImportRequest) -> dict[str, Any]:
    try:
        imported = default_user_api.admin_batch_import_users(
            admin_username=request.admin_username,
            users=[item.model_dump() for item in request.users],
        )
        return {"imported_count": imported}
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@user_router.post("/admin/users/delete")
def admin_batch_delete_users(request: AdminBatchDeleteRequest) -> dict[str, Any]:
    try:
        deleted = default_user_api.admin_batch_delete_users(
            admin_username=request.admin_username,
            usernames=request.usernames,
        )
        return {"deleted_count": deleted}
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@user_router.post("/password/encrypt")
def encrypt_password(request: EncryptPasswordRequest) -> dict[str, str]:
    try:
        encrypted_password = default_user_api.encrypt_raw_password(request.raw_password)
        return {"encrypted_password": encrypted_password}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@user_router.get("/public-key")
def get_public_key() -> dict[str, str]:
    return {"public_key": default_user_api.get_public_key_pem()}


@user_router.get("/me")
def get_user_info_from_token(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    token = _extract_bearer_token(authorization)
    try:
        user_info = default_user_api.get_user_info_from_token(token)
        return {"user_info": user_info}
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
