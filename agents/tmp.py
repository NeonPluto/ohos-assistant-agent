#!/usr/bin/env python3
"""
s_full_langgraph.py - Full Reference Agent (LangChain + LangGraph + OpenAI + Gradio WebUI)

按照 s_full.py 的实现逻辑，使用 LangChain + LangGraph 重写，支持 OpenAI 风格 API，
并通过 Gradio 提供 WebUI。

架构概览:
    ┌──────────────────────────────────────────────────────┐
    │                  LangGraph StateGraph                │
    │                                                      │
    │  ┌────────────┐   ┌──────────┐   ┌───────────────┐  │
    │  │ preprocess │──▶│ llm_call │──▶│should_continue│  │
    │  └────────────┘   └──────────┘   └───────┬───────┘  │
    │       ▲                              ┌────┴────┐     │
    │       │                         tools│         │end  │
    │       │                              ▼         ▼     │
    │  ┌────┴──────────┐              ┌────────┐   END     │
    │  │  tool_execute │◀─────────────│        │           │
    │  └───────────────┘              └────────┘           │
    └──────────────────────────────────────────────────────┘

    preprocess: 微压缩(s06) + 自动压缩(s06) + 后台通知(s08) + 收件箱(s09)
    llm_call:   使用 ChatOpenAI 调用 LLM，绑定所有工具
    tool_execute: 分发并执行工具调用，处理特殊工具(compress/TodoWrite/task)

用法:
    python s_full_langgraph.py              # 终端 REPL 模式
    python s_full_langgraph.py --web        # Gradio WebUI 模式

依赖: pip install langchain-openai langgraph gradio python-dotenv
"""

import json
import logging
import logging.handlers
import os
import re
import subprocess
import threading
import time
import uuid
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from queue import Queue
from typing import Any, Literal, TypedDict

import requests
from dotenv import load_dotenv
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

load_dotenv(override=True)

# ╔══════════════════════════════════════════════════════════════╗
# ║                      日志配置                                ║
# ║  API 对话记录到 .log 目录，按日期轮转，保留 30 天              ║
# ╚══════════════════════════════════════════════════════════════╝

LOG_DIR = Path.cwd() / ".log"
LOG_DIR.mkdir(exist_ok=True)

_api_logger = logging.getLogger("agent.api")
_api_logger.setLevel(logging.DEBUG)
_api_logger.propagate = False

_api_handler = logging.handlers.TimedRotatingFileHandler(
    LOG_DIR / "api.log", when="midnight", backupCount=30, encoding="utf-8",
)
_api_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
))
_api_logger.addHandler(_api_handler)


def _log_messages(tag: str, messages: list, extra: str = ""):
    """将一组消息序列化后写入日志。tag 标识调用来源 (main/subagent/teammate)。"""
    parts = [f"--- [{tag}] {extra} ---"]
    for m in messages:
        role = getattr(m, "type", "unknown")
        content = str(getattr(m, "content", ""))[:2000]
        tool_calls = getattr(m, "tool_calls", None)
        line = f"  [{role}] {content}"
        if tool_calls:
            tc_summary = ", ".join(
                f"{tc['name']}({json.dumps(tc['args'], ensure_ascii=False)[:200]})"
                for tc in tool_calls
            )
            line += f"  | tool_calls: {tc_summary}"
        parts.append(line)
    _api_logger.info("\n".join(parts))


def _log_response(tag: str, response, extra: str = ""):
    """记录单条 API 响应。"""
    content = str(getattr(response, "content", ""))[:2000]
    tool_calls = getattr(response, "tool_calls", None)
    tc_info = ""
    if tool_calls:
        tc_info = " | tool_calls: " + ", ".join(
            f"{tc['name']}({json.dumps(tc['args'], ensure_ascii=False)[:200]})"
            for tc in tool_calls
        )
    _api_logger.info(f"[{tag}] Response{' ' + extra if extra else ''}: {content}{tc_info}")


# ╔══════════════════════════════════════════════════════════════╗
# ║                        全局配置                              ║
# ╚══════════════════════════════════════════════════════════════╝

WORKDIR = Path.cwd()
MODEL = os.environ.get("MODEL_ID", "gpt-4o")

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

TOKEN_THRESHOLD = 100_000
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

VALID_MSG_TYPES = {
    "message", "broadcast", "shutdown_request",
    "shutdown_response", "plan_approval_response",
}

# 创建 OpenAI 风格的 LLM 实例 (兼容任何 OpenAI API 格式的服务)
llm = ChatOpenAI(
    model=MODEL,
    base_url=os.environ.get("OPENAI_BASE_URL"),
    api_key=os.environ.get("OPENAI_API_KEY", "sk-placeholder"),
    max_tokens=8000,
)


# ╔══════════════════════════════════════════════════════════════╗
# ║                     工具执行日志                              ║
# ╚══════════════════════════════════════════════════════════════╝

class ToolLogger:
    """收集工具执行记录，供 WebUI 的工具日志面板展示。"""

    def __init__(self):
        self.entries: list[str] = []
        self._lock = threading.Lock()

    def log(self, tool_name: str, output: str):
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            self.entries.append(f"[{ts}] {tool_name}: {output[:200]}")

    def get_log(self) -> str:
        with self._lock:
            return "\n".join(self.entries[-30:]) or "(no tool calls yet)"

    def clear(self):
        with self._lock:
            self.entries.clear()


TOOL_LOG = ToolLogger()


# ╔══════════════════════════════════════════════════════════════╗
# ║               基础工具函数 (bash / read / write / edit)       ║
# ╚══════════════════════════════════════════════════════════════╝

