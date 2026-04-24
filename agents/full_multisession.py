#!/usr/bin/env python3
"""
s_full_langgraph_multisession_merged.py

整合版：
- 以前版 s_full_langgraph_multisession_fs.py 的 UI 与 session 管理为基准
- 合并 tmp.py 的主要能力：bash/todo/subagent/background/task/team/message/compress/web
- 保留显式 Skill 注入与 allowed-tools 白名单绑定机制
"""

import json
import logging
import logging.handlers
import os
import re
import subprocess
import threading
import time
import traceback
import uuid
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from queue import Queue
from typing import Iterator, Literal, NotRequired, TypedDict

import gradio as gr
import requests
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

load_dotenv(override=True)


# =========================
# 全局配置
# =========================
WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
SESSIONS_DIR = WORKDIR / ".full_multisession_sessions"
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "sk-placeholder")
# 与 test.py 保持一致：优先使用 OPENAI_MODEL，其次 MODEL_ID
MODEL = os.environ.get("OPENAI_MODEL") or os.environ.get("MODEL_ID", "gpt-4o")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TOKEN_THRESHOLD = 100_000
# 网络搜索最多多少层
MAX_WEB_SEARCH_CALLS_PER_TURN = 20
MAX_WEB_FETCH_CALLS_PER_TURN = 20

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

# Skill frontmatter `allowed-tools` 仅可限制下列名称（与 FILE_TOOLS 子集一致）
BINDABLE_FILE_TOOL_NAMES = frozenset(
    {"read_file", "write_file", "edit_file", "list_files", "web_search", "web_fetch"}
)
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}


# =========================
# 日志
# =========================
LOG_DIR = WORKDIR / ".log"
LOG_DIR.mkdir(exist_ok=True)

_api_logger = logging.getLogger("agent.api")
_api_logger.setLevel(logging.DEBUG)
_api_logger.propagate = False
if not _api_logger.handlers:
    _api_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "api.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    _api_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    _api_logger.addHandler(_api_handler)


def _log_messages(tag: str, messages: list, extra: str = "") -> None:
    parts = [f"--- [{tag}] {extra} ---"]
    for m in messages:
        role = getattr(m, "type", "unknown")
        content = str(getattr(m, "content", ""))[:2000]
        tool_calls = getattr(m, "tool_calls", None)
        line = f"  [{role}] {content}"
        if tool_calls:
            tc_summary = ", ".join(
                f"{tc['name']}({json.dumps(tc['args'], ensure_ascii=False)[:200]})" for tc in tool_calls
            )
            line += f"  | tool_calls: {tc_summary}"
        parts.append(line)
    _api_logger.info("\n".join(parts))


def _log_response(tag: str, response, extra: str = "") -> None:
    content = str(getattr(response, "content", ""))[:2000]
    tool_calls = getattr(response, "tool_calls", None)
    tc_info = ""
    if tool_calls:
        tc_info = " | tool_calls: " + ", ".join(
            f"{tc['name']}({json.dumps(tc['args'], ensure_ascii=False)[:200]})" for tc in tool_calls
        )
    _api_logger.info(f"[{tag}] Response{' ' + extra if extra else ''}: {content}{tc_info}")


# =========================
# 基础能力：安全文件读写 + bash
# =========================
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except Exception as e:
        return f"Error: {e}"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        target = safe_path(path)
        if target.is_dir():
            entries = sorted(target.iterdir(), key=lambda p: p.name)
            lines = [f"{e.name}{'/' if e.is_dir() else ''}" for e in entries]
            return "\n".join(lines)[:50000] if lines else f"(empty directory) {path}"
        lines = target.read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_list_files(path: str, suffix: str | None = None) -> str:
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
            return f"(no files found in {path})"
        return "\n".join(files)[:50000]
    except Exception as e:
        return f"Error: {e}"


# =========================
# Skill 加载器
# =========================
class SkillLoader:
    EXPLICIT_INVOKE_LINE = re.compile(r"^\s*/invoke_skill\s+([^\n]+?)\s*(?:\n(.*)|$)", re.DOTALL)

    def __init__(self, skills_dir: Path):
        self.skills: dict[str, dict] = {}
        if skills_dir.exists():
            for skill_file in sorted(skills_dir.rglob("SKILL.md")):
                text = skill_file.read_text(encoding="utf-8")
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
        if not name or name not in self.skills:
            return None
        raw = self.skills[name]["meta"].get("allowed-tools")
        if not isinstance(raw, list) or not raw:
            return None
        names = []
        for item in raw:
            n = str(item).strip()
            if n and n in BINDABLE_FILE_TOOL_NAMES:
                names.append(n)
        return names or None

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills)"
        lines = []
        for name, skill in self.skills.items():
            if self.is_explicit_invoke_only(name):
                continue
            lines.append(f"  - {name}: {skill['meta'].get('description', '-')}")
        return "\n".join(lines) if lines else "(no skills)"

    def explicit_invoke_skill_choices(self) -> list[str]:
        return sorted(name for name in self.skills if self.is_explicit_invoke_only(name))

    def load(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Skill「{name}」未注册。已知技能：{', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"

    def parse_explicit_invoke_prefix(self, user_text: str) -> tuple[str | None, str]:
        m = self.EXPLICIT_INVOKE_LINE.match(user_text)
        if not m:
            return None, user_text
        name = m.group(1).strip()
        rest = (m.group(2) or "").lstrip("\n")
        return name, rest


# =========================
# Web 工具实现
# =========================
class HTMLTextExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "svg", "iframe"}
    BLOCK_TAGS = {
        "p",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "td",
        "th",
        "blockquote",
        "pre",
        "section",
        "article",
    }

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
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
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
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            timeout=10,
        )
        resp.raise_for_status()
        links = re.findall(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span)', resp.text, re.DOTALL)
        results = []
        for i, (url, title) in enumerate(links[:max_results]):
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            real_url = url
            uddg = re.search(r"uddg=([^&]+)", url)
            if uddg:
                from urllib.parse import unquote

                real_url = unquote(uddg.group(1))
            results.append(f"[{i + 1}] {title_clean}\n    URL: {real_url}\n    {snippet}")
        return "\n\n".join(results) if results else "No results found."
    except Exception as e:
        return f"Search error: {e}"


