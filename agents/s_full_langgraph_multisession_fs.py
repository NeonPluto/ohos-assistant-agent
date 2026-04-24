#!/usr/bin/env python3
"""
s_full_langgraph_multisession_fs.py

基于 LangGraph 的多会话 Agent（精简文件工具版）：
- 保留 skill 能力：仅当用户 `/invoke_skill` 或 WebUI 显式选择时由运行时将 SKILL 正文注入历史（SystemMessage）；普通对话不自动加载、模型侧不暴露 load_skill 工具
- Skill frontmatter `allowed-tools` 非空时，本轮向模型 `bind_tools` 并执行阶段强制白名单（仅 read_file / write_file / edit_file / list_files）
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
from typing import NotRequired, TypedDict

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
# Skill frontmatter `allowed-tools` 仅可限制下列名称（与 FILE_TOOLS 一致）
BINDABLE_FILE_TOOL_NAMES = frozenset({"read_file", "write_file", "edit_file", "list_files"})


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

        lines = target.read_text(encoding='utf-8').splitlines()
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
        content = fp.read_text(encoding='utf-8')
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

    # 首行显式启用：仅当用户消息以该格式开头时，由运行时代码注入 load_skill（模型无法伪造 trusted 标记）。
    EXPLICIT_INVOKE_LINE = re.compile(r"^\s*/invoke_skill\s+([^\n]+?)\s*(?:\n(.*)|$)", re.DOTALL)

    def __init__(self, skills_dir: Path):
        self.skills: dict[str, dict] = {}
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.rglob("SKILL.md")):
                text = skill_file.read_text(encoding='utf-8')
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    meta = self._parse_frontmatter(match.group(1))
                    body = match.group(2).strip()
                name = meta.get("name", skill_file.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    @staticmethod
    def _truthy(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return False

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

    def is_explicit_invoke_only(self, name: str) -> bool:
        skill = self.skills.get(name)
        if not skill:
            return False
        return self._truthy(skill["meta"].get("explicit_invoke_only"))

    def tool_allowlist_for_skill(self, name: str | None) -> list[str] | None:
        """Skill frontmatter `allowed-tools` 非空时返回可绑定工具名列表；否则 None 表示使用默认全套文件工具。"""
        if not name or name not in self.skills:
            return None
        raw = self.skills[name]["meta"].get("allowed-tools")
        if not isinstance(raw, list) or not raw:
            return None
        names: list[str] = []
        for item in raw:
            n = str(item).strip()
            if n and n in BINDABLE_FILE_TOOL_NAMES:
                names.append(n)
        return names or None

    def descriptions(self) -> str:
        """返回 skill 简要描述（不含 explicit_invoke_only，避免模型按描述自行 load_skill）。"""
        if not self.skills:
            return "(no skills)"
        lines = []
        for name, skill in self.skills.items():
            if self.is_explicit_invoke_only(name):
                continue
            lines.append(f"  - {name}: {skill['meta'].get('description', '-')}")
        if not lines:
            return "(no skills)"
        lines.append(
            "  - （另有仅支持显式启用的 Skill：首行写 `/invoke_skill <Skill 中文名>` 后换行写正文；"
            "此类 Skill 不在此列表中，亦勿尝试用 load_skill 猜测加载。）"
        )
        return "\n".join(lines)

    def explicit_invoke_skill_choices(self) -> list[str]:
        """供 WebUI 下拉：仅 explicit_invoke_only 的 skill 名称。"""
        return sorted(name for name in self.skills if self.is_explicit_invoke_only(name))

    def load(self, name: str) -> str:
        """按名称加载 skill 正文。"""
        skill = self.skills.get(name)
        if not skill:
            return (
                f"Skill「{name}」未注册。"
                "请使用消息首行 `/invoke_skill <名称>` 或界面「显式启用 Skill」选择已配置的 Skill。"
            )
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"

    def parse_explicit_invoke_prefix(self, user_text: str) -> tuple[str | None, str]:
        """解析首行 `/invoke_skill <name>`；返回 (skill_name 或 None, 剩余正文)。"""
        m = self.EXPLICIT_INVOKE_LINE.match(user_text)
        if not m:
            return None, user_text
        name = m.group(1).strip()
        rest = (m.group(2) or "").lstrip("\n")
        return name, rest


SKILLS = SkillLoader(SKILLS_DIR)


# =========================
# LangChain 工具定义
# =========================
@tool
def load_skill(name: str) -> str:
    """按 skill 名称加载知识内容。标记为 explicit_invoke_only 的 Skill 不可通过本工具加载（须由用户首行 /invoke_skill 或 WebUI 显式选择触发）。"""
    if SKILLS.is_explicit_invoke_only(name):
        return (
            f"Error: Skill「{name}」为显式启用专用，禁止通过 load_skill 加载。"
            "请让用户在消息首行使用 `/invoke_skill {name}` 后换行写问题，或在 WebUI 选择「显式启用 Skill」。"
        )
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
# 模型侧不暴露 load_skill：仅由运行时根据 `/invoke_skill` 或 WebUI 显式选择注入，避免误调虚构名称。
FILE_TOOLS = [read_file, write_file, edit_file, list_files]
TOOL_MAP = {t.name: t for t in ALL_TOOLS}
FILE_TOOL_BY_NAME = {t.name: t for t in FILE_TOOLS}


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
    "You are a specialist in HarmonyOS Next development. \n"
    "Use tools when needed. You can call: read_file / write_file / edit_file / list_files. "
    "Domain skills are not loaded automatically: if the user did not use `/invoke_skill` or the UI to pick a skill, answer directly from general knowledge and the workspace.\n"
    "Workspace browsing restraint: unless the user explicitly asks to browse folders, list directory contents, open, or read specific files/paths, do not call read_file or list_files to explore the repo on your own. Answer from context and general knowledge when file access was not requested; use read/list tools only when clearly aligned with the user's stated need.\n"
    "When presenting user-facing answer content, output complete Markdown sections and avoid JSON-shaped presentation unless the user explicitly asks for JSON.\n"
    "Optional reference — skills the user may enable explicitly (names for `/invoke_skill` only, do not invent tool calls to load them):\n"
    f"{SKILLS.descriptions()}"
)


class AgentState(TypedDict):
    messages: list[BaseMessage]
    tool_allowlist: NotRequired[list[str] | None]


def _tools_for_allowlist(allowlist: list[str] | None) -> list:
    if not allowlist:
        return FILE_TOOLS
    picked = [FILE_TOOL_BY_NAME[n] for n in allowlist if n in FILE_TOOL_BY_NAME]
    return picked if picked else FILE_TOOLS


def llm_call_node(state: AgentState) -> dict:
    """调用模型，让模型决定是否发起工具调用。"""
    messages = state["messages"]
    allowlist = state.get("tool_allowlist")
    tools = _tools_for_allowlist(allowlist)
    model = llm.bind_tools(tools)
    system_text = SYSTEM_PROMPT
    # 显式 Skill 常绑定 write_file：模型易只输出 Markdown、不发起工具调用，导致“展示正常但未落盘”。
    if allowlist and "write_file" in allowlist:
        system_text = (
            SYSTEM_PROMPT
            + "\n\n[Runtime] write_file is enabled for this turn. If the injected skill requires "
            "persisting JSON or other files under the workspace, you must call write_file with the "
            "correct paths and content before you finish. Text-only replies do not create files on disk."
        )
    response = model.invoke([SystemMessage(content=system_text)] + messages)
    out: dict = {"messages": messages + [response]}
    if allowlist is not None:
        out["tool_allowlist"] = allowlist
    return out


def tool_execute_node(state: AgentState) -> dict:
    """执行模型返回的工具调用并追加 ToolMessage。"""
    messages = list(state["messages"])
    last_msg = messages[-1]
    allowlist = state.get("tool_allowlist")
    trusted = isinstance(last_msg, AIMessage) and bool(
        last_msg.additional_kwargs.get("trusted_programmatic_skill")
    )

    for tc in last_msg.tool_calls:
        tool_name = tc["name"]
        tool_args = tc["args"]
        handler = TOOL_MAP.get(tool_name)
        try:
            if allowlist and tool_name in BINDABLE_FILE_TOOL_NAMES and tool_name not in allowlist:
                output = (
                    f"Error: 工具「{tool_name}」未包含在本轮 Skill 绑定的允许列表中。"
                    f"本轮仅允许：{', '.join(allowlist)}。"
                )
                messages.append(ToolMessage(content=output, tool_call_id=tc["id"]))
                continue
            if tool_name == "load_skill":
                name = (tool_args or {}).get("name", "")
                if SKILLS.is_explicit_invoke_only(name) and not trusted:
                    output = (
                        f"Error: Skill「{name}」为显式启用专用，已拒绝本次 load_skill。"
                        "请提示用户在消息首行写 `/invoke_skill {name}` 换行后再写问题，或使用界面上的显式 Skill 选项。"
                    )
                else:
                    output = SKILLS.load(name)
            elif handler:
                output = handler.invoke(tool_args)
            else:
                output = f"Unknown tool: {tool_name}"
        except Exception as e:
            output = f"Error: {e}"
        print(f"> {tool_name}: {str(output)[:200]}")
        messages.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))

    out: dict = {"messages": messages}
    if allowlist is not None:
        out["tool_allowlist"] = allowlist
    return out


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

# LangGraph 默认 recursion_limit 较小时，多轮「LLM→tools→LLM」可能被截断；显式 Skill 写盘需留足步数。
_AGENT_RECURSION_LIMIT = 64


def message_content_to_text(content) -> str:
    """统一解析 LangChain 消息的 content（字符串或多段 list）。"""
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


def _write_file_succeeded(messages: list[BaseMessage]) -> bool:
    """是否存在至少一次成功的 write_file（与 run_write 返回的 \"Wrote … bytes to …\" 一致）。"""
    for msg in messages:
        if isinstance(msg, ToolMessage):
            t = message_content_to_text(msg.content)
            if t.startswith("Wrote ") and " bytes to " in t:
                return True
    return False