def safe_path(p: str) -> Path:
    """解析路径并确保不会逃逸出工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# ╔══════════════════════════════════════════════════════════════╗
# ║                  Web 工具底层实现                              ║
# ║  HTMLTextExtractor: 从 HTML 中提取纯文本                      ║
# ║  web_search_impl: 使用 DuckDuckGo 搜索                       ║
# ║  web_fetch_impl: 抓取网页并提取文本内容                        ║
# ╚══════════════════════════════════════════════════════════════╝

class HTMLTextExtractor(HTMLParser):
    """将 HTML 转为可读纯文本，跳过 script/style 等非内容标签。"""

    SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "svg", "iframe"}
    BLOCK_TAGS = {"p", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "tr", "td", "th", "blockquote", "pre", "section", "article"}

    def __init__(self):
        super().__init__()
        self._buf = StringIO()
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in self.BLOCK_TAGS:
            self._buf.write("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._buf.write(data)

    def get_text(self) -> str:
        text = self._buf.getvalue()
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()


def web_fetch_impl(url: str, max_chars: int = 50000) -> str:
    """抓取 URL 内容，HTML 页面自动提取纯文本。"""
    try:
        resp = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type:
            parser = HTMLTextExtractor()
            parser.feed(resp.text)
            return parser.get_text()[:max_chars]
        return resp.text[:max_chars]
    except Exception as e:
        return f"Fetch error: {e}"


def web_search_impl(query: str, max_results: int = 5) -> str:
    """通过 DuckDuckGo HTML 搜索并解析结果。无需额外依赖。"""
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            timeout=10,
        )
        resp.raise_for_status()
        # 解析 DuckDuckGo HTML 搜索结果
        links = re.findall(
            r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', resp.text, re.DOTALL
        )
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span)', resp.text, re.DOTALL
        )
        results = []
        for i, (url, title) in enumerate(links[:max_results]):
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            # DuckDuckGo 的链接经过重定向编码，尝试提取真实 URL
            real_url = url
            uddg = re.search(r"uddg=([^&]+)", url)
            if uddg:
                from urllib.parse import unquote
                real_url = unquote(uddg.group(1))
            results.append(f"[{i + 1}] {title_clean}\n    URL: {real_url}\n    {snippet}")
        return "\n\n".join(results) if results else "No results found."
    except Exception as e:
        return f"Search error: {e}"


# ╔══════════════════════════════════════════════════════════════╗
# ║                     TodoManager (s03)                        ║
# ║  短期任务清单，内存中维护，支持验证规则和进度渲染               ║
# ╚══════════════════════════════════════════════════════════════╝

class TodoManager:
    def __init__(self):
        self.items: list[dict] = []

    def update(self, items: list[dict]) -> str:
        validated, ip = [], 0
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            if not content:
                raise ValueError(f"Item {i}: content required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: invalid status '{status}'")
            if not af:
                raise ValueError(f"Item {i}: activeForm required")
            if status == "in_progress":
                ip += 1
            validated.append({"content": content, "status": status, "activeForm": af})
        if len(validated) > 20:
            raise ValueError("Max 20 todos")
        if ip > 1:
            raise ValueError("Only one in_progress allowed")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(
                item["status"], "[?]"
            )
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        return any(item.get("status") != "completed" for item in self.items)


# ╔══════════════════════════════════════════════════════════════╗
# ║                     SkillLoader (s05)                        ║
# ║  从 skills/ 目录加载 SKILL.md，解析 YAML front-matter         ║
# ╚══════════════════════════════════════════════════════════════╝

class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills: dict[str, dict] = {}
        if skills_dir.exists():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text()
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills)"
        return "\n".join(
            f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items()
        )

    def load(self, name: str) -> str:
        s = self.skills.get(name)
        if not s:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# ╔══════════════════════════════════════════════════════════════╗
# ║                   上下文压缩 (s06)                            ║
# ║  microcompact: 清除旧的工具返回结果，保留最近3条                ║
# ║  auto_compact: token 超阈值时，用 LLM 生成摘要并替换历史        ║
# ╚══════════════════════════════════════════════════════════════╝

def estimate_tokens(messages: list[BaseMessage]) -> int:
    return sum(len(str(m.content)) for m in messages) // 4


def microcompact(messages: list[BaseMessage]):
    """清除旧的 ToolMessage 内容（保留最近3条），减少 token 消耗。"""
    tool_indices = [
        i for i, m in enumerate(messages)
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and len(m.content) > 100
    ]
    if len(tool_indices) <= 3:
        return
    for idx in tool_indices[:-3]:
        old = messages[idx]
        messages[idx] = ToolMessage(content="[cleared]", tool_call_id=old.tool_call_id)


def auto_compact(messages: list[BaseMessage]) -> list[BaseMessage]:
    """将整个对话压缩为摘要，保存原始 transcript 到磁盘。"""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps({"role": msg.type, "content": str(msg.content)[:5000]}) + "\n")

    _api_logger.info(f"[compact] Compressing {len(messages)} messages, transcript={path}")
    conv_text = "\n".join(str(m.content)[:2000] for m in messages)[:80000]
    compact_req = [HumanMessage(content=f"Summarize this conversation for continuity:\n{conv_text}")]
    _log_messages("compact", compact_req, "compress request")
    resp = llm.invoke(compact_req)
    _log_response("compact", resp)
    summary = resp.content

    return [
        HumanMessage(content=f"[Compressed. Transcript: {path}]\n{summary}"),
        AIMessage(content="Understood. Continuing with summary context."),
    ]


# ╔══════════════════════════════════════════════════════════════╗
# ║                    TaskManager (s07)                         ║
# ║  基于文件的持久化任务管理，支持依赖关系和所有者                  ║
# ╚══════════════════════════════════════════════════════════════╝

class TaskManager:
    def __init__(self):
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists():
            raise ValueError(f"Task {tid} not found")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id(), "subject": subject, "description": description,
            "status": "pending", "owner": None, "blockedBy": [], "blocks": [],
        }
        self._save(task)
        return json.dumps(task, indent=2)

    def get(self, tid: int) -> str:
        return json.dumps(self._load(tid), indent=2)

    def update(self, tid: int, status: str | None = None,
               add_blocked_by: list | None = None, add_blocks: list | None = None) -> str:
        task = self._load(tid)
        if status:
            task["status"] = status
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
            if status == "deleted":
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"Task {tid} deleted"
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
        self._save(task)
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"Claimed task #{tid} for {owner}"


# ╔══════════════════════════════════════════════════════════════╗
# ║                  BackgroundManager (s08)                     ║
# ║  在后台线程中执行命令，通过通知队列报告完成状态                  ║
# ╚══════════════════════════════════════════════════════════════╝

class BackgroundManager:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.notifications: Queue = Queue()

    def run(self, command: str, timeout: int = 120) -> str:
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        return f"Background task {tid} started: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=timeout,
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": output or "(no output)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        self.notifications.put({
            "task_id": tid,
            "status": self.tasks[tid]["status"],
            "result": self.tasks[tid]["result"][:500],
        })

    def check(self, tid: str | None = None) -> str:
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result', '(running)')}" if t else f"Unknown: {tid}"
        return "\n".join(
            f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()
        ) or "No bg tasks."

    def drain(self) -> list[dict]:
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


# ╔══════════════════════════════════════════════════════════════╗
# ║                    MessageBus (s09)                          ║
# ║  基于文件的 Agent 间消息传递，支持点对点和广播                   ║
# ╚══════════════════════════════════════════════════════════════╝

class MessageBus:
    def __init__(self):
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict | None = None) -> str:
        msg = {"type": msg_type, "from": sender, "content": content, "timestamp": time.time()}
        if extra:
            msg.update(extra)
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list[dict]:
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists():
            return []
        msgs = [json.loads(line) for line in path.read_text().strip().splitlines() if line]
        path.write_text("")
        return msgs

    def broadcast(self, sender: str, content: str, names: list[str]) -> str:
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# ╔══════════════════════════════════════════════════════════════╗
# ║                 关停 / 计划审批协议 (s10)                      ║
# ╚══════════════════════════════════════════════════════════════╝

shutdown_requests: dict[str, dict] = {}
plan_requests: dict[str, dict] = {}


def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"Plan {req['status']} for '{req['from']}'"


# ╔══════════════════════════════════════════════════════════════╗
# ║                 TeammateManager (s09/s11)                    ║
# ║  管理自主队友：后台线程中运行，支持 idle 后自动认领任务           ║
# ║  队友的 agent loop 使用 ChatOpenAI 直接实现（简化版）           ║
# ╚══════════════════════════════════════════════════════════════╝

class TeammateManager:
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load_config()

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find(self, name: str) -> dict | None:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        return f"Spawned '{name}' (role: {role})"

    def _set_status(self, name: str, status: str):
        member = self._find(name)
        if member:
            member["status"] = status
            self._save_config()

    def _loop(self, name: str, role: str, prompt: str):
        """队友的 agent 主循环，使用 ChatOpenAI + bind_tools 实现工具调用。"""
        team_name = self.config["team_name"]
        sys_msg = SystemMessage(content=(
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle when done with current work. You may auto-claim tasks."
        ))

        # 队友可用的工具子集
        @tool
        def tm_bash(command: str) -> str:
            """Run a shell command."""
            return run_bash(command)

        @tool
        def tm_read_file(path: str) -> str:
            """Read file contents."""
            return run_read(path)

        @tool
        def tm_write_file(path: str, content: str) -> str:
            """Write content to file."""
            return run_write(path, content)

        @tool
        def tm_edit_file(path: str, old_text: str, new_text: str) -> str:
            """Replace exact text in file."""
            return run_edit(path, old_text, new_text)

        @tool
        def tm_send_message(to: str, content: str) -> str:
            """Send a message to another teammate or lead."""
            return self.bus.send(name, to, content)

        @tool
        def tm_idle() -> str:
            """Signal that current work is done, entering idle phase."""
            return "Entering idle phase."

        @tool
        def tm_claim_task(task_id: int) -> str:
            """Claim a task from the task board."""
            return self.task_mgr.claim(task_id, name)

        tm_tools = [tm_bash, tm_read_file, tm_write_file, tm_edit_file,
                     tm_send_message, tm_idle, tm_claim_task]
        tm_llm = llm.bind_tools(tm_tools)
        tm_tool_map = {t.name: t for t in tm_tools}

        messages: list[BaseMessage] = [HumanMessage(content=prompt)]
        _api_logger.info(f"[teammate:{name}] Started role={role}, prompt={prompt[:500]}")

        while True:
            # ── 工作阶段 ──
            for step in range(50):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        _api_logger.info(f"[teammate:{name}] Shutdown requested")
                        self._set_status(name, "shutdown")
                        return
                    messages.append(HumanMessage(content=json.dumps(msg)))
                try:
                    _log_messages(f"teammate:{name}", [sys_msg] + messages, f"step={step}")
                    response = tm_llm.invoke([sys_msg] + messages)
                    _log_response(f"teammate:{name}", response, f"step={step}")
                except Exception as e:
                    _api_logger.error(f"[teammate:{name}] LLM error: {e}")
                    self._set_status(name, "shutdown")
                    return

                messages.append(response)

                if not response.tool_calls:
                    break

                idle_requested = False
                for tc in response.tool_calls:
                    if tc["name"] == "tm_idle":
                        idle_requested = True
                        output = "Entering idle phase."
                    else:
                        handler = tm_tool_map.get(tc["name"])
                        try:
                            output = handler.invoke(tc["args"]) if handler else "Unknown tool"
                        except Exception as e:
                            output = f"Error: {e}"
                    _api_logger.info(
                        f"[teammate:{name}] Tool {tc['name']}"
                        f"({json.dumps(tc['args'], ensure_ascii=False)[:300]}) => {str(output)[:500]}"
                    )
                    print(f"  [{name}] {tc['name']}: {str(output)[:120]}")
                    messages.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))

                if idle_requested:
                    break

            # ── idle 阶段: 轮询消息和未认领任务 (s11: 自动认领) ──
            self._set_status(name, "idle")
            resume = False
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append(HumanMessage(content=json.dumps(msg)))
                    resume = True
                    break
                # 自动认领未分配且无阻塞的待办任务
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text())
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)
                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    if len(messages) <= 3:
                        messages.insert(0, HumanMessage(
                            content=f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"
                        ))
                        messages.insert(1, AIMessage(content=f"I am {name}. Continuing."))
                    messages.append(HumanMessage(
                        content=f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                                f"{task.get('description', '')}</auto-claimed>"
                    ))
                    messages.append(AIMessage(content=f"Claimed task #{task['id']}. Working on it."))
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list[str]:
        return [m["name"] for m in self.config["members"]]


# ╔══════════════════════════════════════════════════════════════╗
# ║                      全局实例                                ║
# ╚══════════════════════════════════════════════════════════════╝

TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)

SYSTEM_PROMPT = (
    f"You are a coding agent at {WORKDIR}. Use tools to solve tasks.\n"
    f"Prefer task_create/task_update/task_list for multi-step work. "
    f"Use todo_write for short checklists.\n"
    f"Use subagent for isolated exploration or work. Use load_skill for specialized knowledge.\n"
    f"Skills: {SKILLS.descriptions()}"
)


# ╔══════════════════════════════════════════════════════════════╗
# ║              子代理 Subagent (s04)                           ║
# ║  在隔离的 LLM 循环中执行子任务，返回摘要结果                    ║
# ╚══════════════════════════════════════════════════════════════╝

def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    """创建一个独立的子代理，使用 ChatOpenAI + bind_tools 运行自己的工具循环。"""

    @tool
    def sub_bash(command: str) -> str:
        """Run a shell command."""
        return run_bash(command)

    @tool
    def sub_read_file(path: str) -> str:
        """Read file contents."""
        return run_read(path)

    @tool
    def sub_write_file(path: str, content: str) -> str:
        """Write content to file."""
        return run_write(path, content)

    @tool
    def sub_edit_file(path: str, old_text: str, new_text: str) -> str:
        """Replace exact text in file."""
        return run_edit(path, old_text, new_text)

    sub_tools = [sub_bash, sub_read_file]
    if agent_type != "Explore":
        sub_tools += [sub_write_file, sub_edit_file]

    sub_llm = llm.bind_tools(sub_tools)
    sub_tool_map = {t.name: t for t in sub_tools}
    messages: list[BaseMessage] = [HumanMessage(content=prompt)]

    sub_id = str(uuid.uuid4())[:8]
    _api_logger.info(f"[subagent:{sub_id}] Started type={agent_type}, prompt={prompt[:500]}")

    response = None
    for step in range(30):
        _log_messages(f"subagent:{sub_id}", messages, f"step={step}")
        response = sub_llm.invoke(messages)
        _log_response(f"subagent:{sub_id}", response, f"step={step}")
        messages.append(response)

        if not response.tool_calls:
            break

        for tc in response.tool_calls:
            handler = sub_tool_map.get(tc["name"])
            try:
                output = handler.invoke(tc["args"]) if handler else "Unknown tool"
            except Exception as e:
                output = f"Error: {e}"
            _api_logger.info(
                f"[subagent:{sub_id}] Tool {tc['name']}"
                f"({json.dumps(tc['args'], ensure_ascii=False)[:300]}) => {str(output)[:500]}"
            )
            messages.append(ToolMessage(content=str(output)[:50000], tool_call_id=tc["id"]))

    _api_logger.info(f"[subagent:{sub_id}] Finished")
    if response and isinstance(response.content, str) and response.content:
        return response.content
    return "(subagent failed)"


# ╔══════════════════════════════════════════════════════════════╗
# ║            LangChain 工具定义 (@tool 装饰器)                  ║
# ║  使用 LangChain @tool 装饰器定义所有主代理工具                  ║
# ║  LangChain 自动生成 OpenAI function calling 所需的 JSON Schema ║
# ╚══════════════════════════════════════════════════════════════╝

@tool
def bash(command: str) -> str:
    """Run a shell command in the workspace directory."""
    return run_bash(command)


@tool
def read_file(path: str, limit: int | None = None) -> str:
    """Read file contents. Optionally limit to first N lines."""
    return run_read(path, limit)


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file (creates parent dirs if needed)."""
    return run_write(path, content)