# =========================
# Todo / Task / Background / Team
# =========================
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
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item["status"], "[?]")
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        return any(item.get("status") != "completed" for item in self.items)


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
        return json.loads(p.read_text(encoding="utf-8"))

    def _save(self, task: dict) -> None:
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2), encoding="utf-8")

    def create(self, subject: str, description: str = "") -> str:
        task = {
            "id": self._next_id(),
            "subject": subject,
            "description": description,
            "status": "pending",
            "owner": None,
            "blockedBy": [],
            "blocks": [],
        }
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, tid: int) -> str:
        return json.dumps(self._load(tid), indent=2, ensure_ascii=False)

    def update(
        self,
        tid: int,
        status: str | None = None,
        add_blocked_by: list | None = None,
        add_blocks: list | None = None,
    ) -> str:
        task = self._load(tid)
        if status:
            task["status"] = status
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text(encoding="utf-8"))
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
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_all(self) -> str:
        tasks = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(TASKS_DIR.glob("task_*.json"))]
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


class BackgroundManager:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.notifications: Queue = Queue()

    def run(self, command: str, timeout: int = 120) -> str:
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        return f"Background task {tid} started: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int) -> None:
        try:
            r = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=timeout)
            output = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": output or "(no output)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        self.notifications.put(
            {"task_id": tid, "status": self.tasks[tid]["status"], "result": str(self.tasks[tid]["result"])[:500]}
        )

    def check(self, tid: str | None = None) -> str:
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result', '(running)')}" if t else f"Unknown: {tid}"
        return "\n".join(f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()) or "No bg tasks."

    def drain(self) -> list[dict]:
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


class MessageBus:
    def __init__(self):
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str, msg_type: str = "message", extra: dict | None = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: invalid msg_type {msg_type}"
        msg = {"type": msg_type, "from": sender, "content": content, "timestamp": time.time()}
        if extra:
            msg.update(extra)
        with open(INBOX_DIR / f"{to}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list[dict]:
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists():
            return []
        txt = path.read_text(encoding="utf-8").strip()
        msgs = [json.loads(line) for line in txt.splitlines() if line] if txt else []
        path.write_text("", encoding="utf-8")
        return msgs

    def broadcast(self, sender: str, content: str, names: list[str]) -> str:
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


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
    BUS.send(
        "lead",
        req["from"],
        feedback,
        "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"


class TeammateManager:
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load_config()

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        return {"team_name": "default", "members": []}

    def _save_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")

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

    def _set_status(self, name: str, status: str) -> None:
        member = self._find(name)
        if member:
            member["status"] = status
            self._save_config()

    def _loop(self, name: str, role: str, prompt: str) -> None:
        sys_msg = SystemMessage(
            content=f"You are '{name}', role: {role}, team: {self.config['team_name']}, at {WORKDIR}. Use idle when done."
        )

        @tool
        def tm_bash(command: str) -> str:
            """Run a shell command in workspace."""
            return run_bash(command)

        @tool
        def tm_read_file(path: str) -> str:
            """Read file contents from workspace path."""
            return run_read(path)

        @tool
        def tm_write_file(path: str, content: str) -> str:
            """Write file content into workspace path."""
            return run_write(path, content)

        @tool
        def tm_edit_file(path: str, old_text: str, new_text: str) -> str:
            """Replace first matching text in file."""
            return run_edit(path, old_text, new_text)

        @tool
        def tm_send_message(to: str, content: str) -> str:
            """Send message to teammate or lead."""
            return self.bus.send(name, to, content)

        @tool
        def tm_idle() -> str:
            """Mark teammate as entering idle phase."""
            return "Entering idle phase."

        @tool
        def tm_claim_task(task_id: int) -> str:
            """Claim a task by id for this teammate."""
            return self.task_mgr.claim(task_id, name)

        tm_tools = [tm_bash, tm_read_file, tm_write_file, tm_edit_file, tm_send_message, tm_idle, tm_claim_task]
        tm_llm = llm.bind_tools(tm_tools)
        tm_tool_map = {t.name: t for t in tm_tools}
        messages: list[BaseMessage] = [HumanMessage(content=prompt)]

        while True:
            for _ in range(50):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append(HumanMessage(content=json.dumps(msg, ensure_ascii=False)))
                try:
                    response = tm_llm.invoke([sys_msg] + messages)
                except Exception:
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
                    messages.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))
                if idle_requested:
                    break
            self._set_status(name, "idle")
            time.sleep(2)
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


TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()


# =========================
# 压缩与子代理
# =========================
def estimate_tokens(messages: list[BaseMessage]) -> int:
    return sum(len(str(m.content)) for m in messages) // 4