def run_agent_turn(messages: list[BaseMessage], tool_allowlist: list[str] | None = None) -> list[BaseMessage]:
    """执行一轮会话，返回更新后的历史消息。

    tool_allowlist: 非空时仅向模型绑定并允许执行这些文件工具名（须为 read_file/write_file/edit_file/list_files 子集）。
    """
    cfg = {"recursion_limit": _AGENT_RECURSION_LIMIT}

    def _invoke(msgs: list[BaseMessage]) -> list[BaseMessage]:
        payload: AgentState = {"messages": msgs}
        if tool_allowlist:
            payload["tool_allowlist"] = tool_allowlist
        return list(agent_graph.invoke(payload, config=cfg)["messages"])

    out = _invoke(messages)
    # 首轮常见遗漏：仅输出 Markdown、未带 write_file；补救一轮避免「页面上有知识但磁盘无文件」。
    if (
        tool_allowlist
        and "write_file" in tool_allowlist
        and not _write_file_succeeded(out)
    ):
        out.append(
            SystemMessage(
                content=(
                    "[Runtime] 尚未检测到任何成功的 write_file（工具返回中应出现 "
                    "\"Wrote … bytes to …\"）。\n若注入的 Skill 要求将知识/图谱/索引落盘，"
                    "请立即按该 Skill 的路径与结构调用 write_file；不要只用正文描述已保存。\n"
                    "若 Skill 规定不得落盘（如知识不合法），请仅用一两句话说明原因，勿重复上方长 Markdown。"
                )
            )
        )
        out = _invoke(out)
    return out