@tool
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Replace the first occurrence of old_text with new_text in a file."""
    return run_edit(path, old_text, new_text)


class TodoItem(BaseModel):
    content: str = Field(description="Todo item description")
    status: Literal["pending", "in_progress", "completed"] = Field(description="Item status")
    activeForm: str = Field(description="Current active form/step of work")


@tool
def todo_write(items: list[TodoItem]) -> str:
    """Update task tracking checklist. Max 20 items, only one in_progress at a time."""
    return TODO.update([item.model_dump() for item in items])


@tool
def subagent(prompt: str, agent_type: str = "Explore") -> str:
    """Spawn an isolated subagent for exploration or work. Returns a summary when done.
    agent_type: 'Explore' (read-only) or 'general-purpose' (can write files)."""
    return run_subagent(prompt, agent_type)


@tool
def load_skill(name: str) -> str:
    """Load specialized knowledge by skill name."""
    return SKILLS.load(name)


@tool
def compress() -> str:
    """Manually trigger conversation context compression."""
    return "__COMPRESS_SIGNAL__"


@tool
def background_run(command: str, timeout: int = 120) -> str:
    """Run a shell command in a background thread. Returns task ID for later checking."""
    return BG.run(command, timeout)


@tool
def check_background(task_id: str | None = None) -> str:
    """Check status of background task(s). Omit task_id to list all."""
    return BG.check(task_id)


@tool
def task_create(subject: str, description: str = "") -> str:
    """Create a new persistent file-based task."""
    return TASK_MGR.create(subject, description)


@tool
def task_get(task_id: int) -> str:
    """Get details of a task by its ID."""
    return TASK_MGR.get(task_id)


@tool
def task_update(task_id: int, status: str | None = None,
                add_blocked_by: list[int] | None = None,
                add_blocks: list[int] | None = None) -> str:
    """Update a task's status or dependency relationships."""
    return TASK_MGR.update(task_id, status, add_blocked_by, add_blocks)