def microcompact(messages: list[BaseMessage]) -> None:
    tool_indices = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and len(m.content) > 100
    ]
    if len(tool_indices) <= 3:
        return
    for idx in tool_indices[:-3]:
        old = messages[idx]
        messages[idx] = ToolMessage(content="[cleared]", tool_call_id=old.tool_call_id)


def auto_compact(messages: list[BaseMessage]) -> list[BaseMessage]:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps({"role": msg.type, "content": str(msg.content)[:5000]}, ensure_ascii=False) + "\n")
    conv_text = "\n".join(str(m.content)[:2000] for m in messages)[:80000]
    compact_req = [HumanMessage(content=f"Summarize this conversation for continuity:\n{conv_text}")]
    _log_messages("compact", compact_req, "compress request")
    resp = llm.invoke(compact_req)
    _log_response("compact", resp)
    summary = str(resp.content)
    return [
        HumanMessage(content=f"[Compressed. Transcript: {path}]\n{summary}"),
        AIMessage(content="Understood. Continuing with summary context."),
    ]


def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    @tool
    def sub_bash(command: str) -> str:
        """Run a shell command in workspace."""
        return run_bash(command)

    @tool
    def sub_read_file(path: str) -> str:
        """Read file contents from workspace path."""
        return run_read(path)

    @tool
    def sub_write_file(path: str, content: str) -> str:
        """Write file content into workspace path."""
        return run_write(path, content)

    @tool
    def sub_edit_file(path: str, old_text: str, new_text: str) -> str:
        """Replace first matching text in file."""
        return run_edit(path, old_text, new_text)

    sub_tools = [sub_bash, sub_read_file]
    if agent_type != "Explore":
        sub_tools += [sub_write_file, sub_edit_file]
    sub_llm = llm.bind_tools(sub_tools)
    sub_tool_map = {t.name: t for t in sub_tools}
    messages: list[BaseMessage] = [HumanMessage(content=prompt)]

    response = None
    for _ in range(30):
        response = sub_llm.invoke(messages)
        messages.append(response)
        if not response.tool_calls:
            break
        for tc in response.tool_calls:
            handler = sub_tool_map.get(tc["name"])
            try:
                output = handler.invoke(tc["args"]) if handler else "Unknown tool"
            except Exception as e:
                output = f"Error: {e}"
            messages.append(ToolMessage(content=str(output)[:50000], tool_call_id=tc["id"]))
    if response and response.content:
        return str(response.content)
    return "(subagent failed)"


TEAM = TeammateManager(BUS, TASK_MGR)


# =========================
# LangChain 工具定义
# =========================
@tool
def bash(command: str) -> str:
    """Run a shell command in workspace directory."""
    return run_bash(command)


@tool
def load_skill(name: str) -> str:
    """Load a registered skill by name."""
    if SKILLS.is_explicit_invoke_only(name):
        return (
            f"Error: Skill「{name}」为显式启用专用，禁止通过 load_skill 加载。"
            "请让用户在消息首行使用 `/invoke_skill {name}` 后换行写问题，或在 WebUI 选择显式 Skill。"
        )
    return SKILLS.load(name)


@tool
def read_file(path: str, limit: int | None = None) -> str:
    """Read file contents, optionally limited to N lines."""
    return run_read(path, limit)


@tool
def write_file(path: str, content: str) -> str:
    """Write content to file, creating parent directories if needed."""
    return run_write(path, content)


@tool
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Replace first occurrence of old_text with new_text in file."""
    return run_edit(path, old_text, new_text)


@tool
def list_files(path: str, suffix: str | None = None) -> str:
    """List files recursively under directory, optional suffix filter."""
    return run_list_files(path, suffix)


class TodoItem(BaseModel):
    content: str = Field(description="Todo item description")
    status: Literal["pending", "in_progress", "completed"] = Field(description="Item status")
    activeForm: str = Field(description="Current active form/step of work")


@tool
def todo_write(items: list[TodoItem]) -> str:
    """Update short todo checklist with validation rules."""
    return TODO.update([item.model_dump() for item in items])


@tool
def subagent(prompt: str, agent_type: str = "Explore") -> str:
    """Run an isolated subagent and return its final summary."""
    return run_subagent(prompt, agent_type)


@tool
def compress() -> str:
    """Request manual conversation compression."""
    return "__COMPRESS_SIGNAL__"


@tool
def background_run(command: str, timeout: int = 120) -> str:
    """Run a command in background thread and return task id."""
    return BG.run(command, timeout)


@tool
def check_background(task_id: str | None = None) -> str:
    """Check one background task or list all background tasks."""
    return BG.check(task_id)


@tool
def task_create(subject: str, description: str = "") -> str:
    """Create a persistent task item."""
    return TASK_MGR.create(subject, description)


@tool
def task_get(task_id: int) -> str:
    """Get task details by task id."""
    return TASK_MGR.get(task_id)


@tool
def task_update(
    task_id: int, status: str | None = None, add_blocked_by: list[int] | None = None, add_blocks: list[int] | None = None
) -> str:
    """Update task status or dependency relations."""
    return TASK_MGR.update(task_id, status, add_blocked_by, add_blocks)


@tool
def task_list() -> str:
    """List all tasks."""
    return TASK_MGR.list_all()


@tool
def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """Spawn an autonomous teammate worker."""
    return TEAM.spawn(name, role, prompt)


@tool
def list_teammates() -> str:
    """List teammates and statuses."""
    return TEAM.list_all()


@tool
def send_message(to: str, content: str, msg_type: str = "message") -> str:
    """Send a message to teammate inbox."""
    return BUS.send("lead", to, content, msg_type)


@tool
def read_inbox() -> str:
    """Read and drain lead inbox."""
    msgs = BUS.read_inbox("lead")
    return json.dumps(msgs, indent=2, ensure_ascii=False) if msgs else "Inbox empty."


@tool
def broadcast(content: str) -> str:
    """Broadcast message to all teammates."""
    return BUS.broadcast("lead", content, TEAM.member_names())


@tool
def shutdown_request(teammate: str) -> str:
    """Request teammate graceful shutdown."""
    return handle_shutdown_request(teammate)


@tool
def plan_approval(request_id: str, approve: bool, feedback: str = "") -> str:
    """Approve or reject a teammate plan request."""
    return handle_plan_review(request_id, approve, feedback)


@tool
def claim_task(task_id: int) -> str:
    """Claim task for lead agent."""
    return TASK_MGR.claim(task_id, "lead")


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search web using DuckDuckGo HTML endpoint."""
    return web_search_impl(query, max_results)


