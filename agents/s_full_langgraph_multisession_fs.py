#!/usr/bin/env python3
"""
s_full_langgraph_multisession_fs.py

基于 LangGraph 的多会话 Agent（精简文件工具版）：
- 保留 skill 能力：load_skill
- 保留文件读写能力：read_file / write_file / edit_file
- 新增多 session 切换能力：按会话持久化历史，支持切换与新上下文重置
"""

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import TypedDict

import gradio as gr
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph

load_dotenv(override=True)


# =========================
# 全局配置
# =========================
WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
SESSIONS_DIR = WORKDIR / ".sessions"
MODEL = os.environ.get("MODEL_ID", "gpt-4o")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# =========================
# 基础能力：安全文件读写
# =========================
def safe_path(p: str) -> Path:
    """将相对路径解析为工作区内绝对路径，防止路径逃逸。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容，可选限制行数。"""
    try:
        target = safe_path(path)
        if target.is_dir():
            entries = sorted(target.iterdir(), key=lambda p: p.name)
            # 目录读取兜底：返回可用文件清单，避免 Is a directory 中断流程
            lines = [
                f"{e.name}{'/' if e.is_dir() else ''}"
                for e in entries
                if e.is_dir() or e.suffix.lower() == ".json"
            ]
            if not lines:
                lines = [f"(empty directory) {path}"]
            return "\n".join(lines)[:50000]

        lines = target.read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入文件，必要时自动创建父目录。"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在文件中替换一次文本。"""
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_list_files(path: str, suffix: str | None = None) -> str:
    """列出目录下所有文件，可选按后缀过滤。"""
    try:
        target = safe_path(path)
        if not target.exists():
            return f"Error: Path does not exist: {path}"
        if not target.is_dir():
            return f"Error: Not a directory: {path}"

        normalized_suffix = None
        if suffix:
            normalized_suffix = suffix.strip().lower()
            if normalized_suffix and not normalized_suffix.startswith("."):
                normalized_suffix = f".{normalized_suffix}"

        files = []
        for entry in sorted(target.rglob("*")):
            if not entry.is_file():
                continue
            if normalized_suffix and entry.suffix.lower() != normalized_suffix:
                continue
            files.append(entry.relative_to(target).as_posix())

        if not files:
            if normalized_suffix:
                return f"(no files found in {path} with suffix {normalized_suffix})"
            return f"(no files found in {path})"

        return "\n".join(files)[:50000]
    except Exception as e:
        return f"Error: {e}"