@tool
def task_list() -> str:
    """List all tasks with their status, owner, and dependencies."""
    return TASK_MGR.list_all()


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """Spawn a persistent autonomous teammate that runs in a background thread."""
    return TEAM.spawn(name, role, prompt)


@tool
def list_teammates() -> str:
    """List all teammates and their current status."""
    return TEAM.list_all()


@tool
def send_message(to: str, content: str, msg_type: str = "message") -> str:
    """Send a message to a teammate. msg_type: message, broadcast, shutdown_request, etc."""
    return BUS.send("lead", to, content, msg_type)


@tool
def read_inbox() -> str:
    """Read and drain the lead agent's inbox."""
    msgs = BUS.read_inbox("lead")
    return json.dumps(msgs, indent=2) if msgs else "Inbox empty."


@tool
def broadcast(content: str) -> str:
    """Send a message to all active teammates."""
    return BUS.broadcast("lead", content, TEAM.member_names())


@tool
def shutdown_request(teammate: str) -> str:
    """Request a teammate to gracefully shut down."""
    return handle_shutdown_request(teammate)


@tool
def plan_approval(request_id: str, approve: bool, feedback: str = "") -> str:
    """Approve or reject a teammate's submitted plan."""
    return handle_plan_review(request_id, approve, feedback)