@tool
def web_fetch(url: str) -> str:
    """Fetch URL content and extract readable text."""
    return web_fetch_impl(url)


ALL_TOOLS = [
    bash,
    load_skill,
    read_file,
    write_file,
    edit_file,
    list_files,
    todo_write,
    subagent,
    compress,
    background_run,
    check_background,
    task_create,
    task_get,
    task_update,
    task_list,
    spawn_teammate,
    list_teammates,
    send_message,
    read_inbox,
    broadcast,
    shutdown_request,
    plan_approval,
    claim_task,
    web_search,
    web_fetch,
]
# 模型侧不暴露 load_skill：仅运行时注入
FILE_TOOLS = [t for t in ALL_TOOLS if t.name != "load_skill"]
TOOL_MAP = {t.name: t for t in ALL_TOOLS}
FILE_TOOL_BY_NAME = {t.name: t for t in FILE_TOOLS}


# =========================
# LangGraph
# =========================
_extra_body: dict | None = None
if "dashscope.aliyuncs.com" in OPENAI_BASE_URL and MODEL.startswith("qwen"):
    # 兼容阿里云百炼深度思考模型；与 test.py 的调用参数对齐
    _extra_body = {"enable_thinking": True}

llm = ChatOpenAI(
    model=MODEL,
    base_url=OPENAI_BASE_URL or None,
    api_key=OPENAI_API_KEY,
    timeout=120,
    max_retries=2,
    extra_body=_extra_body,
)

SYSTEM_PROMPT = (
    f"You are a coding agent at {WORKDIR}. "
    "You are a specialist in HarmonyOS Next development. \n"
    "Use tools when needed. Domain skills are not loaded automatically.\n"
    "When presenting user-facing content, output complete Markdown sections.\n"
    f"Available skills:\n{SKILLS.descriptions()}"
)


class AgentState(TypedDict):
    messages: list[BaseMessage]
    rounds_without_todo: NotRequired[int]
    tool_allowlist: NotRequired[list[str] | None]
    web_search_calls: NotRequired[int]
    web_fetch_calls: NotRequired[int]
    web_tools_blocked: NotRequired[bool]


def _tools_for_allowlist(allowlist: list[str] | None, web_tools_blocked: bool = False) -> list:
    blocked_names = {"web_search", "web_fetch"} if web_tools_blocked else set()
    if not allowlist:
        return [t for t in FILE_TOOLS if t.name not in blocked_names]
    picked = [FILE_TOOL_BY_NAME[n] for n in allowlist if n in FILE_TOOL_BY_NAME and n not in blocked_names]
    if picked:
        return picked
    return [t for t in FILE_TOOLS if t.name not in blocked_names]


def preprocess_node(state: AgentState) -> dict:
    messages = list(state["messages"])
    microcompact(messages)
    if estimate_tokens(messages) > TOKEN_THRESHOLD:
        messages = auto_compact(messages)
    notifs = BG.drain()
    if notifs:
        txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
        messages.append(HumanMessage(content=f"<background-results>\n{txt}\n</background-results>"))
        messages.append(AIMessage(content="Noted background results."))
    inbox = BUS.read_inbox("lead")
    if inbox:
        messages.append(HumanMessage(content=f"<inbox>{json.dumps(inbox, ensure_ascii=False, indent=2)}</inbox>"))
        messages.append(AIMessage(content="Noted inbox messages."))
    return {
        "messages": messages,
        "tool_allowlist": state.get("tool_allowlist"),
        "rounds_without_todo": state.get("rounds_without_todo", 0),
        "web_search_calls": state.get("web_search_calls", 0),
        "web_fetch_calls": state.get("web_fetch_calls", 0),
        "web_tools_blocked": state.get("web_tools_blocked", False),
    }