# =========================
# Skill 加载器
# =========================
class SkillLoader:
    """扫描 skills 目录中的 SKILL.md 并提供按名加载。"""

    def __init__(self, skills_dir: Path):
        self.skills: dict[str, dict] = {}
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.rglob("SKILL.md")):
                text = skill_file.read_text()
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    meta = self._parse_frontmatter(match.group(1))
                    body = match.group(2).strip()
                name = meta.get("name", skill_file.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    @staticmethod
    def _parse_frontmatter(text: str) -> dict:
        """解析简化 frontmatter，支持 key:value 和 triggers 列表。"""
        meta: dict[str, object] = {}
        current_list_key: str | None = None
        for raw in text.strip().splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- ") and current_list_key:
                meta.setdefault(current_list_key, [])
                casted = meta[current_list_key]
                if isinstance(casted, list):
                    casted.append(stripped[2:].strip())
                continue
            current_list_key = None
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            key = k.strip()
            value = v.strip()
            if value == "":
                meta[key] = []
                current_list_key = key
            else:
                meta[key] = value
        return meta

    def descriptions(self) -> str:
        """返回所有 skill 的简要描述，注入 system prompt。"""
        if not self.skills:
            return "(no skills)"
        return "\n".join(
            f"  - {name}: {skill['meta'].get('description', '-')}"
            for name, skill in self.skills.items()
        )

    def load(self, name: str) -> str:
        """按名称加载 skill 正文。"""
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"

    def find_triggered_skill(self, user_text: str) -> str | None:
        """按前缀匹配 triggers，返回命中的 skill 名称。"""
        query = user_text.strip()
        if not query:
            return None
        for name, skill in self.skills.items():
            triggers = skill["meta"].get("triggers", [])
            if isinstance(triggers, str):
                triggers = [triggers]
            if not isinstance(triggers, list):
                continue
            for trigger in triggers:
                if isinstance(trigger, str) and trigger and query.startswith(trigger):
                    return name
        return None


SKILLS = SkillLoader(SKILLS_DIR)


# =========================
# LangChain 工具定义
# =========================
@tool
def load_skill(name: str) -> str:
    """按 skill 名称加载知识内容。"""
    return SKILLS.load(name)


@tool
def read_file(path: str, limit: int | None = None) -> str:
    """读取文件内容，可选限制前 N 行。"""
    return run_read(path, limit)


@tool
def write_file(path: str, content: str) -> str:
    """写入文件内容。"""
    return run_write(path, content)


@tool
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """在文件里替换第一次出现的 old_text。"""
    return run_edit(path, old_text, new_text)


@tool
def list_files(path: str, suffix: str | None = None) -> str:
    """列出目录下全部文件路径；suffix 可选，例如 '.py' 或 'py'。"""
    return run_list_files(path, suffix)


ALL_TOOLS = [load_skill, read_file, write_file, edit_file, list_files]
TOOL_MAP = {t.name: t for t in ALL_TOOLS}


# =========================
# LangGraph 主循环
# =========================
llm = ChatOpenAI(
    model=MODEL,
    base_url=os.environ.get("OPENAI_BASE_URL"),
    api_key=os.environ.get("OPENAI_API_KEY", "sk-placeholder"),
    max_tokens=8000,
)

SYSTEM_PROMPT = (
    f"You are a coding agent at {WORKDIR}. "
    f"Use tools to complete tasks. You only have: load_skill/read_file/write_file/edit_file/list_files. "
    f"Prefer loading relevant skills before code changes.\n"
    "When presenting user-facing answer content, output complete Markdown sections and avoid JSON-shaped presentation unless the user explicitly asks for JSON.\n"
    f"Available skills:\n{SKILLS.descriptions()}"
)


class AgentState(TypedDict):
    messages: list[BaseMessage]


def llm_call_node(state: AgentState) -> dict:
    """调用模型，让模型决定是否发起工具调用。"""
    last_msg = state["messages"][-1] if state["messages"] else None
    if isinstance(last_msg, HumanMessage):
        triggered_skill = SKILLS.find_triggered_skill(str(last_msg.content))
        if triggered_skill:
            auto_call = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "load_skill",
                        "args": {"name": triggered_skill},
                        "id": f"auto_{uuid.uuid4().hex[:12]}",
                        "type": "tool_call",
                    }
                ],
            )
            return {"messages": state["messages"] + [auto_call]}

    model = llm.bind_tools(ALL_TOOLS)
    response = model.invoke([SystemMessage(content=SYSTEM_PROMPT)] + state["messages"])
    return {"messages": state["messages"] + [response]}


def tool_execute_node(state: AgentState) -> dict:
    """执行模型返回的工具调用并追加 ToolMessage。"""
    messages = list(state["messages"])
    last_msg = messages[-1]

    for tc in last_msg.tool_calls:
        tool_name = tc["name"]
        tool_args = tc["args"]
        handler = TOOL_MAP.get(tool_name)
        try:
            output = handler.invoke(tool_args) if handler else f"Unknown tool: {tool_name}"
        except Exception as e:
            output = f"Error: {e}"
        print(f"> {tool_name}: {str(output)[:200]}")
        messages.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))

    return {"messages": messages}