@tool
def idle() -> str:
    """Enter idle state (only used by teammates, not the lead agent)."""
    return "Lead does not idle."


@tool
def claim_task(task_id: int) -> str:
    """Claim a task from the task board for the lead agent."""
    return TASK_MGR.claim(task_id, "lead")


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo. Returns titles, URLs, and snippets."""
    return web_search_impl(query, max_results)


@tool
def web_fetch(url: str) -> str:
    """Fetch a web page and extract its text content. Automatically strips HTML tags."""
    return web_fetch_impl(url)


# 所有工具列表 —— 传给 ChatOpenAI.bind_tools()
ALL_TOOLS = [
    bash, read_file, write_file, edit_file, todo_write,
    subagent, load_skill, compress,
    background_run, check_background,
    task_create, task_get, task_update, task_list,
    spawn_teammate, list_teammates, send_message, read_inbox,
    broadcast, shutdown_request, plan_approval, idle, claim_task,
    web_search, web_fetch,
]

# ALL_TOOLS = [
#     read_file, load_skill
# ]

# 工具名 -> 工具对象的映射，用于 tool_execute 节点中的分发
TOOL_MAP = {t.name: t for t in ALL_TOOLS}


# ╔══════════════════════════════════════════════════════════════╗
# ║           LangGraph 状态定义 & 图构建                        ║
# ║                                                              ║
# ║  StateGraph 节点:                                            ║
# ║    preprocess   → 压缩 + 后台通知 + 收件箱                    ║
# ║    llm_call     → 调用 ChatOpenAI (已绑定工具)                ║
# ║    tool_execute → 分发执行工具调用                             ║
# ║                                                              ║
# ║  条件边:                                                      ║
# ║    llm_call → should_continue → "tools" | "end"             ║
# ╚══════════════════════════════════════════════════════════════╝

class AgentState(TypedDict):
    messages: list[BaseMessage]
    rounds_without_todo: int


def preprocess_node(state: AgentState) -> dict:
    """每次 LLM 调用前的预处理流水线:
    1. microcompact: 清除旧的工具结果
    2. auto_compact: token 超过阈值则自动压缩
    3. drain: 收集后台任务完成通知
    4. inbox: 读取 lead 的收件箱消息
    """
    messages = list(state["messages"])

    microcompact(messages)

    if estimate_tokens(messages) > TOKEN_THRESHOLD:
        print("[auto-compact triggered]")
        messages = auto_compact(messages)

    notifs = BG.drain()
    if notifs:
        txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
        messages.append(HumanMessage(content=f"<background-results>\n{txt}\n</background-results>"))
        messages.append(AIMessage(content="Noted background results."))

    inbox = BUS.read_inbox("lead")
    if inbox:
        messages.append(HumanMessage(content=f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"))
        messages.append(AIMessage(content="Noted inbox messages."))

    return {"messages": messages}


def llm_call_node(state: AgentState) -> dict:
    """调用绑定了所有工具的 ChatOpenAI 模型。"""
    llm_with_tools = llm.bind_tools(ALL_TOOLS)
    full_messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
    _log_messages("main", full_messages, "LLM request")
    response = llm_with_tools.invoke(full_messages)
    _log_response("main", response)
    return {"messages": state["messages"] + [response]}


def tool_execute_node(state: AgentState) -> dict:
    """执行 LLM 返回的所有工具调用:
    - 普通工具: 直接通过 TOOL_MAP 分发
    - compress: 触发手动压缩
    - todo_write: 标记已使用 todo（用于提醒机制）
    """
    messages = list(state["messages"])
    last_msg = messages[-1]

    used_todo = False
    compress_requested = False

    for tc in last_msg.tool_calls:
        tool_name = tc["name"]
        tool_args = tc["args"]

        if tool_name == "compress":
            compress_requested = True
            output = "Compressing conversation..."
        else:
            handler = TOOL_MAP.get(tool_name)
            try:
                output = handler.invoke(tool_args) if handler else f"Unknown tool: {tool_name}"
            except Exception as e:
                output = f"Error: {e}"

        if tool_name == "todo_write":
            used_todo = True

        print(f"> {tool_name}: {str(output)[:200]}")
        TOOL_LOG.log(tool_name, str(output))
        _api_logger.info(
            f"[main] Tool {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:300]}) "
            f"=> {str(output)[:500]}"
        )
        messages.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))

    # s03: todo 提醒 —— 如果有未完成的 todo 且连续3轮没有更新 todo，注入提醒
    rounds = state.get("rounds_without_todo", 0)
    rounds = 0 if used_todo else rounds + 1

    if TODO.has_open_items() and rounds >= 3:
        messages.append(HumanMessage(content="<reminder>Update your todos.</reminder>"))

    # s06: 手动压缩
    if compress_requested:
        print("[manual compact]")
        messages = auto_compact(messages)

    return {"messages": messages, "rounds_without_todo": rounds}


def should_continue(state: AgentState) -> str:
    """判断是否继续工具循环: 如果最后一条消息有 tool_calls 则继续，否则结束。"""
    last_msg = state["messages"][-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return "end"


# 构建 LangGraph 图
graph_builder = StateGraph(AgentState)
graph_builder.add_node("preprocess", preprocess_node)
graph_builder.add_node("llm_call", llm_call_node)
graph_builder.add_node("tool_execute", tool_execute_node)

graph_builder.set_entry_point("preprocess")
graph_builder.add_edge("preprocess", "llm_call")
graph_builder.add_conditional_edges("llm_call", should_continue, {"tools": "tool_execute", "end": END})
graph_builder.add_edge("tool_execute", "preprocess")  # 工具执行完后回到预处理，形成循环

agent_graph = graph_builder.compile()


# ╔══════════════════════════════════════════════════════════════╗
# ║                    执行入口函数                               ║
# ╚══════════════════════════════════════════════════════════════╝

def run_agent(messages: list[BaseMessage]) -> list[BaseMessage]:
    """执行一轮完整的 agent 交互（阻塞式），返回更新后的消息列表。"""
    result = agent_graph.invoke({
        "messages": messages,
        "rounds_without_todo": 0,
    })
    return result["messages"]


def extract_final_response(messages: list[BaseMessage]) -> str:
    """从消息列表末尾提取最终的 AI 文本回复。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls and msg.content:
            return msg.content
    return "(no response)"


