"""
基础注册登录页面实现：
1. 对接 router 中的注册登录接口
2. 实现登录注册页面
3. 使用 cookie 存在浏览器本地（用于后续鉴权）
4. 自动加载同域名下 cookie 信息
"""

from __future__ import annotations

import json
import os
from typing import Any

import gradio as gr
import requests

from ui.chat_box import build_chat_page, welcome_text

API_BASE_URL = os.getenv("AUTH_API_BASE_URL", "http://127.0.0.1:8000")
AUTH_PREFIX = "/ohos/assistant/auth"
TOKEN_COOKIE_NAME = "ohos_auth_token"


def _api_url(path: str) -> str:
    return f"{API_BASE_URL.rstrip('/')}{AUTH_PREFIX}{path}"


def _handle_response(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = {"detail": response.text}
    return data


def register_action(username: str, password: str, confirm_password: str) -> str:
    username = (username or "").strip()
    password = password or ""
    confirm_password = confirm_password or ""

    if not username or not password:
        return "请输入用户名和密码"
    if password != confirm_password:
        return "两次输入的密码不一致"

    try:
        response = requests.post(
            _api_url("/register"),
            json={"username": username, "password": password},
            timeout=8,
        )
    except requests.RequestException as exc:
        return f"注册请求失败：{exc}"

    data = _handle_response(response)
    if response.ok and data.get("success") is True:
        return "注册成功，请切换到登录页完成登录"
    return f"注册失败：{data.get('detail', data)}"


def login_action(username: str, password: str) -> tuple[str, str, str]:
    username = (username or "").strip()
    password = password or ""

    if not username or not password:
        return "请输入用户名和密码", "", ""

    try:
        response = requests.post(
            _api_url("/login"),
            json={"username": username, "password": password},
            timeout=8,
        )
    except requests.RequestException as exc:
        return f"登录请求失败：{exc}", "", ""

    data = _handle_response(response)
    if not response.ok:
        return f"登录失败：{data.get('detail', data)}", "", ""

    token = str(data.get("token", ""))
    user_info = data.get("user_info", {})
    user_info_text = json.dumps(user_info, ensure_ascii=False, indent=2)
    if not token:
        return "登录失败：接口未返回 token", "", ""
    return "登录成功，正在写入浏览器 Cookie", token, user_info_text


def check_me_from_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return "当前没有可用登录态（Cookie 中未找到 token）"

    try:
        response = requests.get(
            _api_url("/me"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except requests.RequestException as exc:
        return f"鉴权校验失败：{exc}"

    data = _handle_response(response)
    if response.ok:
        user_info = data.get("user_info", {})
        return f"已自动加载 Cookie 登录态：\n{json.dumps(user_info, ensure_ascii=False, indent=2)}"
    return f"Cookie 登录态失效：{data.get('detail', data)}"


def _resolve_username(username: str, user_info_text: str) -> str:
    username = (username or "").strip()
    if username:
        return username
    try:
        payload = json.loads(user_info_text or "{}")
        value = str(payload.get("username", "")).strip()
        if value:
            return value
    except Exception:  # noqa: BLE001
        pass
    return "用户"


def _after_login_show_chat(
    token: str,
    username: str,
    user_info_text: str,
) -> tuple[Any, Any, str]:
    if not (token or "").strip():
        return gr.update(visible=True), gr.update(visible=False), gr.update()
    user_name = _resolve_username(username, user_info_text)
    return (
        gr.update(visible=False),
        gr.update(visible=True),
        welcome_text(user_name),
    )


with gr.Blocks(title="OHOS 助手 - 注册登录") as demo:
    gr.Markdown("## OHOS 助手注册 / 登录")
    gr.Markdown(f"接口地址：`{API_BASE_URL}{AUTH_PREFIX}`")

    token_state = gr.Textbox(label="token_state", visible=False)
    with gr.Column(visible=True) as auth_panel:
        with gr.Tab("登录"):
            login_username = gr.Textbox(label="用户名")
            login_password = gr.Textbox(label="密码", type="password")
            login_btn = gr.Button("登录", variant="primary")
            login_result = gr.Markdown()
            login_user_info = gr.Code(label="登录返回用户信息", language="json")
            cookie_sync_result = gr.Markdown()

        with gr.Tab("注册"):
            register_username = gr.Textbox(label="用户名")
            register_password = gr.Textbox(label="密码", type="password")
            register_confirm_password = gr.Textbox(label="确认密码", type="password")
            register_btn = gr.Button("注册", variant="primary")
            register_result = gr.Markdown()

        cookie_check_result = gr.Markdown(label="自动加载 Cookie 后的鉴权结果")

    with gr.Column(visible=False) as chat_panel:
        chat_components = build_chat_page(default_user_name="用户")

    register_btn.click(
        fn=register_action,
        inputs=[register_username, register_password, register_confirm_password],
        outputs=[register_result],
    )

    login_event = login_btn.click(
        fn=login_action,
        inputs=[login_username, login_password],
        outputs=[login_result, token_state, login_user_info],
    )

    login_event.then(
        fn=lambda token: "登录态已写入浏览器 Cookie" if token else "未写入 Cookie（token 为空）",
        inputs=[token_state],
        outputs=[cookie_sync_result],
        js=f"""
        (token) => {{
          if (token) {{
            document.cookie = "{TOKEN_COOKIE_NAME}=" + encodeURIComponent(token) + "; Path=/; Max-Age=604800; SameSite=Lax";
          }}
          return [token];
        }}
        """,
    )
    login_event.then(
        fn=_after_login_show_chat,
        inputs=[token_state, login_username, login_user_info],
        outputs=[auth_panel, chat_panel, chat_components["user_welcome_md"]],
    )

    demo.load(
        fn=check_me_from_token,
        inputs=[token_state],
        outputs=[cookie_check_result],
        js=f"""
        () => {{
          const cookie = document.cookie
            .split("; ")
            .find((item) => item.startsWith("{TOKEN_COOKIE_NAME}="));
          const token = cookie ? decodeURIComponent(cookie.split("=", 2)[1] || "") : "";
          return [token];
        }}
        """,
    )
    demo.load(
        fn=_after_login_show_chat,
        inputs=[token_state, login_username, login_user_info],
        outputs=[auth_panel, chat_panel, chat_components["user_welcome_md"]],
        js=f"""
        () => {{
          const cookie = document.cookie
            .split("; ")
            .find((item) => item.startsWith("{TOKEN_COOKIE_NAME}="));
          const token = cookie ? decodeURIComponent(cookie.split("=", 2)[1] || "") : "";
          return [token, "", ""];
        }}
        """,
    )


if __name__ == "__main__":
    demo.launch()