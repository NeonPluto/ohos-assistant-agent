"""
AI 对话框页面实现（Gradio）。

布局规则：
1) 页面左右分栏，左 1/5，右 4/5
2) 右侧包含：历史对话区、Skill 下拉框、输入发送区
3) 左侧包含：用户名问候、新建上下文、历史会话列表与删除
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import gradio as gr

DEFAULT_SKILLS = ["通用问答", "代码解释", "文档总结", "故障排查"]


def welcome_text(user_name: str) -> str:
    safe_name = (user_name or "用户").strip() or "用户"
    return f"### {safe_name}你好"


def _new_context(title: str | None = None) -> dict[str, Any]:
    context_id = str(uuid.uuid4())
    return {
        "id": context_id,
        "title": title or f"新对话-{time.strftime('%H:%M:%S')}",
        "messages": [],
    }


def _to_chatbot_messages(messages: list[dict[str, str]]) -> list[tuple[str, str]]:
    # 兼容旧版 Gradio Chatbot：使用 [(user, assistant), ...] 元组列表。
    pairs: list[tuple[str, str]] = []
    pending_user = ""
    for item in messages:
        role = item.get("role", "")
        content = item.get("content", "")
        if role == "user":
            if pending_user:
                pairs.append((pending_user, ""))
            pending_user = content
            continue
        if role == "assistant":
            pairs.append((pending_user, content))
            pending_user = ""
    if pending_user:
        pairs.append((pending_user, ""))
    return pairs


def _mock_llm_answer(user_text: str, skill_name: str) -> str:
    return (
        f"**Skill**: `{skill_name}`\n\n"
        f"我已收到你的问题：{user_text}\n\n"
        "下面是按 Markdown 渲染的示例回复：\n"
        "- 支持列表\n"
        "- 支持代码块\n\n"
        "```python\n"
        "def hello():\n"
        "    return 'hello from assistant'\n"
        "```"
    )


def build_chat_page(default_user_name: str = "用户") -> dict[str, gr.components.Component]:
    default_context = _new_context("默认对话")
    contexts_state = gr.State([default_context])
    active_context_id_state = gr.State(default_context["id"])

    with gr.Row(equal_height=False):
        with gr.Column(scale=1, min_width=260):
            user_welcome_md = gr.Markdown(welcome_text(default_user_name))
            new_context_btn = gr.Button("新建上下文", variant="secondary")
            context_selector = gr.Radio(
                label="历史会话",
                choices=[f"{default_context['title']} ({default_context['id'][:8]})"],
                value=f"{default_context['title']} ({default_context['id'][:8]})",
            )
            delete_context_btn = gr.Button("删除当前会话", variant="stop")

        with gr.Column(scale=4):
            # 思考区放在对话框上方：仅当本轮存在思考内容时显示。
            with gr.Group(visible=False) as thinking_box:
                with gr.Accordion("LLM 思考过程", open=True):
                    thinking_md = gr.Markdown("")
            chat_history = gr.Chatbot(
                label="历史对话",
                height=460,
                value=[],
            )

            skill_dropdown = gr.Dropdown(
                label="SKILL",
                choices=DEFAULT_SKILLS,
                value=DEFAULT_SKILLS[0],
            )

            with gr.Row():
                user_input = gr.Textbox(
                    label="输入",
                    lines=3,
                    max_lines=6,
                    placeholder="输入你的问题，回车发送，Shift+回车换行",
                    scale=8,
                )
                send_btn = gr.Button("发送", variant="primary", scale=1, min_width=100)

    def _context_choices(contexts: list[dict[str, Any]]) -> list[str]:
        return [f"{item['title']} ({item['id'][:8]})" for item in contexts]

    def _get_active_context(contexts: list[dict[str, Any]], active_id: str) -> dict[str, Any]:
        for item in contexts:
            if item["id"] == active_id:
                return item
        return contexts[0]

    def _switch_context(selected: str, contexts: list[dict[str, Any]]):
        if not contexts:
            contexts = [_new_context("默认对话")]
        selected_id = ""
        for item in contexts:
            marker = f"({item['id'][:8]})"
            if selected and selected.endswith(marker):
                selected_id = item["id"]
                break
        if not selected_id:
            selected_id = contexts[0]["id"]
        active = _get_active_context(contexts, selected_id)
        return selected_id, _to_chatbot_messages(active["messages"])

    def _create_context(contexts: list[dict[str, Any]]):
        contexts = list(contexts or [])
        contexts.insert(0, _new_context())
        selected = _context_choices(contexts)[0]
        return (
            contexts,
            contexts[0]["id"],
            gr.update(choices=_context_choices(contexts), value=selected),
            [],
            "",
            gr.update(visible=False),
        )

    def _delete_context(contexts: list[dict[str, Any]], active_id: str):
        contexts = [item for item in (contexts or []) if item["id"] != active_id]
        if not contexts:
            contexts = [_new_context("默认对话")]
        active = contexts[0]
        return (
            contexts,
            active["id"],
            gr.update(choices=_context_choices(contexts), value=_context_choices(contexts)[0]),
            _to_chatbot_messages(active["messages"]),
            "",
            gr.update(visible=False),
        )

    def _send_message_stream(
        text: str,
        skill_name: str,
        contexts: list[dict[str, Any]],
        active_id: str,
    ):
        content = (text or "").strip()
        if not content:
            yield (
                gr.update(),
                gr.update(),
                gr.update(),
                contexts,
                active_id,
                gr.update(visible=False),
            )
            return

        contexts = list(contexts or [])
        if not contexts:
            contexts = [_new_context("默认对话")]
            active_id = contexts[0]["id"]

        active = _get_active_context(contexts, active_id)
        active["messages"].append({"role": "user", "content": content})
        active["messages"].append({"role": "assistant", "content": ""})

        thinking_text = "\n".join(
            [
                "1. 解析用户输入与上下文",
                f"2. 使用 Skill：`{skill_name}`",
                "3. 组织 Markdown 结构化答案",
                "4. 流式返回结果",
            ]
        )
        # 有思考内容时显示思考区。
        yield (
            "",
            _to_chatbot_messages(active["messages"]),
            thinking_text,
            contexts,
            active_id,
            gr.update(visible=True),
        )

        final_answer = _mock_llm_answer(content, skill_name)
        chunks = final_answer.split(" ")
        partial = []
        # 通过逐段 yield 模拟 SSE 流式输出。
        for chunk in chunks:
            partial.append(chunk)
            active["messages"][-1]["content"] = " ".join(partial)
            yield (
                "",
                _to_chatbot_messages(active["messages"]),
                thinking_text,
                contexts,
                active_id,
                gr.update(visible=True),
            )
            time.sleep(0.03)

        # 回答完成后隐藏思考区，避免空状态占位。
        yield (
            "",
            _to_chatbot_messages(active["messages"]),
            "",
            contexts,
            active_id,
            gr.update(visible=False),
        )

    new_context_btn.click(
        fn=_create_context,
        inputs=[contexts_state],
        outputs=[
            contexts_state,
            active_context_id_state,
            context_selector,
            chat_history,
            thinking_md,
            thinking_box,
        ],
    )
    delete_context_btn.click(
        fn=_delete_context,
        inputs=[contexts_state, active_context_id_state],
        outputs=[
            contexts_state,
            active_context_id_state,
            context_selector,
            chat_history,
            thinking_md,
            thinking_box,
        ],
    )
    context_selector.change(
        fn=_switch_context,
        inputs=[context_selector, contexts_state],
        outputs=[active_context_id_state, chat_history],
    )

    send_btn.click(
        fn=_send_message_stream,
        inputs=[user_input, skill_dropdown, contexts_state, active_context_id_state],
        outputs=[user_input, chat_history, thinking_md, contexts_state, active_context_id_state, thinking_box],
    )
    user_input.submit(
        fn=_send_message_stream,
        inputs=[user_input, skill_dropdown, contexts_state, active_context_id_state],
        outputs=[user_input, chat_history, thinking_md, contexts_state, active_context_id_state, thinking_box],
    )

    return {
        "user_welcome_md": user_welcome_md,
        "chat_history": chat_history,
    }


def main() -> None:
    with gr.Blocks(title="OHOS Assistant Chat") as demo:
        gr.Markdown("## OHOS Assistant Chat")
        build_chat_page(default_user_name="用户")
    demo.launch()


if __name__ == "__main__":
    main()