# ╔══════════════════════════════════════════════════════════════╗
# ║                 Chatbot 展示格式化                            ║
# ║  将工具调用过程格式化为 Markdown，在聊天界面中渐进展示            ║
# ╚══════════════════════════════════════════════════════════════╝

def _format_tool_steps(steps: list[dict], status: str = "running") -> str:
    """将工具调用步骤格式化为 Markdown。

    steps: [{"name": "bash", "args": "...", "result": "..." or None}, ...]
    status: "running" | "done"
    """
    if not steps:
        return ""

    icon = {"running": "⏳", "done": "✅"}.get(status, "🔧")
    header = {"running": "Working...", "done": f"Used {len(steps)} tool(s)"}.get(status, "")

    lines = []
    for s in steps:
        name = s["name"]
        args_preview = s.get("args", "")
        if len(args_preview) > 120:
            args_preview = args_preview[:120] + "…"

        if s.get("result") is not None:
            result_preview = s["result"]
            if len(result_preview) > 150:
                result_preview = result_preview[:150] + "…"
            lines.append(f"  ✅ **{name}**(`{args_preview}`)\n  → `{result_preview}`")
        else:
            lines.append(f"  ⏳ **{name}**(`{args_preview}`) …")

    detail_body = "\n".join(lines)

    if status == "done":
        return (
            f"<details>\n<summary>{icon} {header}</summary>\n\n"
            f"{detail_body}\n\n</details>\n\n"
        )
    return f"**{icon} {header}**\n\n{detail_body}\n\n"


# ╔══════════════════════════════════════════════════════════════╗
# ║                  Gradio WebUI (Chatbot)                      ║
# ║                                                              ║
# ║  核心体验:                                                    ║
# ║  1. 流式响应: 通过 agent_graph.stream() 渐进展示工具调用过程    ║
# ║  2. 工具可视化: 工具调用以可折叠卡片展示在聊天中                 ║
# ║  3. 状态面板: 右侧实时显示 Tasks/Team/Todos/ToolLog           ║
# ║  4. 斜杠命令: /compact /tasks /team /inbox                   ║
# ╚══════════════════════════════════════════════════════════════╝

CUSTOM_CSS = """
/* 整体页面 */
.gradio-container { max-width: 1400px !important; }

/* 聊天气泡 */
.chatbot .message { font-size: 15px; line-height: 1.6; }
.chatbot .user { background: #e3f2fd !important; }
.chatbot .bot  { background: #f5f5f5 !important; }
.dark .chatbot .user { background: #1a3a5c !important; }
.dark .chatbot .bot  { background: #2d2d2d !important; }

/* details 折叠块 */
.chatbot details { background: rgba(0,0,0,0.03); border-radius: 8px;
                   padding: 8px 12px; margin-bottom: 8px; }
.chatbot details summary { cursor: pointer; font-weight: 600; }

/* 状态面板 */
.status-panel textarea { font-family: 'SF Mono', 'Fira Code', monospace !important;
                         font-size: 12px !important; }

/* 快捷按钮 */
.cmd-btn { min-width: 80px !important; }

/* 输入框 */
.input-row { gap: 8px !important; }
"""