def extract_final_response(messages: list[BaseMessage]) -> str:
    """从历史中提取最后一条对用户有意义的 AI 文本（含同轮「正文 + tool_calls」）。"""
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage) or not msg.content:
            continue
        text = message_content_to_text(msg.content).strip()
        if text:
            return text
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
        raw = json.loads(path.read_text(encoding='utf-8'))
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

    def run_query(self, query: str, ui_forced_skill: str | None = None) -> str:
        """在当前会话执行一轮用户输入并持久化。

        ui_forced_skill: WebUI 下拉显式指定的 skill 名；与首行 `/invoke_skill` 互斥时以首行为准。
        """
        parsed_name, rest = SKILLS.parse_explicit_invoke_prefix(query)
        pending: str | None = None
        if parsed_name:
            if parsed_name not in SKILLS.skills:
                body = (
                    f"/invoke_skill 指定的名称「{parsed_name}」不存在。"
                    f"已知名称：{', '.join(SKILLS.skills)}\n\n" + rest
                ).strip()
            else:
                pending = parsed_name
                body = rest.strip() if rest.strip() else "（请在此说明你的 HarmonyOS 相关问题。）"
        elif ui_forced_skill and ui_forced_skill.strip() in SKILLS.skills:
            pending = ui_forced_skill.strip()
            body = query.strip()
        else:
            body = query.strip()

        with self.lock:
            # 显式 Skill 须在「第一轮」模型调用前进入上下文：原先仅靠 pending_forced_skill
            # 注入合成 load_skill 工具轮次，首轮真正生成答案的 LLM 往往晚于工具结果，
            # 且部分环境下首跳状态未带上 pending，导致首条回复未按 SKILL 约束执行。
            # 此处直接把已加载正文写入 SystemMessage（与 trusted load_skill 同源），无需用户二次确认。
            skill_allow: list[str] | None = None
            if pending and pending in SKILLS.skills:
                skill_allow = SKILLS.tool_allowlist_for_skill(pending)
                bind_hint = ""
                if skill_allow:
                    bind_hint = (
                        "\n\n[Runtime 工具绑定] 本轮仅可调用以下工具（模型侧已限制，勿尝试其他工具）："
                        + ", ".join(skill_allow)
                        + "。"
                    )
                self.history.append(
                    SystemMessage(
                        content=(
                            "用户已通过首行 `/invoke_skill` 或界面「显式启用 Skill」指定垂域 Skill；"
                            "本轮回答须严格遵循其中流程与约束，不要追问用户是否启用。\n"
                            + SKILLS.load(pending)
                            + bind_hint
                        )
                    )
                )
            self.history.append(HumanMessage(content=body))
            self.history = run_agent_turn(self.history, tool_allowlist=skill_allow)
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
            "Tools: `read_file` / `write_file` / `edit_file` / `list_files` "
            "(垂域 Skill 由 `/invoke_skill` 或界面显式选择后由运行时注入)</sub>"
        )

        internal_state = gr.State(value=runtime.history)
        with gr.Row(equal_height=False):
            with gr.Column(scale=4):
                # 兼容较老版本 Gradio：不使用 type="messages" 参数。
                chat = gr.Chatbot(height=560)
                explicit_choices = ["（不使用）"] + SKILLS.explicit_invoke_skill_choices()
                force_skill_dd = gr.Dropdown(
                    label="显式启用 Skill（可选）",
                    choices=explicit_choices,
                    value=explicit_choices[0],
                    info="仅对 YAML 中 `explicit_invoke_only: true` 的 Skill 生效；与首行 `/invoke_skill 名称` 同时存在时以首行为准。",
                )
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

        def _escape_html(text: str) -> str:
            return (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

        def _render_tool_thinking(tool_calls: list[dict], tool_results: list[ToolMessage]) -> str:
            blocks: list[str] = []
            result_map = {tm.tool_call_id: message_content_to_text(tm.content) for tm in tool_results}
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
                    view.append({"role": "user", "content": message_content_to_text(msg.content)})
                    idx += 1
                    continue
                if isinstance(msg, AIMessage):
                    if msg.tool_calls:
                        j = idx + 1
                        tool_msgs: list[ToolMessage] = []
                        while j < len(messages) and isinstance(messages[j], ToolMessage):
                            tool_msgs.append(messages[j])
                            j += 1
                        # 同轮「正文 + tool_calls」时：主区只显示正文；工具参数与结果仅见「中间过程」。
                        text_part = message_content_to_text(msg.content).strip()
                        if text_part:
                            view.append({"role": "assistant", "content": text_part})
                        idx = j
                        continue
                    content = message_content_to_text(msg.content).strip()
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

        def send_message(text: str, forced_skill_choice: str):
            if not text.strip():
                snap = runtime.snapshot()
                with runtime.lock:
                    history = list(runtime.history)
                return "", _chat_view_from_messages(history), _thinking_view_from_messages(history), history, snap
            ui_skill = None
            if forced_skill_choice and forced_skill_choice != "（不使用）":
                ui_skill = forced_skill_choice
            reply = runtime.run_query(text.strip(), ui_forced_skill=ui_skill)
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
            inputs=[msg, force_skill_dd],
            outputs=[msg, chat, thinking_md, internal_state, state_json],
        )
        msg.submit(
            send_message,
            inputs=[msg, force_skill_dd],
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