def llm_call_node(state: AgentState) -> dict:
    messages = state["messages"]
    allowlist = state.get("tool_allowlist")
    web_tools_blocked = state.get("web_tools_blocked", False)
    tools = _tools_for_allowlist(allowlist, web_tools_blocked=web_tools_blocked)
    model = llm.bind_tools(tools)
    full_messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages
    start_ts = time.time()
    _api_logger.info(
        "[main] LLM invoke start | messages=%d | allowlist=%s",
        len(messages),
        ",".join(allowlist) if allowlist else "default",
    )
    _log_messages("main", full_messages, "LLM request")
    # 加一层兜底：首调异常/空结果时重试一次，避免前端无返回
    first_err: Exception | None = None
    response: AIMessage | None = None
    try:
        response = model.invoke(full_messages)
    except Exception as e:
        first_err = e
        _api_logger.exception("[main] LLM first invoke failed")
        time.sleep(0.25)
        try:
            response = model.invoke(full_messages)
            _api_logger.info("[main] LLM invoke recovered on retry")
        except Exception as retry_e:
            _api_logger.exception("[main] LLM retry invoke failed")
            content = f"模型调用失败：{retry_e}"
            if first_err:
                content += f"\n首次错误：{first_err}"
            response = AIMessage(content=content)
    if response is None or (
        not getattr(response, "tool_calls", None) and not message_content_to_text(getattr(response, "content", "")).strip()
    ):
        _api_logger.warning("[main] LLM returned empty response; emitting fallback text")
        response = AIMessage(content="模型暂时没有返回内容，请重试一次。")
    elapsed = time.time() - start_ts
    _log_response("main", response)
    _api_logger.info(
        "[main] LLM invoke done | elapsed=%.2fs | has_tool_calls=%s | content_len=%d",
        elapsed,
        bool(getattr(response, "tool_calls", None)),
        len(str(getattr(response, "content", "") or "")),
    )
    out: dict = {
        "messages": messages + [response],
        "rounds_without_todo": state.get("rounds_without_todo", 0),
        "web_search_calls": state.get("web_search_calls", 0),
        "web_fetch_calls": state.get("web_fetch_calls", 0),
        "web_tools_blocked": web_tools_blocked,
    }
    if allowlist is not None:
        out["tool_allowlist"] = allowlist
    return out


def tool_execute_node(state: AgentState) -> dict:
    messages = list(state["messages"])
    last_msg = messages[-1]
    allowlist = state.get("tool_allowlist")
    web_search_calls = state.get("web_search_calls", 0)
    web_fetch_calls = state.get("web_fetch_calls", 0)
    web_tools_blocked = state.get("web_tools_blocked", False)
    trusted = isinstance(last_msg, AIMessage) and bool(last_msg.additional_kwargs.get("trusted_programmatic_skill"))
    used_todo = False
    compress_requested = False
    hit_web_limit = False
    for tc in last_msg.tool_calls:
        tool_name = tc["name"]
        tool_args = tc["args"]
        handler = TOOL_MAP.get(tool_name)
        _api_logger.info(
            "[main] Tool start | name=%s | args=%s",
            tool_name,
            json.dumps(tool_args, ensure_ascii=False)[:800],
        )
        try:
            if allowlist and tool_name in BINDABLE_FILE_TOOL_NAMES and tool_name not in allowlist:
                output = f"Error: 工具「{tool_name}」不在本轮允许列表中。仅允许：{', '.join(allowlist)}。"
            elif tool_name == "web_search" and web_search_calls >= MAX_WEB_SEARCH_CALLS_PER_TURN:
                output = (
                    f"Error: web_search 调用已达本轮上限（{MAX_WEB_SEARCH_CALLS_PER_TURN} 次），"
                    "请基于现有证据总结，或让用户提供更具体线索后重试。"
                )
                hit_web_limit = True
            elif tool_name == "web_fetch" and web_fetch_calls >= MAX_WEB_FETCH_CALLS_PER_TURN:
                output = (
                    f"Error: web_fetch 调用已达本轮上限（{MAX_WEB_FETCH_CALLS_PER_TURN} 次），"
                    "请基于已抓取内容输出结论，或请求用户提供可访问的目标链接。"
                )
                hit_web_limit = True
            elif tool_name == "compress":
                compress_requested = True
                output = "Compressing conversation..."
            elif tool_name == "load_skill":
                name = (tool_args or {}).get("name", "")
                if SKILLS.is_explicit_invoke_only(name) and not trusted:
                    output = f"Error: Skill「{name}」为显式启用专用，已拒绝本次 load_skill。"
                else:
                    output = SKILLS.load(name)
            else:
                output = handler.invoke(tool_args) if handler else f"Unknown tool: {tool_name}"
        except Exception as e:
            tb = traceback.format_exc(limit=5)
            output = f"Error: tool `{tool_name}` failed: {e}\nTraceback:\n{tb}"
            _api_logger.exception("[main] Tool failed | name=%s", tool_name)
        if tool_name == "todo_write":
            used_todo = True
        if tool_name == "web_search" and not str(output).startswith("Error: web_search 调用已达本轮上限"):
            web_search_calls += 1
        if tool_name == "web_fetch" and not str(output).startswith("Error: web_fetch 调用已达本轮上限"):
            web_fetch_calls += 1
        _api_logger.info("[main] Tool done | name=%s | output=%s", tool_name, str(output)[:800])
        messages.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))
    if compress_requested:
        messages = auto_compact(messages)
    rounds = state.get("rounds_without_todo", 0)
    rounds = 0 if used_todo else rounds + 1
    if hit_web_limit and not web_tools_blocked:
        web_tools_blocked = True
        messages.append(
            HumanMessage(
                content=(
                    "<runtime-hint>web_search/web_fetch 已达本轮上限，后续请禁止继续检索，"
                    "直接基于已有工具结果完成后续分析与最终回答。</runtime-hint>"
                )
            )
        )
    if TODO.has_open_items() and rounds >= 3:
        messages.append(HumanMessage(content="<reminder>Update your todos.</reminder>"))
    out: dict = {
        "messages": messages,
        "rounds_without_todo": rounds,
        "web_search_calls": web_search_calls,
        "web_fetch_calls": web_fetch_calls,
        "web_tools_blocked": web_tools_blocked,
    }
    if allowlist is not None:
        out["tool_allowlist"] = allowlist
    return out