def build_webui():
    """构建流式 Chatbot WebUI。"""
    import gradio as gr

    with gr.Blocks(
        title="Agent Chatbot",
        theme=gr.themes.Soft(
            primary_hue="blue",
            neutral_hue="slate",
            font=gr.themes.GoogleFont("Inter"),
        ),
        css=CUSTOM_CSS,
    ) as demo:

        # ── Header ──
        gr.Markdown(
            "## 🤖 Agent Chatbot\n"
            f"<sub>Model: **{MODEL}** &nbsp;|&nbsp; Workspace: `{WORKDIR}` &nbsp;|&nbsp; "
            f"Tools: {len(ALL_TOOLS)} (incl. web_search, web_fetch)</sub>"
        )

        # 内部状态: 保存完整的 LangChain 消息历史 (包括工具调用/结果)
        # Gradio 的 chatbot 组件只展示 user/assistant 文本，
        # 而 internal_state 保留了所有中间消息供下一轮使用。
        internal_state = gr.State(value=[])

        with gr.Row(equal_height=False):
            # ════════════════ 左侧: 聊天主区域 ════════════════
            with gr.Column(scale=4):
                chatbot = gr.Chatbot(
                    height=560,
                    # type="messages",
                    # show_copy_button=True,
                    avatar_images=(None, None),
                    placeholder=(
                        "<h3 style='text-align:center;opacity:0.5'>Send a message to start</h3>"
                        "<p style='text-align:center;opacity:0.3'>supports web search, "
                        "file operations, background tasks, team management</p>"
                    ),
                    render_markdown=True,
                )

                with gr.Row(elem_classes="input-row"):
                    msg_input = gr.Textbox(
                        placeholder="Ask anything … (Enter to send, Shift+Enter for newline)",
                        show_label=False,
                        scale=6,
                        container=False,
                        lines=1,
                        max_lines=5,
                    )
                    send_btn = gr.Button("Send", variant="primary", scale=1, min_width=80)

                with gr.Row():
                    compact_btn = gr.Button("⚡ /compact", size="sm", elem_classes="cmd-btn")
                    tasks_btn = gr.Button("📋 /tasks", size="sm", elem_classes="cmd-btn")
                    team_btn = gr.Button("👥 /team", size="sm", elem_classes="cmd-btn")
                    inbox_btn = gr.Button("📨 /inbox", size="sm", elem_classes="cmd-btn")
                    clear_btn = gr.Button("🗑 Clear", size="sm", variant="stop", elem_classes="cmd-btn")

            # ════════════════ 右侧: 状态面板 ════════════════
            with gr.Column(scale=1, min_width=260):
                with gr.Accordion("📋 Tasks", open=True):
                    tasks_display = gr.Textbox(
                        lines=6, interactive=False, show_label=False,
                        elem_classes="status-panel", value=TASK_MGR.list_all(),
                    )
                with gr.Accordion("👥 Team", open=True):
                    team_display = gr.Textbox(
                        lines=4, interactive=False, show_label=False,
                        elem_classes="status-panel", value=TEAM.list_all(),
                    )
                with gr.Accordion("✅ Todos", open=True):
                    todo_display = gr.Textbox(
                        lines=4, interactive=False, show_label=False,
                        elem_classes="status-panel", value=TODO.render(),
                    )
                with gr.Accordion("🔧 Tool Log", open=False):
                    tool_log_display = gr.Textbox(
                        lines=10, interactive=False, show_label=False,
                        elem_classes="status-panel", value=TOOL_LOG.get_log(),
                    )

        # ── 辅助函数：刷新右侧面板 ──
        def _panels():
            return TASK_MGR.list_all(), TEAM.list_all(), TODO.render(), TOOL_LOG.get_log()

        # ════════════════════════════════════════════════════
        # 核心：流式聊天响应 (generator)
        # 使用 agent_graph.stream(stream_mode="values") 实现
        # 每个 yield 即时刷新 chatbot + 右侧面板
        # ════════════════════════════════════════════════════

        def respond_stream(message: str, chat_history: list, internal_msgs: list):
            """流式处理用户消息，渐进式展示工具调用过程和最终回复。"""
            if not message.strip():
                yield "", chat_history, internal_msgs, *_panels()
                return

            # ── 斜杠命令快捷处理 ──
            cmd = message.strip()
            if cmd == "/compact" and internal_msgs:
                internal_msgs = auto_compact(internal_msgs)
                chat_history = list(chat_history)
                chat_history.append({"role": "assistant", "content": "⚡ Conversation compacted."})
                yield "", chat_history, internal_msgs, *_panels()
                return
            if cmd == "/tasks":
                chat_history = list(chat_history)
                chat_history.append({"role": "user", "content": cmd})
                chat_history.append({"role": "assistant", "content": f"```\n{TASK_MGR.list_all()}\n```"})
                yield "", chat_history, internal_msgs, *_panels()
                return
            if cmd == "/team":
                chat_history = list(chat_history)
                chat_history.append({"role": "user", "content": cmd})
                chat_history.append({"role": "assistant", "content": f"```\n{TEAM.list_all()}\n```"})
                yield "", chat_history, internal_msgs, *_panels()
                return
            if cmd == "/inbox":
                msgs = BUS.read_inbox("lead")
                result = json.dumps(msgs, indent=2) if msgs else "Inbox empty."
                chat_history = list(chat_history)
                chat_history.append({"role": "user", "content": cmd})
                chat_history.append({"role": "assistant", "content": f"```json\n{result}\n```"})
                yield "", chat_history, internal_msgs, *_panels()
                return

            # ── 正常消息：流式处理 ──
            _api_logger.info(f"[webui] User: {message[:1000]}")
            internal_msgs = list(internal_msgs)
            internal_msgs.append(HumanMessage(content=message))

            chat_history = list(chat_history)
            chat_history.append({"role": "user", "content": message})
            # 占位 assistant 消息，后续渐进更新
            chat_history.append({"role": "assistant", "content": "⏳ Thinking..."})
            yield "", chat_history, internal_msgs, *_panels()

            tool_steps: list[dict] = []  # 记录本轮所有工具调用
            final_messages = internal_msgs
            prev_msg_count = len(internal_msgs)

            try:
                for snapshot in agent_graph.stream(
                    {"messages": internal_msgs, "rounds_without_todo": 0},
                    stream_mode="values",
                ):
                    current_msgs = snapshot["messages"]
                    final_messages = current_msgs

                    if len(current_msgs) <= prev_msg_count:
                        continue
                    prev_msg_count = len(current_msgs)

                    last = current_msgs[-1]

                    if isinstance(last, AIMessage) and last.tool_calls:
                        # LLM 决定调用工具 —— 将每个调用记录到 tool_steps
                        for tc in last.tool_calls:
                            args_str = json.dumps(tc["args"], ensure_ascii=False)
                            tool_steps.append({
                                "name": tc["name"],
                                "args": args_str,
                                "result": None,
                            })
                        display = _format_tool_steps(tool_steps, "running")
                        chat_history[-1] = {"role": "assistant", "content": display}
                        yield "", chat_history, internal_msgs, *_panels()

                    elif isinstance(last, ToolMessage):
                        # 工具执行完成 —— 回填对应 step 的结果
                        # 找到最近一批连续的 ToolMessage
                        recent_tool_msgs = []
                        for m in reversed(current_msgs):
                            if isinstance(m, ToolMessage):
                                recent_tool_msgs.insert(0, m)
                            else:
                                break
                        # 回填结果到 tool_steps 的最后 N 项
                        offset = len(tool_steps) - len(recent_tool_msgs)
                        for i, tm in enumerate(recent_tool_msgs):
                            idx = offset + i
                            if 0 <= idx < len(tool_steps):
                                tool_steps[idx]["result"] = str(tm.content)[:300]
                        display = _format_tool_steps(tool_steps, "running")
                        chat_history[-1] = {"role": "assistant", "content": display}
                        yield "", chat_history, internal_msgs, *_panels()

                    elif isinstance(last, AIMessage) and not last.tool_calls:
                        # 最终文本回复（无工具调用）
                        final_text = last.content or ""
                        if tool_steps:
                            display = _format_tool_steps(tool_steps, "done") + final_text
                        else:
                            display = final_text
                        chat_history[-1] = {"role": "assistant", "content": display}
                        yield "", chat_history, final_messages, *_panels()

            except Exception as e:
                err_msg = f"❌ **Error**: {e}"
                if tool_steps:
                    display = _format_tool_steps(tool_steps, "done") + err_msg
                else:
                    display = err_msg
                chat_history[-1] = {"role": "assistant", "content": display}
                yield "", chat_history, internal_msgs, *_panels()
                return

            # ── 最终 yield：确保内部状态和面板都已更新 ──
            final_text = extract_final_response(final_messages)
            _api_logger.info(f"[webui] Final response: {final_text[:1000]}")
            if tool_steps:
                display = _format_tool_steps(tool_steps, "done") + final_text
            else:
                display = final_text
            chat_history[-1] = {"role": "assistant", "content": display}
            yield "", chat_history, final_messages, *_panels()

        # ── 绑定事件 ──
        output_components = [
            msg_input, chatbot, internal_state,
            tasks_display, team_display, todo_display, tool_log_display,
        ]

        msg_input.submit(respond_stream, [msg_input, chatbot, internal_state], output_components)
        send_btn.click(respond_stream, [msg_input, chatbot, internal_state], output_components)

        def do_compact(chat_history, state):
            if state:
                state = auto_compact(state)
                chat_history = list(chat_history)
                chat_history.append({"role": "assistant", "content": "⚡ Conversation compacted."})
            return chat_history, state

        compact_btn.click(do_compact, [chatbot, internal_state], [chatbot, internal_state])

        def show_tasks():
            return TASK_MGR.list_all()

        tasks_btn.click(show_tasks, [], [tasks_display])

        def show_team():
            return TEAM.list_all()

        team_btn.click(show_team, [], [team_display])

        def show_inbox():
            msgs = BUS.read_inbox("lead")
            return json.dumps(msgs, indent=2) if msgs else "Inbox empty."

        inbox_btn.click(show_inbox, [], [tool_log_display])

        def clear_chat():
            TOOL_LOG.clear()
            return [], [], *_panels()

        clear_btn.click(
            clear_chat, [],
            [chatbot, internal_state, tasks_display, team_display, todo_display, tool_log_display],
        )

    return demo