def should_continue(state: AgentState) -> str:
    """如果最后一条 AI 消息包含工具调用，则继续工具执行节点。"""
    last_msg = state["messages"][-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return "end"


graph_builder = StateGraph(AgentState)
graph_builder.add_node("llm_call", llm_call_node)
graph_builder.add_node("tool_execute", tool_execute_node)
graph_builder.set_entry_point("llm_call")
graph_builder.add_conditional_edges("llm_call", should_continue, {"tools": "tool_execute", "end": END})
graph_builder.add_edge("tool_execute", "llm_call")
agent_graph = graph_builder.compile()


def run_agent_turn(messages: list[BaseMessage]) -> list[BaseMessage]:
    """执行一轮会话，返回更新后的历史消息。"""
    result = agent_graph.invoke({"messages": messages})
    return result["messages"]


def extract_final_response(messages: list[BaseMessage]) -> str:
    """从历史中提取最后一条无工具调用的 AI 文本。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls and msg.content:
            return str(msg.content)
    return "(no response)"


# =========================
# 多会话存储与切换
# =========================
class SessionStore:
    """按 session_id 将消息历史持久化到 JSON 文件。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    def list_sessions_with_meta(self) -> list[dict]:
        """返回会话元信息，按最近更新时间倒序。"""
        items = []
        for p in self.root.glob("*.json"):
            items.append(
                {
                    "id": p.stem,
                    "updated_ts": p.stat().st_mtime,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
                }
            )
        return sorted(items, key=lambda x: x["updated_ts"], reverse=True)

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def create(self, session_id: str) -> None:
        if not self.exists(session_id):
            self.save(session_id, [])

    def load(self, session_id: str) -> list[BaseMessage]:
        path = self._path(session_id)
        if not path.exists():
            return []
        raw = json.loads(path.read_text())
        return [self._deserialize_message(item) for item in raw]

    def save(self, session_id: str, messages: list[BaseMessage]) -> None:
        raw = [self._serialize_message(m) for m in messages]
        self._path(session_id).write_text(json.dumps(raw, ensure_ascii=False, indent=2))

    @staticmethod
    def _serialize_message(msg: BaseMessage) -> dict:
        if isinstance(msg, HumanMessage):
            return {"type": "human", "content": msg.content}
        if isinstance(msg, ToolMessage):
            return {"type": "tool", "content": msg.content, "tool_call_id": msg.tool_call_id}
        if isinstance(msg, AIMessage):
            payload = {"type": "ai", "content": msg.content}
            if msg.tool_calls:
                payload["tool_calls"] = msg.tool_calls
            return payload
        if isinstance(msg, SystemMessage):
            return {"type": "system", "content": msg.content}
        return {"type": "human", "content": str(msg.content)}

    @staticmethod
    def _deserialize_message(item: dict) -> BaseMessage:
        msg_type = item.get("type")
        content = item.get("content", "")
        if msg_type == "human":
            return HumanMessage(content=content)
        if msg_type == "tool":
            return ToolMessage(content=content, tool_call_id=item.get("tool_call_id", ""))
        if msg_type == "ai":
            return AIMessage(content=content, tool_calls=item.get("tool_calls") or [])
        if msg_type == "system":
            return SystemMessage(content=content)
        return HumanMessage(content=content)


def validate_session_id(session_id: str) -> bool:
    """校验 session_id 字符范围，避免非法文件名。"""
    return bool(SESSION_ID_RE.match(session_id))


class AgentRuntime:
    """运行态容器：管理当前会话、切换逻辑与并发访问。"""

    def __init__(self, store: SessionStore):
        self.store = store
        self.current_session = "default"
        self.store.create(self.current_session)
        self.history = self.store.load(self.current_session)
        self.lock = threading.Lock()

    def list_sessions(self) -> list[str]:
        return self.store.list_sessions()

    def switch_session(self, session_id: str, new_context: bool = False) -> None:
        """切换会话：先保存当前，再加载目标；可选择清空目标上下文。"""
        if not validate_session_id(session_id):
            raise ValueError("session_id must match [A-Za-z0-9._-]+")
        with self.lock:
            self.store.save(self.current_session, self.history)
            self.store.create(session_id)
            if new_context:
                self.store.save(session_id, [])
            self.current_session = session_id
            self.history = self.store.load(session_id)

    def run_query(self, query: str) -> str:
        """在当前会话执行一轮用户输入并持久化。"""
        with self.lock:
            self.history.append(HumanMessage(content=query))
            self.history = run_agent_turn(self.history)
            self.store.save(self.current_session, self.history)
            return extract_final_response(self.history)

    def snapshot(self) -> dict:
        """返回当前会话状态给 WebUI 展示。"""
        with self.lock:
            return {
                "current_session": self.current_session,
                "sessions": self.list_sessions(),
                "session_meta": self.store.list_sessions_with_meta(),
                "history_size": len(self.history),
            }


# =========================
# Gradio WebUI
# =========================
def build_webui(runtime: AgentRuntime):
    """构建 WebUI，支持历史会话展示与新建对话。"""
    with gr.Blocks(
        title="s_full_langgraph_multisession_fs",
        theme=gr.themes.Soft(
            primary_hue="blue",
            neutral_hue="slate",
            font=gr.themes.GoogleFont("Inter"),
        ),
    ) as demo:
        gr.Markdown(
            "## 🤖 LangGraph Multi-Session FS Agent\n"
            f"<sub>Model: **{MODEL}** &nbsp;|&nbsp; Workspace: `{WORKDIR}` &nbsp;|&nbsp; "
            "Tools: `load_skill` / `read_file` / `write_file` / `edit_file` / `list_files`</sub>"
        )

        internal_state = gr.State(value=runtime.history)
        with gr.Row(equal_height=False):
            with gr.Column(scale=4):
                # 兼容较老版本 Gradio：不使用 type="messages" 参数。
                chat = gr.Chatbot(height=560)
                with gr.Row():
                    msg = gr.Textbox(
                        label="输入消息",
                        lines=2,
                        max_lines=5,
                        placeholder="请输入你的请求，回车发送，Shift+回车换行",
                        scale=6,
                    )
                    send_btn = gr.Button("发送", variant="primary", scale=1, min_width=90)

            with gr.Column(scale=1, min_width=300):
                with gr.Accordion("🧭 会话切换", open=True):
                    session_id_input = gr.Textbox(label="Session ID", value=runtime.current_session)
                    gen_uuid_btn = gr.Button("🎲 生成 UUID", variant="secondary")
                    with gr.Row():
                        switch_btn = gr.Button("切换/创建")
                        switch_new_btn = gr.Button("切换并重置")
                    with gr.Row():
                        new_btn = gr.Button("🆕 新建对话", variant="secondary")
                        refresh_btn = gr.Button("刷新列表")
                    session_dropdown = gr.Dropdown(
                        label="选择已有会话",
                        choices=runtime.list_sessions(),
                        value=runtime.current_session,
                    )
                    use_selected_btn = gr.Button("使用所选会话")

                with gr.Accordion("🕘 历史 Sessions", open=True):
                    session_history_md = gr.Markdown(value="(暂无会话历史)")

                with gr.Accordion("📊 当前状态", open=False):
                    state_json = gr.JSON(label="运行状态", value=runtime.snapshot())
                with gr.Accordion("🧠 中间过程（可展开）", open=False):
                    thinking_md = gr.Markdown(value="(暂无中间过程)")

        def _to_text(content) -> str:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                chunks: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        chunks.append(str(item.get("text", "")))
                    else:
                        chunks.append(str(item))
                return "\n".join(c for c in chunks if c).strip()
            return str(content)

        def _escape_html(text: str) -> str:
            return (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

        def _render_tool_thinking(tool_calls: list[dict], tool_results: list[ToolMessage]) -> str:
            blocks: list[str] = []
            result_map = {tm.tool_call_id: _to_text(tm.content) for tm in tool_results}
            for i, tc in enumerate(tool_calls, start=1):
                name = tc.get("name", "unknown_tool")
                args = tc.get("args", {})
                call_id = tc.get("id", "")
                result = result_map.get(call_id, "(no tool result)")
                args_text = _escape_html(json.dumps(args, ensure_ascii=False, indent=2))
                result_text = _escape_html(result)
                blocks.append(
                    f"### Thinking Step {i}: `{name}`\n\n"
                    f"```json\n{json.dumps(args, ensure_ascii=False, indent=2)}\n```\n\n"
                    f"```text\n{result}\n```"
                )
            return "\n".join(blocks)

        def _collect_thinking_blocks(messages: list[BaseMessage]) -> list[str]:
            blocks: list[str] = []
            idx = 0
            turn = 1
            while idx < len(messages):
                msg = messages[idx]
                if isinstance(msg, AIMessage) and msg.tool_calls:
                    j = idx + 1
                    tool_msgs: list[ToolMessage] = []
                    while j < len(messages) and isinstance(messages[j], ToolMessage):
                        tool_msgs.append(messages[j])
                        j += 1
                    block = _render_tool_thinking(msg.tool_calls, tool_msgs)
                    if block:
                        blocks.append(f"## 第 {turn} 轮\n\n{block}")
                        turn += 1
                    idx = j
                    continue
                idx += 1
            return blocks

        def _thinking_view_from_messages(messages: list[BaseMessage]) -> str:
            blocks = _collect_thinking_blocks(messages)
            if not blocks:
                return "(暂无中间过程)"
            return "\n\n---\n\n".join(blocks)

        def _chat_view_from_messages(messages: list[BaseMessage]) -> list[dict]:
            # 当前环境 Chatbot 期望 messages 格式：[{role, content}, ...]
            view: list[dict] = []
            idx = 0
            while idx < len(messages):
                msg = messages[idx]
                if isinstance(msg, HumanMessage):
                    view.append({"role": "user", "content": _to_text(msg.content)})
                    idx += 1
                    continue
                if isinstance(msg, AIMessage):
                    if msg.tool_calls:
                        j = idx + 1
                        tool_msgs: list[ToolMessage] = []
                        while j < len(messages) and isinstance(messages[j], ToolMessage):
                            tool_msgs.append(messages[j])
                            j += 1
                        thinking_block = _render_tool_thinking(msg.tool_calls, tool_msgs)
                        if thinking_block:
                            view.append({"role": "assistant", "content": thinking_block})
                        idx = j
                        continue
                    content = _to_text(msg.content).strip()
                    if content:
                        view.append({"role": "assistant", "content": content})
                idx += 1
            return view

        def _format_session_history(meta_list: list[dict], current_session: str) -> str:
            if not meta_list:
                return "(暂无会话历史)"
            lines = []
            for item in meta_list:
                marker = "👉 " if item["id"] == current_session else "- "
                lines.append(f"{marker}`{item['id']}`  \n  最近更新: {item['updated_at']}")
            return "\n\n".join(lines)

        def refresh_all():
            snap = runtime.snapshot()
            with runtime.lock:
                history = list(runtime.history)
            return (
                snap["current_session"],
                gr.Dropdown(choices=snap["sessions"], value=snap["current_session"]),
                _format_session_history(snap.get("session_meta", []), snap["current_session"]),
                _chat_view_from_messages(history),
                _thinking_view_from_messages(history),
                history,
                snap,
            )

        def switch_session(session_id: str, new_context: bool):
            runtime.switch_session(session_id, new_context=new_context)
            return refresh_all()

        def use_selected(session_id: str):
            runtime.switch_session(session_id, new_context=False)
            return refresh_all()

        def create_new_session():
            session_id = f"chat_{time.strftime('%Y%m%d_%H%M%S')}"
            runtime.switch_session(session_id, new_context=True)
            return refresh_all()

        def generate_session_and_switch():
            session_id = str(uuid.uuid4())
            runtime.switch_session(session_id, new_context=True)
            return refresh_all()

        def send_message(text: str):
            if not text.strip():
                snap = runtime.snapshot()
                with runtime.lock:
                    history = list(runtime.history)
                return "", _chat_view_from_messages(history), _thinking_view_from_messages(history), history, snap
            reply = runtime.run_query(text.strip())
            with runtime.lock:
                history = list(runtime.history)
            snap = runtime.snapshot()
            print(f"ASSISTANT[{snap['current_session']}]: {reply[:200]}")
            return "", _chat_view_from_messages(history), _thinking_view_from_messages(history), history, snap

        refresh_btn.click(
            refresh_all,
            inputs=[],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, thinking_md, internal_state, state_json],
        )
        gen_uuid_btn.click(
            generate_session_and_switch,
            inputs=[],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, thinking_md, internal_state, state_json],
        )
        switch_btn.click(
            lambda sid: switch_session(sid, False),
            inputs=[session_id_input],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, thinking_md, internal_state, state_json],
        )
        switch_new_btn.click(
            lambda sid: switch_session(sid, True),
            inputs=[session_id_input],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, thinking_md, internal_state, state_json],
        )
        use_selected_btn.click(
            use_selected,
            inputs=[session_dropdown],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, thinking_md, internal_state, state_json],
        )
        new_btn.click(
            create_new_session,
            inputs=[],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, thinking_md, internal_state, state_json],
        )
        send_btn.click(
            send_message,
            inputs=[msg],
            outputs=[msg, chat, thinking_md, internal_state, state_json],
        )
        msg.submit(
            send_message,
            inputs=[msg],
            outputs=[msg, chat, thinking_md, internal_state, state_json],
        )

        # 页面初次加载时自动填充会话列表和历史区域。
        demo.load(
            refresh_all,
            inputs=[],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, thinking_md, internal_state, state_json],
        )

    return demo


def main() -> None:
    runtime = AgentRuntime(SessionStore(SESSIONS_DIR))
    host = os.getenv("S_FULL_LG_FS_HOST", "127.0.0.1")
    port = int(os.getenv("S_FULL_LG_FS_PORT", "8771"))
    demo = build_webui(runtime)
    print(f"WebUI running at http://{host}:{port}")
    demo.launch(server_name=host, server_port=port, share=False)


if __name__ == "__main__":
    main()