def should_continue(state: AgentState) -> str:
    last_msg = state["messages"][-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return "end"


graph_builder = StateGraph(AgentState)
graph_builder.add_node("preprocess", preprocess_node)
graph_builder.add_node("llm_call", llm_call_node)
graph_builder.add_node("tool_execute", tool_execute_node)
graph_builder.set_entry_point("preprocess")
graph_builder.add_edge("preprocess", "llm_call")
graph_builder.add_conditional_edges("llm_call", should_continue, {"tools": "tool_execute", "end": END})
graph_builder.add_edge("tool_execute", "preprocess")
agent_graph = graph_builder.compile()
_AGENT_RECURSION_LIMIT = 64


def message_content_to_text(content) -> str:
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


def extract_final_response(messages: list[BaseMessage]) -> str:
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
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    def list_sessions_with_meta(self) -> list[dict]:
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
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [self._deserialize_message(item) for item in raw]

    def save(self, session_id: str, messages: list[BaseMessage]) -> None:
        raw = [self._serialize_message(m) for m in messages]
        self._path(session_id).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

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
    return bool(SESSION_ID_RE.match(session_id))


class AgentRuntime:
    def __init__(self, store: SessionStore):
        self.store = store
        self.current_session = "default"
        self.store.create(self.current_session)
        self.history = self.store.load(self.current_session)
        # run_query_stream 会在持锁时 yield，外层 UI 逻辑会回调 snapshot() 再次取锁；
        # 使用可重入锁避免同线程重入时出现“stream 无响应”。
        self.lock = threading.RLock()

    def list_sessions(self) -> list[str]:
        return self.store.list_sessions()

    def switch_session(self, session_id: str, new_context: bool = False) -> None:
        if not validate_session_id(session_id):
            raise ValueError("session_id must match [A-Za-z0-9._-]+")
        with self.lock:
            self.store.save(self.current_session, self.history)
            self.store.create(session_id)
            if new_context:
                self.store.save(session_id, [])
            self.current_session = session_id
            self.history = self.store.load(session_id)

    def _prepare_turn(self, query: str, ui_forced_skill: str | None = None) -> tuple[str, list[str] | None]:
        parsed_name, rest = SKILLS.parse_explicit_invoke_prefix(query)
        pending: str | None = None
        if parsed_name:
            if parsed_name not in SKILLS.skills:
                body = (f"/invoke_skill 指定的名称「{parsed_name}」不存在。\n\n" + rest).strip()
            else:
                pending = parsed_name
                body = rest.strip() if rest.strip() else "（请在此说明你的问题。）"
        elif ui_forced_skill and ui_forced_skill.strip() in SKILLS.skills:
            pending = ui_forced_skill.strip()
            body = query.strip()
        else:
            body = query.strip()

        skill_allow: list[str] | None = None
        if pending and pending in SKILLS.skills:
            skill_allow = SKILLS.tool_allowlist_for_skill(pending)
            bind_hint = ""
            if skill_allow:
                bind_hint = "\n\n[Runtime 工具绑定] 本轮仅可调用工具：" + ", ".join(skill_allow) + "。"
            self.history.append(
                SystemMessage(
                    content=(
                        "用户已显式指定垂域 Skill；本轮回答须遵循其中流程与约束。\n"
                        + SKILLS.load(pending)
                        + bind_hint
                    )
                )
            )
        return body, skill_allow

    def run_query_stream(self, query: str, ui_forced_skill: str | None = None) -> Iterator[dict]:
        with self.lock:
            body, skill_allow = self._prepare_turn(query, ui_forced_skill)
            _api_logger.info(
                "[runtime] stream start | session=%s | query_len=%d | forced_skill=%s | parsed_allowlist=%s",
                self.current_session,
                len(body),
                ui_forced_skill or "(none)",
                ",".join(skill_allow) if skill_allow else "default",
            )
            self.history.append(HumanMessage(content=body))
            yield {"phase": "start", "messages": list(self.history)}

            payload: AgentState = {"messages": list(self.history), "rounds_without_todo": 0}
            payload["web_search_calls"] = 0
            payload["web_fetch_calls"] = 0
            payload["web_tools_blocked"] = False
            if skill_allow:
                payload["tool_allowlist"] = skill_allow
            cfg = {"recursion_limit": _AGENT_RECURSION_LIMIT}
            final_messages = list(self.history)
            prev_msg_count = len(final_messages)
            progress_idx = 0
            last_progress_ts = time.time()

            try:
                for snapshot in agent_graph.stream(payload, config=cfg, stream_mode="values"):
                    current_msgs = list(snapshot["messages"])
                    final_messages = current_msgs
                    if len(current_msgs) <= prev_msg_count:
                        now = time.time()
                        if now - last_progress_ts >= 10:
                            _api_logger.info(
                                "[runtime] stream heartbeat | session=%s | msg_count=%d | no_new_messages_for=%.1fs",
                                self.current_session,
                                len(current_msgs),
                                now - last_progress_ts,
                            )
                        continue
                    prev_msg_count = len(current_msgs)
                    progress_idx += 1
                    last = current_msgs[-1]
                    last_type = getattr(last, "type", type(last).__name__)
                    has_tool_calls = bool(isinstance(last, AIMessage) and last.tool_calls)
                    _api_logger.info(
                        "[runtime] stream progress | session=%s | step=%d | msg_count=%d | last_type=%s | last_has_tool_calls=%s",
                        self.current_session,
                        progress_idx,
                        len(current_msgs),
                        last_type,
                        has_tool_calls,
                    )
                    last_progress_ts = time.time()
                    yield {"phase": "progress", "messages": current_msgs}
            except GraphRecursionError as e:
                _api_logger.warning(
                    "[runtime] stream recursion guard triggered | session=%s | steps=%d | err=%s",
                    self.current_session,
                    progress_idx,
                    e,
                )
                final_messages = list(final_messages)
                try:
                    # 达到最大循环后，强制进入“无工具收敛输出”阶段，基于现有证据直接给出结论。
                    forced_finalize_prompt = HumanMessage(
                        content=(
                            "<runtime-finalize>\n"
                            "本轮已达到最大循环次数，禁止继续调用任何工具。\n"
                            "请仅基于当前对话中已存在的检索/抓取结果，直接完成后续知识挖掘与结构化输出。\n"
                            "如果证据不足，请明确标注假设与不确定性，但必须给出可落地的最终结果。\n"
                            "</runtime-finalize>"
                        )
                    )
                    final_completion = llm.invoke([SystemMessage(content=SYSTEM_PROMPT)] + final_messages + [forced_finalize_prompt])
                    if message_content_to_text(getattr(final_completion, "content", "")).strip():
                        final_messages.append(final_completion)
                    else:
                        final_messages.append(
                            AIMessage(
                                content=(
                                    "本轮工具调用次数过多且已触发自动收敛，但模型未返回有效正文。"
                                    "请重试，或提供更明确线索以提升命中率。"
                                )
                            )
                        )
                except Exception as finalize_err:
                    _api_logger.exception("[runtime] finalize-after-recursion failed")
                    final_messages.append(
                        AIMessage(
                            content=(
                                "本轮工具调用次数过多且未收敛，已自动中止；"
                                f"收敛输出失败：{finalize_err}"
                            )
                        )
                    )

            self.history = final_messages
            self.store.save(self.current_session, self.history)
            final_resp = extract_final_response(self.history)
            _api_logger.info(
                "[runtime] stream done | session=%s | total_msgs=%d | final_response_len=%d",
                self.current_session,
                len(self.history),
                len(final_resp),
            )
            yield {
                "phase": "done",
                "messages": list(self.history),
                "final_response": final_resp,
            }

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "current_session": self.current_session,
                "sessions": self.list_sessions(),
                "session_meta": self.store.list_sessions_with_meta(),
                "history_size": len(self.history),
                "tasks": TASK_MGR.list_all(),
                "team": TEAM.list_all(),
                "todos": TODO.render(),
            }