# ╔══════════════════════════════════════════════════════════════╗
# ║                      终端 REPL 模式                          ║
# ╚══════════════════════════════════════════════════════════════╝

def repl_mode():
    """终端交互模式，与原始 s_full.py 保持一致的交互体验。"""
    history: list[BaseMessage] = []
    while True:
        try:
            query = input("\033[36ms_full_lg >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        cmd = query.strip()
        if cmd.lower() in ("q", "exit", ""):
            break
        if cmd == "/compact":
            if history:
                print("[manual compact via /compact]")
                history[:] = auto_compact(history)
            continue
        if cmd == "/tasks":
            print(TASK_MGR.list_all())
            continue
        if cmd == "/team":
            print(TEAM.list_all())
            continue
        if cmd == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue

        history.append(HumanMessage(content=query))
        _api_logger.info(f"[repl] User: {query[:1000]}")
        history[:] = run_agent(history)
        final = extract_final_response(history)
        _api_logger.info(f"[repl] Final response: {final[:1000]}")
        print(f"\n{final}\n")


# ╔══════════════════════════════════════════════════════════════╗
# ║                         主入口                               ║
# ╚══════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    import sys

    _api_logger.info(f"Agent starting. Model={MODEL}, Workdir={WORKDIR}, LogDir={LOG_DIR}")

    if "--web" in sys.argv or len(sys.argv) == 1:
        # 默认启动 WebUI (也可用 --web 显式指定)
        demo = build_webui()
        port = 7860
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        print(f"\n{'=' * 50}")
        print(f"  Agent Chatbot starting on http://localhost:{port}")
        print(f"  Model: {MODEL}")
        print(f"  Tools: {len(ALL_TOOLS)} (incl. web_search, web_fetch)")
        print(f"{'=' * 50}\n")
        demo.launch(server_port=port, share="--share" in sys.argv)
    elif "--repl" in sys.argv:
        repl_mode()
    else:
        print("Usage:")
        print("  python s_full_langgraph.py          # WebUI (default)")
        print("  python s_full_langgraph.py --web     # WebUI")
        print("  python s_full_langgraph.py --repl    # Terminal REPL")
        print("  python s_full_langgraph.py --port 8080  # Custom port")
        print("  python s_full_langgraph.py --share   # Public Gradio link")