# =========================
# Gradio WebUI（以前版 UI 为基准）
# =========================
def build_webui(runtime: AgentRuntime):
    with gr.Blocks(
        title="s_full_langgraph_multisession_merged",
        theme=gr.themes.Soft(primary_hue="blue", neutral_hue="slate", font=gr.themes.GoogleFont("Inter")),
    ) as demo:
        gr.Markdown(
            "## 🤖 LangGraph Multi-Session Agent (Merged)\n"
            f"<sub>Model: **{MODEL}** &nbsp;|&nbsp; Workspace: `{WORKDIR}` &nbsp;|&nbsp; "
            "Session/UI 以前版为准，功能已并入 task/team/todo/background/subagent/web</sub>"
        )

        internal_state = gr.State(value=runtime.history)
        with gr.Row(equal_height=False):
            with gr.Column(scale=4):
                chat = gr.Chatbot(height=560)
                explicit_choices = ["（不使用）"] + SKILLS.explicit_invoke_skill_choices()
                force_skill_dd = gr.Dropdown(
                    label="显式启用 Skill（可选）",
                    choices=explicit_choices,
                    value=explicit_choices[0],
                    info="仅 explicit_invoke_only 技能；与首行 /invoke_skill 同时存在时以首行为准。",
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
            with gr.Column(scale=1, min_width=320):
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
                        label="选择已有会话", choices=runtime.list_sessions(), value=runtime.current_session
                    )
                    use_selected_btn = gr.Button("使用所选会话")
                with gr.Accordion("🕘 历史 Sessions", open=True):
                    session_history_md = gr.Markdown(value="(暂无会话历史)")
                with gr.Accordion("📊 当前状态", open=False):
                    state_json = gr.JSON(label="运行状态", value=runtime.snapshot())

        def _render_tool_thinking(tool_calls: list[dict], tool_results: list[ToolMessage]) -> str:
            blocks: list[str] = []
            result_map = {tm.tool_call_id: message_content_to_text(tm.content) for tm in tool_results}
            for i, tc in enumerate(tool_calls, start=1):
                name = tc.get("name", "unknown_tool")
                args = tc.get("args", {})
                call_id = tc.get("id", "")
                result = result_map.get(call_id, "(no tool result)")
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
                msgi = messages[idx]
                if isinstance(msgi, AIMessage) and msgi.tool_calls:
                    j = idx + 1
                    tool_msgs: list[ToolMessage] = []
                    while j < len(messages) and isinstance(messages[j], ToolMessage):
                        tool_msgs.append(messages[j])
                        j += 1
                    block = _render_tool_thinking(msgi.tool_calls, tool_msgs)
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

        def _format_tool_steps(steps: list[dict], status: str = "running") -> str:
            """将工具调用步骤格式化为 Markdown（参考 tmp.py 的 thinking 展示）。"""
            if not steps:
                return ""

            icon = {"running": "⏳", "done": "✅"}.get(status, "🔧")
            header = {"running": "Working...", "done": f"Used {len(steps)} tool(s)"}.get(status, "")

            lines: list[str] = []
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

        def _chat_view_from_messages(messages: list[BaseMessage]) -> list[dict]:
            view: list[dict] = []
            idx = 0
            while idx < len(messages):
                m = messages[idx]
                if isinstance(m, HumanMessage):
                    view.append({"role": "user", "content": message_content_to_text(m.content)})
                    idx += 1
                    continue
                if isinstance(m, AIMessage):
                    if m.tool_calls:
                        j = idx + 1
                        while j < len(messages) and isinstance(messages[j], ToolMessage):
                            j += 1
                        text_part = message_content_to_text(m.content).strip()
                        if text_part:
                            view.append({"role": "assistant", "content": text_part})
                        idx = j
                        continue
                    content = message_content_to_text(m.content).strip()
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
                yield "", _chat_view_from_messages(history), history, snap
                return
            ui_skill = None
            if forced_skill_choice and forced_skill_choice != "（不使用）":
                ui_skill = forced_skill_choice
            stream = runtime.run_query_stream(text.strip(), ui_forced_skill=ui_skill)

            tool_steps: list[dict] = []
            latest_chat_view: list[dict] | None = None

            for event in stream:
                history = event["messages"]
                chat_view = _chat_view_from_messages(history)

                if event["phase"] == "start":
                    chat_view = list(chat_view)
                    chat_view.append({"role": "assistant", "content": "⏳ Thinking..."})
                    latest_chat_view = chat_view
                elif event["phase"] == "progress":
                    if history:
                        last = history[-1]
                        if isinstance(last, AIMessage) and last.tool_calls:
                            for tc in last.tool_calls:
                                tool_steps.append(
                                    {
                                        "name": tc["name"],
                                        "args": json.dumps(tc.get("args", {}), ensure_ascii=False),
                                        "result": None,
                                    }
                                )
                        elif isinstance(last, ToolMessage):
                            recent_tool_msgs: list[ToolMessage] = []
                            for m in reversed(history):
                                if isinstance(m, ToolMessage):
                                    recent_tool_msgs.insert(0, m)
                                else:
                                    break
                            offset = len(tool_steps) - len(recent_tool_msgs)
                            for i, tm in enumerate(recent_tool_msgs):
                                idx = offset + i
                                if 0 <= idx < len(tool_steps):
                                    tool_steps[idx]["result"] = message_content_to_text(tm.content)[:300]

                    if tool_steps:
                        if not chat_view or chat_view[-1].get("role") != "assistant":
                            chat_view = list(chat_view)
                            chat_view.append({"role": "assistant", "content": ""})
                        chat_view[-1] = {
                            "role": "assistant",
                            "content": _format_tool_steps(tool_steps, "running"),
                        }
                    latest_chat_view = chat_view
                elif event["phase"] == "done":
                    final_text = event.get("final_response", "") or extract_final_response(history)
                    chat_view = list(latest_chat_view if latest_chat_view is not None else chat_view)
                    if not chat_view or chat_view[-1].get("role") != "assistant":
                        chat_view.append({"role": "assistant", "content": final_text})
                    else:
                        if tool_steps:
                            chat_view[-1] = {
                                "role": "assistant",
                                "content": _format_tool_steps(tool_steps, "done") + final_text,
                            }
                        else:
                            chat_view[-1] = {"role": "assistant", "content": final_text}
                    latest_chat_view = chat_view

                snap = runtime.snapshot()
                yield "", (latest_chat_view or chat_view), history, snap

        refresh_btn.click(
            refresh_all,
            inputs=[],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, internal_state, state_json],
        )
        gen_uuid_btn.click(
            generate_session_and_switch,
            inputs=[],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, internal_state, state_json],
        )
        switch_btn.click(
            lambda sid: switch_session(sid, False),
            inputs=[session_id_input],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, internal_state, state_json],
        )
        switch_new_btn.click(
            lambda sid: switch_session(sid, True),
            inputs=[session_id_input],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, internal_state, state_json],
        )
        use_selected_btn.click(
            use_selected,
            inputs=[session_dropdown],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, internal_state, state_json],
        )
        new_btn.click(
            create_new_session,
            inputs=[],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, internal_state, state_json],
        )
        send_btn.click(
            send_message,
            inputs=[msg, force_skill_dd],
            outputs=[msg, chat, internal_state, state_json],
        )
        msg.submit(
            send_message,
            inputs=[msg, force_skill_dd],
            outputs=[msg, chat, internal_state, state_json],
        )
        demo.load(
            refresh_all,
            inputs=[],
            outputs=[session_id_input, session_dropdown, session_history_md, chat, internal_state, state_json],
        )
    return demo


def main() -> None:
    runtime = AgentRuntime(SessionStore(SESSIONS_DIR))
    host = os.getenv("S_FULL_LG_MERGED_HOST", "127.0.0.1")
    port = int(os.getenv("S_FULL_LG_MERGED_PORT", "8772"))
    _api_logger.info("Agent starting | model=%s | host=%s | port=%d | workdir=%s", MODEL, host, port, WORKDIR)
    demo = build_webui(runtime)
    print(f"WebUI running at http://{host}:{port}")
    demo.launch(server_name=host, server_port=port, share=False)


if __name__ == "__main__":
    main()
