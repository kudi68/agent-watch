#!/usr/bin/env python3
"""agent-watch v8: Textual dashboard for reliable run status and quota monitoring."""

import argparse
import calendar
import copy
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import CancelledError
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from rich.markup import escape
from rich.text import Text

# Textual imports (conditional for --once flag speed)
try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widgets import (
        DataTable, Input, Label, OptionList, Select, Static, TabbedContent, TabPane,
    )
    from textual.widgets.option_list import Option
except ImportError:
    App = object  # type: ignore

def _env_path(name: str, default: Path) -> Path:
    """Read a path setting from the environment, expanding ~ when present."""
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


# All locations are configurable so the dashboard works outside the author's Mac.
# AGENT_WATCH_RUNS_ROOT is the base directory used for relative paths in the TSVs.
ROOT_DIR = _env_path(
    "AGENT_WATCH_RUNS_ROOT", Path.home() / "Projects" / "night"
)
DEFAULT_RUNS_TSV = _env_path("AGENT_WATCH_RUNS", ROOT_DIR / "agent-runs.tsv")
DEFAULT_DONE_TSV = _env_path("AGENT_WATCH_DONE", ROOT_DIR / "agent-runs-done.tsv")
DEFAULT_SESSIONS_PATH = _env_path(
    "AGENT_WATCH_CODEX_SESSIONS", Path.home() / ".codex" / "sessions"
)
ARCHITECTURE_PATH = _env_path(
    "AGENT_WATCH_ARCH", Path(__file__).with_name("arch.json")
)
MAGPIE_COMMAND = os.environ.get("AGENT_WATCH_MAGPIE", "magpie")


def load_architecture_data(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and validate the editable architecture graph from JSON."""
    source = Path(path) if path is not None else ARCHITECTURE_PATH
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("arch.json must be an object")
    projects = data.get("projects")
    architectures = data.get("architectures")
    if not isinstance(projects, dict) or not projects:
        raise ValueError("arch.json must define at least one project")
    if not isinstance(architectures, dict) or "now" not in architectures:
        raise ValueError("arch.json must define architectures.now")

    for project_id, project in projects.items():
        if not isinstance(project, dict) or not project.get("label"):
            raise ValueError(f"project {project_id!r} has no label")
        for key in ("aliases", "match_keywords", "path_keywords", "match_patterns"):
            values = project.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"project {project_id!r} has invalid {key}")
            if key == "match_patterns":
                for pattern in values:
                    re.compile(pattern)
        default_view = project.get("default_view", "now")
        if default_view not in architectures:
            raise ValueError(f"project {project_id!r} uses unknown architecture {default_view!r}")

    for view_id, view in architectures.items():
        if not isinstance(view, dict):
            raise ValueError(f"architecture {view_id!r} is invalid")
        nodes = view.get("nodes")
        layers = view.get("layers")
        edges = view.get("edges")
        if not isinstance(nodes, dict) or not isinstance(layers, list) or not layers:
            raise ValueError(f"architecture {view_id!r} has invalid nodes or layers")
        if not all(isinstance(node, dict) for node in nodes.values()):
            raise ValueError(f"architecture {view_id!r} has invalid node definitions")
        if edges is not None and not isinstance(edges, list):
            raise ValueError(f"architecture {view_id!r} has invalid edges")
        layer_by_node = {}
        for layer_index, layer in enumerate(layers):
            if not isinstance(layer, list) or not layer:
                raise ValueError(f"architecture {view_id!r} has an empty layer")
            for node_id in layer:
                if node_id not in nodes or node_id in layer_by_node:
                    raise ValueError(f"architecture {view_id!r} has an invalid node: {node_id!r}")
                layer_by_node[node_id] = layer_index
        for edge in edges or []:
            if (
                not isinstance(edge, list)
                or len(edge) not in (2, 3)
                or edge[0] not in layer_by_node
                or edge[1] not in layer_by_node
                or abs(layer_by_node[edge[1]] - layer_by_node[edge[0]]) != 1
            ):
                raise ValueError(f"architecture {view_id!r} has an invalid edge: {edge!r}")
    return data


FALLBACK_ARCHITECTURE = {
    "projects": {"other": {"label": "其他", "default_view": "now"}},
    "architectures": {
        "now": {
            "label": "現在", "caption": "架構資料無法讀取", "layers": [["operator"]],
            "nodes": {"operator": {"label": "Operator", "kind": "actor"}}, "edges": [],
        }
    },
}
ARCHITECTURE_DATA: Dict[str, Any] = {}
ARCHITECTURE_ERRORS: List[str] = []


def ensure_architecture_data() -> None:
    """Load at runtime, keeping a usable graph and one warning on bad input."""
    if ARCHITECTURE_DATA:
        return
    try:
        data = load_architecture_data()
    except (OSError, ValueError, TypeError, KeyError, re.error) as exc:
        data = copy.deepcopy(FALLBACK_ARCHITECTURE)
        ARCHITECTURE_ERRORS[:] = [f"arch.json 無法讀取，使用預設架構：{exc}"]
    ARCHITECTURE_DATA.update(data)
PROJECT_OTHER = "other"
RECENT_DONE_SECONDS = 2 * 60 * 60
# Tolerate small clock skew so a just-finished task is not aged into history.
FUTURE_SKEW_TOLERANCE_SECONDS = 5 * 60
MAGPIE_TIMEOUT_SECONDS = 10
QUOTA_REFRESH_SECONDS = 60
QUOTA_BURN_AHEAD_PERCENT = 15
QUOTA_CRITICAL_PERCENT = 85
# SPEC §3.3: a question left unanswered for 30 minutes turns red and notifies.
QUESTION_OVERDUE_SECONDS = 30 * 60
def _stale_attention_seconds_from_env() -> int:
    try:
        hours = float(os.environ.get("AGENT_WATCH_STALE_HOURS", "24"))
    except (TypeError, ValueError):
        return 24 * 60 * 60
    seconds = hours * 60 * 60
    if not math.isfinite(seconds) or seconds <= 0:
        return 24 * 60 * 60
    return max(1, int(seconds))

STALE_ATTENTION_SECONDS = _stale_attention_seconds_from_env()
STATE_TASK_RETENTION_SECONDS = 30 * 24 * 60 * 60
QUESTION_FILENAME = "QUESTION.md"
ANSWER_FILENAME = "ANSWER.md"
TERMINAL_TAIL_LINES = 400
TERMINAL_TAIL_BYTES = 512 * 1024
QUOTA_PROVIDER_NAMES = {
    "codex": "Codex",
    "claude": "Claude",
    "opencode-go": "OpenCode Go",
    "openrouter": "OpenRouter",
}


def keyword_matches(keyword: str, text: str) -> bool:
    token = keyword.casefold()
    return bool(token) and re.search(
        rf"(?<![a-z0-9]){re.escape(token)}" + (r"(?![a-z0-9])" if token[-1:].isalnum() else ""),
        text.casefold(),
    ) is not None


def resolve_project(value: str) -> Tuple[str, str]:
    ensure_architecture_data()
    for project_id, config in ARCHITECTURE_DATA["projects"].items():
        if value.casefold() in [project_id.casefold()] + [
            alias.casefold() for alias in config.get("aliases", [])
        ]:
            return project_id, ""
    return PROJECT_OTHER, f"未知專案：{value}"


def infer_project(
    name: str,
    log_path: Optional[Path] = None,
    last_path: Optional[Path] = None,
) -> str:
    """Map a task to the configured project table; unknown tasks use other."""
    ensure_architecture_data()
    name_text = name.casefold()
    path_text = " ".join(
        str(path) for path in (log_path, last_path) if path is not None
    ).casefold()
    for project_id, config in ARCHITECTURE_DATA.get("projects", {}).items():
        if not isinstance(config, dict):
            continue
        name_keywords = config.get("match_keywords", [])
        path_keywords = config.get("path_keywords", [])
        if any(
            isinstance(keyword, str) and keyword_matches(keyword, name_text)
            for keyword in name_keywords
        ):
            return project_id
        if any(re.search(pattern, name_text + " " + path_text, re.IGNORECASE) for pattern in config.get("match_patterns", [])):
            return project_id
        if any(
            isinstance(keyword, str) and keyword_matches(keyword, path_text)
            for keyword in path_keywords
        ):
            return project_id
    return PROJECT_OTHER


def project_label(project_id: str) -> str:
    ensure_architecture_data()
    config = ARCHITECTURE_DATA.get("projects", {}).get(project_id)
    if isinstance(config, dict) and config.get("label"):
        return str(config["label"])
    return "其他" if project_id == PROJECT_OTHER else project_id


def project_sort_key(project_id: str) -> Tuple[int, str]:
    ensure_architecture_data()
    configured = list(ARCHITECTURE_DATA.get("projects", {}).keys())
    if project_id in configured:
        return configured.index(project_id), project_label(project_id)
    if project_id == PROJECT_OTHER:
        return len(configured), "其他"
    return len(configured) + 1, project_label(project_id)


def group_agents_by_project(agents: List["AgentInfo"]) -> Dict[str, List["AgentInfo"]]:
    groups: Dict[str, List[AgentInfo]] = {}
    for agent in agents:
        groups.setdefault(agent.project or PROJECT_OTHER, []).append(agent)
    return {
        project_id: groups[project_id]
        for project_id in sorted(groups, key=project_sort_key)
    }

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
TIMESTAMP_RE = re.compile(
    r"\b(20\d{2}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
CLOCK_RE = re.compile(r"\b(\d{2}:\d{2}:\d{2})\b")

# Noise patterns to filter out
NOISE_RE = re.compile(
    r"ERROR codex_api|^hook:|disable_response_storage|failed to connect to websocket|Upgrade Required|responses_websocket",
    re.IGNORECASE,
)

INVALID_MODELS = {
    "model",
    "models",
    "none",
    "null",
    "true",
    "false",
    "inherit",
    "undefined",
    "str",
    "string",
    "optional",
    "union",
    "snapshot.model",
    "self.model",
    "",
}
UNKNOWN_IDENTITY = "未知"
HEADER_METADATA_EVENTS = {
    "metadata",
    "run_metadata",
    "session_meta",
    "session.start",
    "init",
    "system",
}
MESSAGE_EVENTS = {"user", "assistant", "message", "output", "result", "step", "tool"}
METADATA_CONTAINERS = ("metadata", "meta", "runtime", "session", "init", "payload", "info")
HARNESS_NAMES = {
    "codex": "Codex",
    "claude": "Claude Code",
    "claude-code": "Claude Code",
    "opencode": "OpenCode",
    "pi": "Pi Agent",
    "pi-agent": "Pi Agent",
    "agy": "agy",
    "gemini": "Gemini CLI",
}
COMMANDS_WITH_MODEL_FOLLOWING_OPTIONS = {
    "--add-dir",
    "--approval-mode",
    "--config",
    "--cwd",
    "--mode",
    "--output-format",
    "--permission-mode",
    "--profile",
    "--provider",
    "--sandbox",
    "-c",
}
KNOWN_SUBCOMMANDS = {"exec", "run", "chat"}


def clean_text(text: str) -> str:
    """Strip ANSI sequences and carriage returns, keeping clean readable text."""
    if not text:
        return ""
    text = ANSI_RE.sub("", text)
    return text.replace("\r", "").replace("\t", "    ").strip()


def is_noise(line: str) -> bool:
    """Return True if line contains known ignorable noise."""
    return bool(NOISE_RE.search(line))


def extract_model_from_line(line: str) -> Optional[str]:
    """Extract a model only from a structured JSON record."""
    clean = clean_text(line)
    if not clean or not clean.startswith("{"):
        return None

    try:
        data = json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    _, model = _identity_from_metadata_record(data)
    if model != UNKNOWN_IDENTITY:
        return model
    candidate = data.get("model")
    if candidate is not None and _event_name(data) not in MESSAGE_EVENTS:
        return _model_value(candidate)
    return None


def _event_name(data: Dict[str, Any]) -> str:
    return str(data.get("type") or data.get("event") or data.get("kind") or "").lower()


def _harness_value(value: Any) -> str:
    if value is None:
        return UNKNOWN_IDENTITY
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "未知"}:
        return UNKNOWN_IDENTITY
    return HARNESS_NAMES.get(text.lower().replace("_", "-"), text)


def _model_value(value: Any) -> str:
    if value is None:
        return UNKNOWN_IDENTITY
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "未知", "none", "null"}:
        return UNKNOWN_IDENTITY
    return text


def _identity_from_metadata_record(data: Dict[str, Any]) -> Tuple[str, str]:
    """Read identity fields from explicit metadata objects, never message bodies."""
    event = _event_name(data)
    if event in MESSAGE_EVENTS:
        return UNKNOWN_IDENTITY, UNKNOWN_IDENTITY

    containers = [data]
    for key in METADATA_CONTAINERS:
        value = data.get(key)
        if isinstance(value, dict):
            containers.append(value)

    harness = UNKNOWN_IDENTITY
    model = UNKNOWN_IDENTITY
    for item in containers:
        if item is data and event and event not in HEADER_METADATA_EVENTS:
            continue
        if item is not data and event and event not in HEADER_METADATA_EVENTS:
            continue
        if harness == UNKNOWN_IDENTITY and item.get("harness") is not None:
            harness = _harness_value(item.get("harness"))
        if model == UNKNOWN_IDENTITY and item.get("model") is not None:
            model = _model_value(item.get("model"))
    if event == "system" and data.get("subtype") == "init":
        harness = "Claude Code"
    return harness, model


def _parse_header_comment(line: str) -> Tuple[str, str]:
    match = re.match(r"^#\s*(harness|model)\s*[:=]\s*(.*?)\s*$", line, re.IGNORECASE)
    if not match:
        return UNKNOWN_IDENTITY, UNKNOWN_IDENTITY
    key, value = match.group(1).lower(), match.group(2)
    if key == "harness":
        return _harness_value(value), UNKNOWN_IDENTITY
    return UNKNOWN_IDENTITY, _model_value(value)


def read_log_metadata(path: Path) -> Tuple[str, str]:
    """Read only explicit comment or JSON metadata at the start of a log."""
    harness = UNKNOWN_IDENTITY
    model = UNKNOWN_IDENTITY
    try:
        with path.open("rb") as stream:
            prefix = stream.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return harness, model

    for line in prefix.splitlines()[:32]:
        clean = clean_text(line)
        if not clean:
            continue
        if clean.startswith("#"):
            parsed_harness, parsed_model = _parse_header_comment(clean)
            if parsed_harness != UNKNOWN_IDENTITY:
                harness = parsed_harness
            if parsed_model != UNKNOWN_IDENTITY:
                model = parsed_model
            continue
        if not clean.startswith("{"):
            break
        try:
            data = json.loads(clean)
        except (json.JSONDecodeError, ValueError):
            break
        if not isinstance(data, dict):
            break
        parsed_harness, parsed_model = _identity_from_metadata_record(data)
        if parsed_harness != UNKNOWN_IDENTITY:
            harness = parsed_harness
        if parsed_model != UNKNOWN_IDENTITY:
            model = parsed_model
        event = _event_name(data)
        if (
            parsed_harness == UNKNOWN_IDENTITY
            and parsed_model == UNKNOWN_IDENTITY
            and event not in HEADER_METADATA_EVENTS
        ):
            break
    return harness, model


def parse_command_identity(command: str) -> Tuple[str, str]:
    """Read a known CLI executable and its option flags before the prompt."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return UNKNOWN_IDENTITY, UNKNOWN_IDENTITY
    if not tokens:
        return UNKNOWN_IDENTITY, UNKNOWN_IDENTITY

    program_index = 0
    program = Path(tokens[program_index]).name.lower().removesuffix(".exe")
    if program in {"node", "bun", "deno"} and len(tokens) > 1:
        program_index = 1
        program = Path(tokens[program_index]).name.lower().removesuffix(".exe")
        program = program.removesuffix(".js").removesuffix(".mjs")
    harness = HARNESS_NAMES.get(program, UNKNOWN_IDENTITY)
    if harness == UNKNOWN_IDENTITY:
        return harness, UNKNOWN_IDENTITY

    model = UNKNOWN_IDENTITY
    args = tokens[program_index + 1 :]
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if token in KNOWN_SUBCOMMANDS:
            index += 1
            continue
        if token in {"-m", "--model"}:
            if index + 1 < len(args) and not args[index + 1].startswith("-"):
                model = _model_value(args[index + 1])
            break
        if token.startswith("-m="):
            model = _model_value(token.split("=", 1)[1])
            break
        if token.startswith("--model="):
            model = _model_value(token.split("=", 1)[1])
            break
        if token in COMMANDS_WITH_MODEL_FOLLOWING_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    return harness, model


def resolve_run_identity(
    log_path: Path,
    pid: Optional[int],
    harness: Optional[str],
    model: Optional[str],
    process_alive: Optional[bool],
) -> Tuple[str, str]:
    """Prefer TSV fields, then structured log metadata; never inspect process args."""
    resolved_harness = _harness_value(harness)
    resolved_model = _model_value(model)

    if resolved_harness == UNKNOWN_IDENTITY or resolved_model == UNKNOWN_IDENTITY:
        log_harness, log_model = read_log_metadata(log_path)
        if resolved_harness == UNKNOWN_IDENTITY:
            resolved_harness = log_harness
        if resolved_model == UNKNOWN_IDENTITY:
            resolved_model = log_model
    return resolved_harness, resolved_model


def is_valid_model(name: str) -> bool:
    """Check if model name looks like a real model rather than code variable or noise."""
    s = name.strip()
    low = s.lower()
    if not s or low in INVALID_MODELS:
        return False
    if "." in s:
        parts = s.split(".")
        if all(p.isalpha() for p in parts):
            return False
    if "=" in s or " " in s:
        return False
    return True


def parse_timestamp(text: str) -> Optional[float]:
    """Parse ISO timestamp from text into POSIX timestamp float."""
    match = TIMESTAMP_RE.search(text)
    if not match:
        return None
    val = match.group(1).replace("/", "-")
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", val):
        val = val[:-5] + val[-5:-2] + ":" + val[-2:]
    try:
        parsed = datetime.fromisoformat(val)
        if parsed.tzinfo:
            return parsed.timestamp()
        return parsed.astimezone().timestamp()
    except (ValueError, OSError):
        return None


def format_elapsed(start_time: Optional[float], now: float) -> str:
    """Format elapsed time as HH:MM:SS or '時間未知'."""
    if start_time is None or start_time <= 0:
        return "時間未知"
    diff = max(0, int(now - start_time))
    h, rem = divmod(diff, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def summarize_jsonl_step(data: dict) -> str:
    """Produce a concise human-readable summary for an agy jsonl event."""
    kind = data.get("type")
    if kind == "system":
        model = data.get("model", "")
        return f"初始化 Claude ({model})" if model else "Claude 系統事件"
    if kind in ("assistant", "user"):
        message = data.get("message")
        content = message.get("content", []) if isinstance(message, dict) else []
        if isinstance(content, str):
            return clean_text(content)
        summaries = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                summaries.append(clean_text(block["text"]))
            elif block.get("type") == "tool_use":
                params = json.dumps(block.get("input", {}), ensure_ascii=False, separators=(",", ":"))
                summaries.append(f"工具：{block.get('name', '未知')} {params[:100]}")
        return " · ".join(summaries) or ("接收使用者指令" if kind == "user" else "")
    if kind == "result":
        result = clean_text(str(data.get("result") or ""))
        failed = data.get("is_error") is True or str(data.get("subtype", "")).startswith("error")
        return f"{'失敗' if failed else '完成'}：{result or data.get('subtype', '')}"
    part = data.get("part")
    if isinstance(part, dict):
        if isinstance(part.get("text"), str):
            return clean_text(part["text"])
        if part.get("tool"):
            return f"工具：{part['tool']}"
    if kind in ("item.completed", "item.updated"):
        item = data.get("item", {})
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            return clean_text(item["text"])
    event = data.get("event", "")
    if event == "init":
        model = data.get("init", {}).get("model", "")
        return f"初始化工作區 ({model})" if model else "初始化工作區"
    if event == "step_update":
        su = data.get("step_update", {})
        stype = su.get("step_type", "")
        state = su.get("state", "")
        if stype == "tool":
            tname = su.get("tool_name") or su.get("tool_info", {}).get("name", "tool")
            params = su.get("tool_info", {}).get("parameters", {})
            param_summary = ""
            if "Path" in params:
                param_summary = f": {Path(params['Path']).name}"
            elif "CommandLine" in params:
                cmd = str(params["CommandLine"]).split()
                param_summary = f": {' '.join(cmd[:3])}"
            status_label = "完成" if state == "DONE" else "執行中"
            return f"{status_label}工具 {tname}{param_summary}"
        elif stype == "agent_response":
            return "產生思考與回應"
        elif stype == "user_input":
            return "接收使用者指令"
        return f"步驟更新 ({stype} {state})"
    return clean_text(str(data.get("text") or data.get("message") or data.get("type") or event or "日誌事件"))[:120]


def record_timestamp(data: Dict[str, Any]) -> Optional[float]:
    value = data.get("timestamp")
    if isinstance(value, (float, int)):
        return float(value) / 1000 if value > 10**11 else float(value)
    if value:
        return parse_timestamp(str(value))
    return None


def question_key_value(line: str) -> Optional[Tuple[str, str]]:
    """Parse a `key: value` line from QUESTION.md / ANSWER.md."""
    match = re.match(r"^([A-Za-z_]+)\s*[:：]\s*(.*)$", line.strip())
    if not match:
        return None
    return match.group(1).casefold(), match.group(2).strip()


def parse_question(text: str) -> Dict[str, Any]:
    """Parse SPEC §3.3 QUESTION.md; `error` keeps unreadable files answerless."""
    question: Dict[str, Any] = dict(
        id="", asked_at=None, asked_at_text="", question="", options=[],
        raw=text, error="",
    )
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].strip().upper().startswith("# QUESTION"):
        question["error"] = "缺少 # QUESTION 標題"
        return question

    values: Dict[str, str] = {}
    options: List[Tuple[str, str]] = []
    option_error = ""
    in_options = False
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if in_options:
            match = re.match(r"^[-*]\s*([^:：\s]+)\s*[:：]\s*(.+?)\s*$", stripped)
            if match:
                options.append((match.group(1), match.group(2)))
                continue
            if question_key_value(stripped) is None:
                option_error = option_error or "選項格式錯誤：" + stripped
                continue
            in_options = False
        pair = question_key_value(stripped)
        if pair is None:
            continue
        key, value = pair
        if key == "options":
            in_options = True
            if value:
                option_error = option_error or "options 必須另起一行列出"
        else:
            values[key] = value

    question["id"] = values.get("id", "")
    question["asked_at_text"] = values.get("asked_at", "")
    question["question"] = values.get("question", "")
    question["options"] = options
    codes = [code for code, _ in options]
    if not question["id"]:
        question["error"] = "缺少 id"
    elif not question["question"]:
        question["error"] = "缺少 question"
    elif not options:
        question["error"] = "缺少 options 或沒有可用選項"
    elif len(set(codes)) != len(codes):
        question["error"] = "選項代號重複"
    elif option_error:
        question["error"] = option_error
    if question["asked_at_text"]:
        question["asked_at"] = parse_timestamp(question["asked_at_text"])
    return question


def parse_answer(text: str) -> Dict[str, Any]:
    """Parse ANSWER.md; a mismatched id is treated as no answer."""
    answer: Dict[str, Any] = dict(id="", choice="", note="", answered_at="", error="")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].strip().upper().startswith("# ANSWER"):
        answer["error"] = "缺少 # ANSWER 標題"
        return answer
    for line in lines[1:]:
        if line.strip().startswith("#"):
            continue
        pair = question_key_value(line)
        if pair is None:
            continue
        key, value = pair
        if key in ("id", "choice", "note", "answered_at"):
            answer[key] = value
    if not answer["id"]:
        answer["error"] = "缺少 id"
    elif not answer["choice"]:
        answer["error"] = "缺少 choice"
    return answer


def read_task_question(directory: Path) -> Optional[Dict[str, Any]]:
    """Read QUESTION.md beside the run log; None when the task has no question."""
    path = Path(directory) / QUESTION_FILENAME
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None
    question = parse_question(text)
    question["path"] = str(path)
    question["mtime"] = info.st_mtime
    asked = question.get("asked_at")
    question["asked_at_ts"] = asked if asked is not None else info.st_mtime
    question["asked_at_estimated"] = asked is None
    answer_text = ""
    try:
        answer_path = path.with_name(ANSWER_FILENAME)
        if stat.S_ISREG(answer_path.stat().st_mode):
            answer_text = answer_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        answer_text = ""
    answer = parse_answer(answer_text) if answer_text.strip() else None
    question["answer"] = answer
    question["answered"] = bool(
        question["id"] and answer is not None and not answer["error"]
        and answer["id"] == question["id"]
    )
    return question


def question_is_active(question: Optional[Dict[str, Any]],
                       completion_time: Optional[float] = None) -> bool:
    """An unanswered question is attention until a matching ANSWER.md exists."""
    if not question or question.get("answered"):
        return False
    asked = question.get("asked_at_ts") or question.get("mtime") or 0
    if completion_time is not None and asked <= completion_time:
        # A leftover question older than the finished run belongs to an older run.
        return False
    return True


def question_wait_seconds(question: Optional[Dict[str, Any]], now: float) -> int:
    if not question:
        return 0
    asked = question.get("asked_at_ts") or question.get("mtime") or now
    return max(0, int(now - asked))


def format_question_wait(seconds: int) -> str:
    minutes = max(0, int(seconds)) // 60
    if minutes < 60:
        return "已等 %dm" % minutes
    hours, minutes = divmod(minutes, 60)
    return "已等 %dh%02dm" % (hours, minutes)


def write_answer_file(directory: Path, question: Dict[str, Any], choice: str,
                      note: str = "", now: Optional[float] = None) -> Tuple[bool, str]:
    """Write ANSWER.md atomically with the question's id (SPEC §3.3)."""
    if question.get("error"):
        return False, "問題格式錯誤，不能回答"
    codes = [code for code, _ in question.get("options", [])]
    if not choice or choice not in codes:
        return False, "選項不存在"
    if not question.get("id"):
        return False, "問題缺少 id，不能回答"
    stamp = datetime.fromtimestamp(now if now is not None else time.time()).astimezone()
    clean_note = " ".join(str(note or "").splitlines()).strip()
    content = (
        "# ANSWER\n"
        "id: %s\n"
        "answered_at: %s\n"
        "choice: %s\n"
        "note: %s\n"
    ) % (question["id"], stamp.isoformat(timespec="seconds"), choice, clean_note)
    target = Path(directory) / ANSWER_FILENAME
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(Path(directory)),
            prefix="." + ANSWER_FILENAME + "-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        return False, "ANSWER.md 寫入失敗：" + str(exc)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return True, str(target)


def read_log_tail(path: Path, limit: int = TERMINAL_TAIL_LINES) -> List[str]:
    """Read only the log tail, like `tail -n`, without scanning the whole file."""
    if str(path) == "/dev/null":
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            block = min(size, TERMINAL_TAIL_BYTES)
            stream.seek(size - block)
            data = stream.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-limit:]


def humanize_log_line(line: str) -> str:
    """Turn one tail line into plain language; JSON events never show raw keys."""
    cleaned = clean_text(line)
    if not cleaned or is_noise(cleaned):
        return ""
    stripped = cleaned.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return " ".join(summarize_jsonl_step(data).splitlines()).strip()
    return " ".join(cleaned.splitlines()).strip()


def humanize_log_lines(lines: List[str]) -> List[str]:
    result = []
    for line in lines:
        summary = humanize_log_line(line)
        if summary:
            result.append(summary)
    return result


@dataclass
class AgentInfo:
    name: str
    log_path: Path
    last_path: Path
    status: str  # running, waiting, complete, failed, blocked, partial, no_report, error
    model: str
    start_time: Optional[float]
    elapsed: str
    latest: str
    log_lines: List[str] = field(default_factory=list)
    log_mtime: float = 0.0
    is_done_file: bool = False
    key: str = ""
    error: str = ""
    last_content: str = ""
    pid: Optional[int] = None
    completion_time: Optional[float] = None
    stage: str = ""
    harness: str = UNKNOWN_IDENTITY
    project: str = PROJECT_OTHER
    start_source: str = ""
    elapsed_estimated: bool = False
    question: Optional[Dict[str, Any]] = None
    note: str = ""
    superseded_hint: str = ""
    process_alive: Optional[bool] = None


# Sentinel so an injected reader and the built-in `ps` reader stay distinct:
# passing None still disables the PID source explicitly.
DEFAULT_PROCESS_START_READER = object()


class LogParser:
    """Parse changed log bytes only; log text never determines the final status."""

    MAX_INITIAL_READ = 2 * 1024 * 1024
    START_SCAN_BYTES = 64 * 1024
    PID_START_CACHE_LIMIT = 512

    def __init__(
        self,
        process_start_reader: Any = DEFAULT_PROCESS_START_READER,
    ):
        self.cache: Dict[str, dict] = {}
        # SPEC §4.1 layer 2: a live PID's `ps -o lstart=` start time sits
        # between the TSV's started_at and the log/birthtime estimate.
        if process_start_reader is DEFAULT_PROCESS_START_READER:
            process_start_reader = read_process_start
        self.process_start_reader: Optional[Callable[[int], Optional[float]]] = process_start_reader
        self.pid_start_cache: Dict[int, Optional[float]] = {}
        self.result_events: Dict[str, Tuple[Optional[float], str]] = {}
        # The latest init event bounds the CURRENT run. Claude writes its
        # REPORT.md before the final log records, so this (not the log mtime)
        # decides whether a report belongs to this invocation.
        self.init_events: Dict[str, Optional[float]] = {}

    def pid_start_time(self, pid: Optional[int]) -> Optional[float]:
        """Return a live PID's start time; a successful lookup is cached per PID."""
        if pid is None or self.process_start_reader is None:
            return None
        if pid in self.pid_start_cache:
            return self.pid_start_cache[pid]
        try:
            value = self.process_start_reader(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            value = None
        # A start after `now` cannot describe an already-running process;
        # treat it as unusable rather than showing a negative elapsed time.
        if value is not None and (not isinstance(value, (int, float)) or value > time.time() + 60):
            value = None
        if value is not None:
            # A process's start time never changes, so cache the hit; a miss is
            # retried on the next poll in case `ps` was only briefly unavailable.
            if len(self.pid_start_cache) >= self.PID_START_CACHE_LIMIT:
                self.pid_start_cache.clear()
            self.pid_start_cache[pid] = value
        return value

    @staticmethod
    def _summarize(raw_line: bytes, mtime: float) -> Tuple[Optional[float], Optional[str], Optional[str]]:
        line = clean_text(raw_line.decode("utf-8", errors="replace"))
        if not line:
            return None, None, None

        timestamp = parse_timestamp(line) if re.match(r"^20\d{2}[-/]", line) else None
        model = None
        if is_noise(line):
            return timestamp, model, None

        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                summary = summarize_jsonl_step(data) if isinstance(data, dict) else line
                if isinstance(data, dict):
                    timestamp = record_timestamp(data)
            except (json.JSONDecodeError, ValueError):
                summary = line
        else:
            summary = line

        if not summary:
            return timestamp, model, None
        clock = time.strftime("%H:%M:%S", time.localtime(timestamp)) if timestamp is not None else ""
        return timestamp, model, f"{clock}  {summary}" if clock else summary

    def parse(self, path: Path) -> Tuple[Optional[float], str, List[str], float, str]:
        if str(path) == "/dev/null":
            return None, UNKNOWN_IDENTITY, [], 0.0, ""

        cache_key = str(path.resolve())
        try:
            st = path.stat()
            if not stat.S_ISREG(st.st_mode):
                self.init_events.pop(cache_key, None)
                return None, UNKNOWN_IDENTITY, [], 0.0, "不是一般檔案"
        except OSError as e:
            self.init_events.pop(cache_key, None)
            return None, UNKNOWN_IDENTITY, [], 0.0, e.strerror or "無法讀取"

        identity = (st.st_dev, st.st_ino)
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
        cached = self.cache.get(cache_key)
        unchanged = (
            cached is not None
            and cached["identity"] == identity
            and cached["size"] == st.st_size
            and cached["mtime_ns"] == mtime_ns
        )
        if unchanged:
            return (
                cached["start_time"],
                cached["model"],
                list(cached["recent"]),
                st.st_mtime,
                "",
            )

        append_only = (
            cached is not None
            and cached["identity"] == identity
            and st.st_size > cached["size"]
        )
        start_time = cached["start_time"] if append_only else None
        model = cached["model"] if append_only else UNKNOWN_IDENTITY
        recent = list(cached["recent"]) if append_only else []
        pending = cached["pending"] if append_only else b""
        if not append_only:
            self.result_events.pop(cache_key, None)
            self.init_events.pop(cache_key, None)

        try:
            with path.open("rb") as stream:
                if append_only:
                    stream.seek(cached["size"])
                    raw = stream.read()
                else:
                    if st.st_size:
                        stream.seek(0)
                        prefix = stream.read(min(st.st_size, self.START_SCAN_BYTES))
                        # Only a log record's own time is evidence of its start.
                        # Dates quoted inside prompts are unrelated to this run.
                        for text_line in prefix.decode("utf-8", errors="replace").splitlines():
                            if re.match(r"^\s*20\d{2}[-/]\d{2}[-/]\d{2}[T ]", text_line):
                                start_time = parse_timestamp(text_line)
                                break
                            try:
                                record = json.loads(text_line)
                            except ValueError:
                                continue
                            if isinstance(record, dict) and record_timestamp(record) is not None:
                                start_time = record_timestamp(record)
                                break
                        if start_time is None:
                            start_time = getattr(st, "st_birthtime", None)

                    offset = max(0, st.st_size - self.MAX_INITIAL_READ)
                    stream.seek(offset)
                    raw = stream.read()
                    if offset:
                        first_newline = raw.find(b"\n")
                        raw = raw[first_newline + 1 :] if first_newline >= 0 else b""
                    pending = b""

            combined = pending + raw
            lines = combined.split(b"\n")
            if combined.endswith(b"\n"):
                pending = b""
            else:
                pending = lines.pop() if lines else combined

            for raw_line in lines:
                ts, _, summary = self._summarize(raw_line.rstrip(b"\r"), st.st_mtime)
                if start_time is None and ts is not None:
                    start_time = ts
                try:
                    record = json.loads(raw_line)
                except ValueError:
                    record = None
                if isinstance(record, dict) and record.get("type") == "result":
                    failed = record.get("is_error") is True or str(record.get("subtype", "")).startswith("error")
                    self.result_events[cache_key] = (record_timestamp(record) or st.st_mtime, "failed" if failed else "complete")
                elif summary:
                    self.result_events.pop(cache_key, None)
                is_init_event = (
                    isinstance(record, dict) and record.get("type") == "system" and record.get("subtype") == "init"
                ) or re.search(rb"\bsession start\b", raw_line, re.IGNORECASE) is not None
                if is_init_event:
                    # A new init starts a new run. Without its timestamp the
                    # boundary is unknown, so do not keep an older init time.
                    self.init_events[cache_key] = ts
                    if ts is not None:
                        start_time = ts
                        self.result_events.pop(cache_key, None)
                if summary:
                    recent.append(summary)

        except OSError as e:
            return None, UNKNOWN_IDENTITY, [], 0.0, e.strerror or "讀取失敗"

        recent = recent[-12:]
        self.cache[cache_key] = {
            "identity": identity,
            "size": st.st_size,
            "mtime_ns": mtime_ns,
            "start_time": start_time,
            "model": model,
            "recent": recent,
            "pending": pending,
        }
        return start_time, model, recent, st.st_mtime, ""


def read_last_file(path: Path) -> Tuple[str, float, bool, str]:
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            return "", 0.0, False, "結果檔不是一般檔案"
        content = path.read_text(encoding="utf-8", errors="replace")
        return content, st.st_mtime, True, ""
    except FileNotFoundError:
        return "", 0.0, False, ""
    except OSError as e:
        return "", 0.0, False, e.strerror or "無法讀取結果檔"


def resolve_result_path(path: Path) -> Path:
    """Use the newest LAST.md or REPORT.md beside the configured result path."""
    candidates = [path]
    if path.name.lower() == "last.md":
        candidates.append(path.with_name("REPORT.md"))
    elif path.name.lower() == "report.md":
        candidates.append(path.with_name("LAST.md"))

    existing = []
    for candidate in candidates:
        try:
            if stat.S_ISREG(candidate.stat().st_mode):
                existing.append(candidate)
        except OSError:
            continue
    if not existing:
        return path
    return max(existing, key=lambda candidate: candidate.stat().st_mtime)


def read_progress_stage(directory: Path, run_start: Optional[float] = None) -> str:
    """Return the last non-empty line in a sibling PROGRESS.md, if present."""
    try:
        path = directory / "PROGRESS.md"
        if run_start is None or path.stat().st_mtime <= run_start:
            return ""
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return next((line.strip() for line in reversed(content.splitlines()) if line.strip()), "")


def find_git_root(path: Path) -> Optional[Path]:
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def find_new_branch_commit(path: Path, run_start: Optional[float]) -> Tuple[bool, Optional[float]]:
    """Check local refs only; return whether HEAD is ahead and its newest commit time."""
    repo = find_git_root(path)
    if repo is None:
        return False, None

    try:
        branch_result = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
        upstream_result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        bases = []
        if upstream_result.returncode == 0 and upstream_result.stdout.strip():
            bases.append(upstream_result.stdout.strip())
        bases.extend(("origin/main", "origin/master", "main", "master"))

        seen = set()
        has_base = False
        for base in bases:
            if base in seen or base == current_branch:
                continue
            seen.add(base)
            result = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%ct", f"{base}..HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode != 0:
                continue
            has_base = True
            commit_time = result.stdout.strip()
            if not commit_time:
                return False, None
            try:
                timestamp = float(commit_time)
                return (True, timestamp) if run_start is not None and timestamp > run_start else (False, None)
            except ValueError:
                return False, None

        if has_base or run_start is None:
            return False, None

        result = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ct", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        commit_time = float(result.stdout.strip()) if result.returncode == 0 else 0.0
        if commit_time > run_start:
            return True, commit_time
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, None
    return False, None


def last_status_from_content(content: str) -> Optional[str]:
    """Read an explicit result marker from the heading or first 20 report lines."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    normalized_lines = [
        re.sub(r"^[\s#>*`-]+", "", line.replace("**", "").replace("`", "")).strip()
        for line in lines[:20]
    ]
    if normalized_lines and re.search(r"尚未完成交付|未完成交付", normalized_lines[0]):
        return "partial"

    def status_value(word: str) -> str:
        upper = word.upper()
        return "complete" if upper in ("DONE", "COMPLETE") else "failed" if upper == "FAIL" else upper.lower()

    if normalized_lines:
        first_status = re.match(
            r"^(?:(?:status|狀態|結論|判定|result|結果)\s*[:：]\s*)?"
            r"(DONE|COMPLETE|FAIL|FAILED|BLOCKED|PARTIAL)\b",
            normalized_lines[0],
            re.IGNORECASE,
        )
        if first_status:
            return status_value(first_status.group(1))

    for line in normalized_lines:
        status_field = re.match(
            r"^(?:status|狀態|結論|判定|result|結果)\s*[:：]\s*"
            r"(DONE|COMPLETE|FAIL|FAILED|BLOCKED|PARTIAL)\b",
            line,
            re.IGNORECASE,
        )
        if status_field:
            return status_value(status_field.group(1))

    for line in normalized_lines:
        status_line = re.match(
            r"^(DONE|COMPLETE|FAIL|FAILED|BLOCKED|PARTIAL)\b",
            line,
            re.IGNORECASE,
        )
        if status_line:
            return status_value(status_line.group(1))

    return None


_PID_CACHE_UNSET = object()


def running_pid_set() -> Optional[Set[int]]:
    """Read the process inventory once; None means inspection failed."""
    try:
        result = subprocess.run(
            ["pgrep", "-l", "."], capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in (0, 1):
        return None
    pids = set()
    for line in result.stdout.splitlines():
        try:
            pids.add(int(line.split(maxsplit=1)[0]))
        except (IndexError, ValueError):
            continue
    return pids


def is_pid_alive(pid: int) -> Optional[bool]:
    """None means inspection failed, not that the process has ended."""
    if pid <= 0:
        return None
    pids = running_pid_set()
    return None if pids is None else pid in pids


def parse_process_lstart(text: str) -> Optional[float]:
    """Parse `ps -o lstart=` output ("Sun Oct 25 10:00:00 2026") as epoch seconds."""
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    try:
        parsed = time.strptime(normalized, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    try:
        return time.mktime(parsed)
    except (OverflowError, ValueError):
        return None


def read_process_start(pid: int, now: Optional[float] = None) -> Optional[float]:
    """SPEC §4.1 layer 2: read a live PID's start time from `ps -o lstart=`.

    Returns None whenever the process is gone, `ps` fails, or the output cannot
    be parsed, so the caller can fall back to the log/birthtime estimate.
    """
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            # `lstart` is locale dependent; pin C so %a/%b stay parseable.
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0:
        return None
    started = parse_process_lstart(result.stdout or "")
    if started is None:
        return None
    if now is not None and started > now + 60:
        return None
    return started


def report_is_fresh(
    last_valid: bool,
    last_mtime: float,
    run_start: float,
    init_time: Optional[float] = None,
) -> bool:
    """Return whether a result document was written by the current run.

    Claude stream-json keeps writing after REPORT.md is saved (the final
    assistant text and the ``result`` event), so the log's mtime cannot be the
    upper boundary. The run start and the latest init event are the real
    boundaries; a report older than either belongs to an earlier invocation.
    """
    if not last_valid or last_mtime <= run_start:
        return False
    return init_time is None or last_mtime >= init_time


def explicit_report_status(
    is_done_file: bool,
    last_valid: bool,
    last_mtime: float,
    last_status: Optional[str],
    run_start: float,
    init_time: Optional[float] = None,
) -> Optional[str]:
    """Return the REPORT's explicit status when it belongs to this run."""
    if not last_valid or last_status is None:
        return None
    if is_done_file or report_is_fresh(last_valid, last_mtime, run_start, init_time):
        return last_status
    return None


def determine_status(
    is_done_file: bool,
    last_valid: bool,
    last_mtime: float,
    start_time: Optional[float],
    log_mtime: float,
    is_null_log: bool,
    data_error: str,
    now: float,
    pid: Optional[int] = None,
    last_status: Optional[str] = None,
    has_new_commit: bool = False,
    process_alive: Optional[bool] = None,
    inspect_process: bool = True,
    init_time: Optional[float] = None,
) -> str:
    if pid is not None:
        if process_alive is None and inspect_process:
            process_alive = is_pid_alive(pid)
        if process_alive:
            return "running"
        if process_alive is None:
            return "unconfirmed"
    if is_done_file:
        return (last_status or "reported") if last_valid else "no_report"
    run_start = start_time if start_time is not None else log_mtime
    last_is_fresh = report_is_fresh(last_valid, last_mtime, run_start, init_time)
    if last_is_fresh:
        return last_status or "reported"
    if has_new_commit:
        return "complete"
    if data_error:
        return "error"
    if is_null_log:
        return "unconfirmed"
    if pid is not None:
        return "no_report"
    if log_mtime <= 0:
        return "unconfirmed"
    if now - log_mtime > 30 * 60:
        return "waiting"
    return "running"


def build_agent(
    name: str,
    log_str: str,
    last_str: str,
    is_done_file: bool,
    log_parser: LogParser,
    now: float,
    pid: Optional[int] = None,
    row_error: str = "",
    registration_time: Optional[float] = None,
    harness: Optional[str] = None,
    model: Optional[str] = None,
    project: Optional[str] = None,
    started_at: Optional[float] = None,
    note: str = "",
    pid_alive_override: Any = _PID_CACHE_UNSET,
) -> AgentInfo:
    log_path = Path(log_str).expanduser()
    if not log_path.is_absolute():
        log_path = ROOT_DIR / log_path

    last_path = Path(last_str).expanduser()
    if not last_path.is_absolute():
        last_path = ROOT_DIR / last_path

    is_null_log = (str(log_path) == "/dev/null")
    start_time, _, log_lines, log_mtime, log_err = log_parser.parse(log_path)
    result_path = resolve_result_path(last_path)
    last_content, last_mtime, last_valid, last_err = read_last_file(result_path)

    data_err = row_error or log_err or last_err
    if not is_null_log and not log_path.exists():
        data_err = f"log 檔案不存在：{log_path}"
    last_status = last_status_from_content(last_content) if last_valid else None
    if pid is None:
        pid_alive = None
    elif pid_alive_override is _PID_CACHE_UNSET:
        pid_alive = is_pid_alive(pid)
    else:
        pid_alive = pid_alive_override
    resolved_harness, resolved_model = resolve_run_identity(
        log_path,
        pid,
        harness,
        model,
        pid_alive,
    )

    elapsed_start = started_at
    start_source = "tsv" if started_at is not None else ""
    if elapsed_start is None and pid_alive is True:
        elapsed_start = log_parser.pid_start_time(pid)
        if elapsed_start is not None:
            start_source = "pid"
    if elapsed_start is None:
        elapsed_start = start_time
        start_source = "log" if elapsed_start is not None else ""
        # A result followed by new log activity proves reuse, but the old
        # prefix cannot prove when this invocation began.
        if last_valid and log_mtime > last_mtime and (elapsed_start or 0) <= last_mtime:
            elapsed_start = None
            start_source = ""
    run_start = elapsed_start if elapsed_start is not None else log_mtime
    log_key = str(log_path.resolve())
    init_time = log_parser.init_events.get(log_key)
    last_is_fresh = report_is_fresh(last_valid, last_mtime, run_start, init_time)
    report_status = explicit_report_status(
        is_done_file, last_valid, last_mtime, last_status, run_start, init_time
    )
    run_started = run_start if run_start > 0 else None
    stage = read_progress_stage(log_path.parent, run_started)
    has_new_commit = False
    commit_time = None
    if pid_alive is not True and not is_done_file and not last_is_fresh:
        has_new_commit, commit_time = find_new_branch_commit(result_path.parent, run_started)

    status = determine_status(
        is_done_file=is_done_file,
        last_valid=last_valid,
        last_mtime=last_mtime,
        start_time=run_start,
        log_mtime=log_mtime,
        is_null_log=is_null_log,
        data_error=data_err,
        now=now,
        pid=pid,
        last_status=last_status,
        has_new_commit=has_new_commit,
        process_alive=pid_alive,
        inspect_process=False,
        init_time=init_time,
    )
    event_status = log_parser.result_events.get(log_key)
    if (
        pid_alive is not True and not (pid is not None and pid_alive is None)
        and report_status is None and event_status is not None
        and event_status[0] is not None and run_started is not None
        and event_status[0] >= run_started
    ):
        status = event_status[1]

    is_terminal = status in {"complete", "failed", "blocked", "partial", "reported"}
    completion_time = None
    if is_done_file and status != "running":
        completion_time = last_mtime if last_valid else log_mtime or None
    elif is_terminal:
        if last_is_fresh:
            completion_time = last_mtime
        elif status == "complete" and has_new_commit:
            completion_time = commit_time or (last_mtime if last_valid else 0) or log_mtime or now
        elif event_status and event_status[0] is not None:
            completion_time = event_status[0]
    if completion_time is not None and elapsed_start is not None and elapsed_start > completion_time:
        elapsed_start = None
    question = None if is_null_log else read_task_question(log_path.parent)
    if question is not None and question_is_active(question, completion_time):
        # SPEC §3.3: an unanswered question is a needs-attention task even while
        # its PID is alive, so it must leave the running bucket.
        status = "question"
    elapsed = format_elapsed(elapsed_start, completion_time if completion_time is not None else now)
    estimated = start_source == "log" and elapsed_start is not None
    if estimated:
        elapsed = "~" + elapsed
    key = f"{name}::{log_path}::{result_path}"

    if is_terminal and last_content and (last_is_fresh or is_done_file):
        clean_last_lines = [
            clean_text(l) for l in last_content.splitlines() if clean_text(l) and not is_noise(clean_text(l))
        ]
        conclusion_lines = [
            line for line in clean_last_lines
            if not line.startswith("#") and not re.fullmatch(
                r"(?:(?:STATUS|狀態|判定)\s*[:：]\s*)?"
                r"(?:DONE|COMPLETE|COMPLETED|PARTIAL|FAIL|FAILED|BLOCKED|REPORTED|完成|部分完成|失敗|受阻)",
                line.replace("**", "").strip(), re.IGNORECASE,
            )
        ]
        latest = (conclusion_lines or ["完成" if status == "complete" else "任務已回報"])[0]
    elif log_lines:
        latest = log_lines[-1].partition("  ")[2] or log_lines[-1]
    else:
        latest = data_err or "尚無訊息"

    resolved_project, project_error = resolve_project(project) if project else (
        infer_project(name, log_path, result_path), ""
    )
    data_err = "；".join(value for value in (data_err, project_error) if value)
    return AgentInfo(
        name=name,
        log_path=log_path,
        last_path=result_path,
        status=status,
        model=resolved_model,
        harness=resolved_harness,
        start_time=elapsed_start,
        elapsed=elapsed,
        latest=latest,
        log_lines=log_lines,
        log_mtime=log_mtime,
        is_done_file=is_done_file,
        key=key,
        error=data_err,
        last_content=last_content,
        pid=pid,
        completion_time=completion_time,
        stage=stage,
        project=resolved_project,
        start_source=start_source,
        elapsed_estimated=estimated,
        question=question,
        note=note,
        superseded_hint=parse_superseded_hint(note),
        process_alive=pid_alive,
    )


def parse_superseded_hint(note: str) -> str:
    """Extract the named replacement from a SUPERSEDED note."""
    if not note.startswith("SUPERSEDED("):
        return ""
    match = re.match(r"SUPERSEDED\(by\s+(.+?)(?:;|\))", note)
    return match.group(1).strip() if match else ""


def load_tsv_agents(
    tsv_path: Path,
    is_done_file: bool,
    log_parser: LogParser,
    now: float,
    process_pids: Any = _PID_CACHE_UNSET,
) -> Tuple[List[AgentInfo], List[str]]:
    agents = []
    errors = []
    try:
        content = tsv_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], [f"TSV 不存在：{tsv_path}"]
    except OSError as e:
        return [], [f"讀取 TSV 失敗 {tsv_path}：{e.strerror or e}"]

    # A shared TSV's mtime changes when *any* row changes.
    registration_time = None

    for line_num, line in enumerate(content.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if not 3 <= len(parts) <= 9:
            errors.append(f"{tsv_path.name} 第 {line_num} 行格式錯誤（需 3 至 9 欄）")
            continue
        name, log_p, last_p = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not name or not log_p or not last_p:
            errors.append(f"{tsv_path.name} 第 {line_num} 行格式錯誤（名稱、log、LAST.md 不可空白）")
            continue

        pid = None
        row_error = ""
        if len(parts) >= 4 and parts[3].strip():
            try:
                pid = int(parts[3].strip())
                if pid <= 0:
                    raise ValueError("PID 必須大於 0")
            except ValueError:
                row_error = f"{tsv_path.name} 第 {line_num} 行 PID 格式錯誤"
                errors.append(row_error)
                pid = None
        harness = parts[4].strip() or None if len(parts) >= 5 else None
        model = parts[5].strip() or None if len(parts) >= 6 else None
        project = parts[6].strip() or None if len(parts) >= 7 else None
        note = parts[8].strip() if len(parts) >= 9 else ""
        started_at = None
        if len(parts) >= 8 and parts[7].strip():
            raw_start = parts[7].strip()
            try:
                parsed = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("timezone required")
                started_at = parsed.timestamp()
            except (ValueError, OverflowError, OSError):
                errors.append(f"{tsv_path.name} 第 {line_num} 行 started_at 格式錯誤（需 ISO 8601 含時區）")

        pid_alive_override = _PID_CACHE_UNSET
        if is_done_file:
            pid_alive_override = False
        elif pid is not None and process_pids is not _PID_CACHE_UNSET:
            pid_alive_override = None if process_pids is None else pid in process_pids
        agents.append(
            build_agent(
                name,
                log_p,
                last_p,
                is_done_file,
                log_parser,
                now,
                pid=pid,
                row_error=row_error,
                registration_time=registration_time,
                harness=harness,
                model=model,
                project=project,
                started_at=started_at,
                note=note,
                pid_alive_override=pid_alive_override,
            )
        )

    return agents, errors


def load_all_agents(
    runs_path: Path = DEFAULT_RUNS_TSV,
    done_path: Path = DEFAULT_DONE_TSV,
    log_parser: Optional[LogParser] = None,
    now: Optional[float] = None,
) -> Tuple[List[AgentInfo], List[str]]:
    ensure_architecture_data()
    if log_parser is None:
        log_parser = LogParser()
    if now is None:
        now = time.time()

    process_pids = running_pid_set()
    running_agents, err1 = load_tsv_agents(
        runs_path, False, log_parser, now, process_pids=process_pids,
    )
    done_agents, err2 = load_tsv_agents(
        done_path, True, log_parser, now, process_pids=process_pids,
    )

    active_keys = {a.key for a in running_agents}
    archived_only = [a for a in done_agents if a.key not in active_keys]
    all_agents = list({a.key: a for a in running_agents + archived_only}.values())
    all_agents.sort(
        key=lambda a: (
            a.completion_time is not None,
            a.completion_time if a.completion_time is not None else a.log_mtime,
        ),
        reverse=True,
    )
    return all_agents, list(ARCHITECTURE_ERRORS) + err1 + err2


def is_active_agent(agent: AgentInfo) -> bool:
    return not is_done_agent(agent)


def is_done_agent(agent: AgentInfo) -> bool:
    return agent.status in {"complete", "partial", "failed", "blocked", "reported"} or (
        agent.is_done_file and agent.status != "running"
    )


def done_timestamp(agent: AgentInfo) -> Optional[float]:
    value = agent.completion_time if agent.completion_time is not None else agent.log_mtime
    return value if value is not None and value > 0 else None


def split_recent_done(
    done_agents: List[AgentInfo],
    now: Optional[float] = None,
) -> Tuple[List[AgentInfo], List[AgentInfo]]:
    """Split terminal tasks into the 2-hour recent list and older history."""
    current = time.time() if now is None else now
    recent = []
    older = []
    for agent in done_agents:
        timestamp = done_timestamp(agent)
        age = current - timestamp if timestamp is not None else None
        if (
            age is not None
            and -FUTURE_SKEW_TOLERANCE_SECONDS <= age <= RECENT_DONE_SECONDS
        ):
            recent.append(agent)
        else:
            older.append(agent)
    sort_key = lambda agent: done_timestamp(agent) or 0.0
    recent.sort(key=sort_key, reverse=True)
    older.sort(key=sort_key, reverse=True)
    return recent, older


def task_bucket(agent: AgentInfo, now: Optional[float] = None) -> str:
    """Partition every task once, allowing ended issues to age into history."""
    current = time.time() if now is None else now
    if agent.status == "running":
        return "running"
    ended = agent.completion_time
    if ended is None and is_done_agent(agent):
        ended = done_timestamp(agent)
    if ended is not None and current - ended > RECENT_DONE_SECONDS:
        return "history"
    if agent.status in {"waiting", "blocked", "failed", "no_report", "unconfirmed", "error"}:
        return "attention"
    if agent.status in {"complete", "reported", "partial"}:
        if (
            ended is not None
            and -FUTURE_SKEW_TOLERANCE_SECONDS <= current - ended <= RECENT_DONE_SECONDS
        ):
            return "recent"
        return "history"
    return "attention"


def count_buckets(
    agents: List[AgentInfo], now: Optional[float] = None,
    bucket_fn: Callable[[AgentInfo, float], str] = task_bucket,
) -> Dict[str, int]:
    current = time.time() if now is None else now
    counts = dict(attention=0, running=0, recent=0, history=0, total=len(agents))
    for agent in agents:
        counts[bucket_fn(agent, current)] += 1
    return counts


def format_bucket_counts(
    agents: List[AgentInfo], now: float,
    bucket_fn: Callable[[AgentInfo, float], str] = task_bucket,
) -> str:
    counts = count_buckets(agents, now, bucket_fn)
    return (
        f"需處理 {counts['attention']} │ 進行中 {counts['running']} │ "
        f"剛完成(2h) {counts['recent']} │ 歷史 {counts['history']} │ 總計 {counts['total']}"
    )


def work_board_groups(agents: List[AgentInfo], now: float) -> Dict[str, List[AgentInfo]]:
    all_groups = group_agents_by_project(agents)
    groups = group_agents_by_project([a for a in agents if task_bucket(a, now) != "history"])
    latest_finish = {
        project: max(
            (done_timestamp(a) or 0 for a in tasks if is_done_agent(a)),
            default=0,
        )
        for project, tasks in all_groups.items()
    }
    order = {"attention": 0, "running": 1, "recent": 2}
    for tasks in groups.values():
        tasks.sort(key=lambda a: (
            order[task_bucket(a, now)],
            -(a.start_time or 0) if task_bucket(a, now) == "running" else -(done_timestamp(a) or 0),
            a.key,
        ))
    return dict(sorted(groups.items(), key=lambda item: (
        min(order[task_bucket(a, now)] for a in item[1]),
        -latest_finish[item[0]],
        project_sort_key(item[0]),
    )))


# ==============================================================================
# Quota Logic & Magpie Fetching
# ==============================================================================

def quota_time_progress(window: Dict[str, Any], now: Optional[float] = None) -> Optional[float]:
    """Infer elapsed percentage only when the window duration and reset are known."""
    reset = quota_epoch(window.get("resetsAt") or window.get("resets_at"))
    if reset is None:
        return None
    minutes = window.get("window_minutes")
    name = str(window.get("name") or "").strip().casefold()
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and math.isfinite(minutes) and minutes > 0:
        seconds = minutes * 60
    elif name in ("5 hours", "5 hour", "5h", "5 小時"):
        seconds = 5 * 3600
    elif name in ("7 days", "7 day", "7d", "weekly", "7 天"):
        seconds = 7 * 86400
    elif name in ("30 days", "30 day", "30d"):
        seconds = 30 * 86400
    elif name in ("month", "monthly", "月"):
        end = datetime.fromtimestamp(reset, ZoneInfo("Asia/Taipei"))
        year, month = (end.year - 1, 12) if end.month == 1 else (end.year, end.month - 1)
        start = end.replace(year=year, month=month, day=min(end.day, calendar.monthrange(year, month)[1]))
        seconds = reset - start.timestamp()
    else:
        return None
    current = time.time() if now is None else now
    return max(0.0, min(100.0, 100 * (current - reset + seconds) / seconds))


def quota_window_status(used: float, progress: Optional[float]) -> Tuple[str, str]:
    """Critical usage takes precedence over burning ahead of the clock."""
    if used >= QUOTA_CRITICAL_PERCENT:
        return "#f7768e", "告急"
    if progress is not None and used - progress >= QUOTA_BURN_AHEAD_PERCENT:
        return "#e0af68", "燒太快"
    return "#9ece6a", ""


def render_quota_chart(used: float, progress: Optional[float], width: int = 12) -> str:
    width = max(1, width)
    filled = int(round(used / 100 * width))
    marker = None if progress is None else int(round(progress / 100 * (width - 1)))
    color, _ = quota_window_status(used, progress)
    cells = []
    for index in range(width):
        if index == marker:
            cells.append("[bold #c0caf5]│[/]")
        elif index < filled:
            cells.append(f"[{color}]▓[/]")
        else:
            cells.append("[#737b91]░[/]")
    return "".join(cells)


def ordered_quota_items(data: list) -> list:
    """Use canonical provider order without discarding additional accounts."""
    items = [
        item for item in data
        if isinstance(item, dict) and str(item.get("provider") or "").strip()
    ] if isinstance(data, list) else []
    if not items:
        return []
    ordered = []
    for provider in QUOTA_PROVIDER_NAMES:
        matches = [item for item in items if str(item.get("provider", "")).casefold() == provider]
        ordered.extend(matches or [{"provider": provider, "windows": []}])
    ordered.extend(item for item in items if str(item.get("provider", "")).casefold() not in QUOTA_PROVIDER_NAMES)
    return ordered


def quota_epoch(value: Any) -> Optional[float]:
    """Parse a quota timestamp without guessing when the value is unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return number / 1000 if number > 100_000_000_000 else number
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return quota_epoch(float(text))
    return parse_timestamp(text)


def format_quota_source_time(value: Any) -> str:
    """Render the actual source timestamp in Taiwan time."""
    epoch = quota_epoch(value)
    if epoch is None:
        return "時間未確認"
    return datetime.fromtimestamp(epoch, ZoneInfo("Asia/Taipei")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def format_reset_time(value: Any, now: Optional[float] = None) -> str:
    """Format an ISO or epoch reset time in Taipei time; never infer a missing value."""
    epoch = quota_epoch(value)
    if epoch is None:
        return "未提供"
    reset = datetime.fromtimestamp(epoch, ZoneInfo("Asia/Taipei"))
    current = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo("Asia/Taipei"))
    if reset.date() == current.date():
        return reset.strftime("%H:%M")
    return reset.strftime("%m/%d %H:%M")


def valid_quota_percent(value: Any) -> Optional[float]:
    """Return a finite 0-100 percentage, rejecting bool and non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 100:
        return None
    return number


def quota_window_label(window: Dict[str, Any]) -> str:
    name = str(window.get("name") or "").lower()
    minutes = window.get("window_minutes")
    if "5 hour" in name or "5h" in name or minutes == 300:
        return "5 小時"
    if "7 day" in name or "7d" in name or minutes == 10080:
        return "7 天"
    if "month" in name or "30 day" in name or minutes == 43200:
        return "月"
    label = str(window.get("name") or "").strip()
    return label or "額度窗口"


def rate_limit_window(name: str, value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    used = valid_quota_percent(value.get("used_percent"))
    if used is None:
        return None
    minutes = value.get("window_minutes")
    window_name = "5 hours" if minutes == 300 else "7 days" if minutes == 10080 else name
    return {
        "name": window_name,
        "used": used,
        "window_minutes": minutes,
        "resetsAt": value.get("resets_at"),
    }


def latest_session_file(sessions_path: Path) -> Optional[Path]:
    try:
        files = [path for path in sessions_path.rglob("*.jsonl") if path.is_file()]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda path: (path.stat().st_mtime, str(path)))


def _iter_reverse_lines(path: Path, block_size: int = 64 * 1024):
    """Yield JSONL lines backwards without loading a full session into memory."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        remainder = b""
        while position > 0:
            size = min(block_size, position)
            position -= size
            stream.seek(position)
            chunk = stream.read(size) + remainder
            parts = chunk.split(b"\n")
            remainder = parts[0]
            for raw_line in reversed(parts[1:]):
                yield raw_line.decode("utf-8", errors="replace")
        if remainder:
            yield remainder.decode("utf-8", errors="replace")


def latest_rate_limits(path: Path) -> Tuple[bool, Any, Any]:
    """Return (found, event timestamp, rate_limits) from the newest matching event."""
    for line in _iter_reverse_lines(path):
        if "rate_limits" not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        if isinstance(payload, dict) and "rate_limits" in payload:
            return True, event.get("timestamp"), payload.get("rate_limits")
    return False, None, None


def read_codex_quota_item(sessions_path: Path) -> Tuple[Dict[str, Any], str]:
    item: Dict[str, Any] = {
        "provider": "codex",
        "name": "Codex",
        "asOf": None,
        "windows": [],
    }
    try:
        session_file = latest_session_file(sessions_path)
    except OSError as error:
        return item, f"Codex sessions 讀取失敗：{error}"
    if session_file is None:
        item["reason"] = "找不到 Codex session"
        return item, ""

    try:
        found, event_time, limits = latest_rate_limits(session_file)
        file_time = session_file.stat().st_mtime
    except OSError as error:
        return item, f"Codex session 讀取失敗：{error}"

    item["asOf"] = event_time if quota_epoch(event_time) is not None else file_time
    if not found:
        item["reason"] = "最新 session 尚無 rate_limits"
        return item, ""
    if not isinstance(limits, dict):
        item["reason"] = "rate_limits 為 null"
        return item, ""

    plan = limits.get("plan_type")
    if isinstance(plan, str) and plan.strip():
        item["plan"] = plan.strip()
    windows = []
    for name, key in (("5 hours", "primary"), ("7 days", "secondary")):
        window = rate_limit_window(name, limits.get(key))
        if window is not None:
            windows.append(window)
    item["windows"] = windows
    if not windows:
        item["reason"] = "rate_limits 沒有有效數字"
    return item, ""


def normalize_magpie_item(value: Any) -> Optional[Dict[str, Any]]:
    """Normalize one magpie quota row; unusable rows are dropped, not guessed."""
    if not isinstance(value, dict):
        return None
    provider = str(value.get("provider") or "").strip()
    if not provider:
        return None
    windows = []
    raw_windows = value.get("windows")
    if isinstance(raw_windows, list):
        for window in raw_windows:
            if not isinstance(window, dict):
                continue
            used = valid_quota_percent(window.get("used"))
            if used is None:
                continue
            windows.append(
                {
                    "name": str(window.get("name") or "額度窗口"),
                    "used": used,
                    "resetsAt": window.get("resetsAt") or window.get("resets_at"),
                    "window_minutes": window.get("window_minutes"),
                }
            )
    return {
        "provider": provider,
        "name": value.get("name") or provider,
        "kind": value.get("kind"),
        "balance": value.get("balance"),
        "plan": value.get("plan"),
        "asOf": value.get("readAt"),
        "windows": windows,
    }


class QuotaService:
    """Fetches Codex session quota and optional non-Codex magpie quota."""

    def __init__(
        self,
        interval: float = QUOTA_REFRESH_SECONDS,
        sessions_path: Optional[Path] = None,
    ):
        self.interval = interval
        self.sessions_path = Path(sessions_path or DEFAULT_SESSIONS_PATH).expanduser()
        self.last_fetch: float = float("-inf")
        self.cached_summary: str = "額度：載入中..."
        self.cached_raw: list = []
        self.stale = False
        self.last_error = ""
        self._fetching = False
        self._lock = threading.Lock()

    def snapshot(self) -> Tuple[str, list, bool, str]:
        with self._lock:
            return self.cached_summary, list(self.cached_raw), self.stale, self.last_error

    def refresh_in_background(self, force: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._fetching:
                return False
            if not force and now - self.last_fetch < self.interval:
                return False
            self.last_fetch = now
            self._fetching = True

        worker = threading.Thread(target=self._fetch_and_store, daemon=True, name="agent-watch-quota")
        worker.start()
        return True

    def _fetch_and_store(self) -> None:
        try:
            summary, raw, error = self._read_quota()
            with self._lock:
                self._store_result(summary, raw, error)
        finally:
            with self._lock:
                self._fetching = False

    def _store_result(self, summary: str, raw: list, error: str) -> None:
        # A failed refresh must not keep old percentages on screen.
        self.cached_raw = list(raw)
        self.cached_summary = summary
        self.stale = bool(error)
        self.last_error = error

    def _read_magpie_quota(self) -> Tuple[list, str]:
        try:
            res = subprocess.run(
                [MAGPIE_COMMAND, "quota", "--json"],
                capture_output=True,
                text=True,
                timeout=MAGPIE_TIMEOUT_SECONDS,
                check=False,
            )
            if res.returncode != 0:
                return [], f"magpie quota 結束碼 {res.returncode}"
            if not res.stdout.strip():
                return [], "magpie quota 沒有輸出資料"
            try:
                data = json.loads(res.stdout)
            except json.JSONDecodeError as error:
                return [], f"magpie quota JSON 格式錯誤：{error}"
            if not isinstance(data, list):
                return [], "magpie quota JSON 格式錯誤"
            items = [
                item
                for value in data
                if (item := normalize_magpie_item(value)) is not None
            ]
            return items, ""
        except subprocess.TimeoutExpired:
            return [], "magpie quota 逾時"
        except Exception as e:
            return [], f"magpie quota 讀取失敗：{e}"

    def _read_quota(self) -> Tuple[str, list, str]:
        """Use magpie values or this read's local Codex session fallback."""
        magpie_items, magpie_error = self._read_magpie_quota()
        items = list(magpie_items)
        errors: List[str] = [magpie_error] if magpie_error else []
        fetched_at = time.time()
        for item in items:
            item["fetchedAt"] = fetched_at
        providers = {str(item.get("provider") or "").lower() for item in items}
        codex_has_data = any(
            str(item.get("provider", "")).lower() == "codex" and quota_item_has_data(item)
            for item in items
        )
        if not codex_has_data:
            codex_item, codex_error = read_codex_quota_item(self.sessions_path)
            if codex_error:
                errors.append(codex_error)
            if quota_item_has_data(codex_item):
                items = [item for item in items if str(item.get("provider", "")).lower() != "codex"]
                items.insert(0, codex_item)
                providers.add("codex")
        for provider in QUOTA_PROVIDER_NAMES:
            if provider not in providers:
                items.append({
                    "provider": provider, "windows": [],
                    "reason": "magpie 讀取失敗" if magpie_error else "無資料",
                    "fetchedAt": fetched_at,
                })
        return parse_quota_json(items), items, "；".join(errors)

    def fetch(self, force: bool = False) -> Tuple[str, list]:
        """Synchronous fetch for one-shot CLI output; the live UI uses the background method."""
        now = time.monotonic()
        with self._lock:
            if not force and now - self.last_fetch < self.interval:
                return self.cached_summary, list(self.cached_raw)
            self.last_fetch = now

        summary, raw, error = self._read_quota()
        with self._lock:
            self._store_result(summary, raw, error)
            return self.cached_summary, list(self.cached_raw)


def valid_quota_windows(item: Dict[str, Any]) -> List[Tuple[str, float, Dict[str, Any]]]:
    windows = item.get("windows")
    if not isinstance(windows, list):
        return []
    result = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        used = valid_quota_percent(window.get("used"))
        if used is None:
            continue
        result.append((quota_window_label(window), used, window))
    return result


def quota_item_has_data(item: Dict[str, Any]) -> bool:
    """A balance or valid windows with known read provenance are usable."""
    balance = item.get("balance")
    has_time = any(quota_epoch(item.get(key)) is not None for key in ("readAt", "asOf", "fetchedAt"))
    return bool(
        (isinstance(balance, str) and balance.strip())
        or (has_time and valid_quota_windows(item))
    )


def quota_data_time(item: Dict[str, Any]) -> str:
    source = item.get("readAt") or item.get("asOf")
    if quota_epoch(source) is not None:
        return format_quota_source_time(source)
    fetched = quota_epoch(item.get("fetchedAt"))
    if fetched is not None:
        clock = datetime.fromtimestamp(fetched, ZoneInfo("Asia/Taipei")).strftime("%H:%M:%S")
        return f"未提供（本次讀取 {clock}）"
    return "時間未確認"


def parse_quota_json(data: list) -> str:
    """Keep the home summary short; source and reset times live in details."""
    if not isinstance(data, list):
        return "額度：無資料"
    parts = []
    seen = set()
    for item in ordered_quota_items(data):
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        if not provider or provider.lower() in seen:
            continue
        seen.add(provider.lower())
        name = QUOTA_PROVIDER_NAMES.get(provider.lower(), provider)
        windows = valid_quota_windows(item)
        if not quota_item_has_data(item):
            parts.append(f"{name}：{item.get('reason') or '無資料'}")
            continue
        abbreviations = {"5 小時": "5h", "7 天": "7d"}
        values = []
        for label, used, _ in windows:
            window_name = abbreviations.get(label, label)
            values.append(f"{window_name} {int(round(used))}%")
        if isinstance(item.get("balance"), str) and item["balance"].strip():
            values.append(item["balance"])
        parts.append(f"{name} {' '.join(values)}")
    if not parts:
        return "額度：無資料"
    updated = max(
        (quota_epoch(item.get("fetchedAt")) or quota_epoch(item.get("asOf")) or 0
         for item in data if isinstance(item, dict)), default=0,
    )
    clock = datetime.fromtimestamp(updated, ZoneInfo("Asia/Taipei")).strftime("%H:%M:%S") if updated else "未確認"
    return " │ ".join(parts) + f" │ 更新 {clock}"


def render_compact_quota(summary: str, width: int = 80) -> str:
    """Wrap at provider separators, preserving every service without ellipsis."""
    lines: List[str] = []
    current = "額度 "
    for part in summary.split(" │ "):
        candidate = current + (" │ " if current != "額度 " else "") + part
        if display_width(candidate) <= width:
            current = candidate
        else:
            if current.strip() != "額度":
                lines.append(current)
            wrapped = wrap_display(part, max(1, width))
            lines.extend(wrapped[:-1])
            current = wrapped[-1]
    lines.append(current)
    return "\n".join(lines)


def render_full_quota_view(
    quota_data: list, width: int = 80, stale: bool = False, now: Optional[float] = None,
) -> str:
    """Render each quota window on its own short line for narrow terminals."""
    lines = [
        "[bold #7aa2f7]● 服務額度用量[/bold #7aa2f7]",
        "[#a9b1bd]│ 時間進度；用量領先 ≥15 點黃、用量 ≥85% 紅；每 60 秒更新[/]",
    ]
    if stale:
        lines.append("[bold #ff9e3b]部分來源讀取失敗；只顯示本次取得且有時間的有效數字[/bold #ff9e3b]")
    if not quota_data:
        lines.append("[#ff9e3b]無資料[/#ff9e3b]")
        return "\n".join(lines)

    rendered = False
    for item in ordered_quota_items(quota_data):
        provider = str(item.get("provider", "")).casefold()
        name = QUOTA_PROVIDER_NAMES.get(provider, str(item.get("name") or provider))
        plan = item.get("plan")
        title = name if not plan else f"{name} ({plan})"
        lines.append(f"[bold #c0caf5]{escape(title)}[/bold #c0caf5]")
        source_time = quota_data_time(item)
        windows = valid_quota_windows(item)
        if not quota_item_has_data(item):
            lines.append(f"  [#a9b1bd]資料時間：{escape(source_time)}[/]")
            lines.append(f"  [#ff9e3b]{escape(str(item.get('reason') or '無資料'))}[/#ff9e3b]")
            rendered = True
            continue
        if isinstance(item.get("balance"), str) and item["balance"].strip():
            lines.append(f"  帳戶餘額 {escape(item['balance'])}")
            lines.append(f"  [#a9b1bd]資料時間：{escape(source_time)}[/]")
            rendered = True
        for label, used, window in windows:
            reset = format_reset_time(window.get("resetsAt") or window.get("resets_at"), now)
            progress = quota_time_progress(window, now)
            color, tag = quota_window_status(used, progress)
            if progress is None:
                tag = (tag + " " if tag else "") + "時間進度未知"
            short_label = {"5 小時": "5h", "7 天": "7d"}.get(label, label)
            bar = render_quota_chart(used, progress, max(6, min(20, width - 62)))
            row = f"  {escape(short_label)} {bar} [{color}]{used:g}% {tag}[/]  重置 {escape(reset)}"
            stamp = f"資料時間：{escape(source_time)}"
            if display_width(Text.from_markup(row + "  " + stamp).plain) <= max(1, width - 4):
                lines.append(row + "  [#a9b1bd]" + stamp + "[/]")
            else:
                lines.append(row)
                lines.append("    [#a9b1bd]" + stamp + "[/]")
            rendered = True

    if not rendered:
        lines.append("[#ff9e3b]無資料[/#ff9e3b]")

    return "\n".join(lines)


def render_plain_quota_details(quota_data: list) -> List[str]:
    """Render sanitized plain-text quota details for --once."""
    lines = []
    for item in ordered_quota_items(quota_data):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("provider") or "未知服務")
        source_time = quota_data_time(item)
        windows = valid_quota_windows(item)
        if not quota_item_has_data(item):
            lines.append(f"  * {name}: {item.get('reason') or '無資料'}（資料時間：{source_time}）")
            continue
        if isinstance(item.get("balance"), str) and item["balance"].strip():
            lines.append(f"  * {name}: 帳戶餘額 {item['balance']}（資料時間：{source_time}）")
        for label, used, window in windows:
            reset = format_reset_time(window.get("resetsAt") or window.get("resets_at"))
            lines.append(
                f"  * {name} {label}: {int(round(used))}%"
                f"（重置 {reset}，資料時間：{source_time}）"
            )
    return lines or ["  * 無資料"]


# ==============================================================================
# Architecture View Rendering
# ==============================================================================

def wrap_display(text: str, width: int) -> List[str]:
    """Wrap text by terminal display width without dropping content."""
    if width <= 0:
        return [""]
    output = []
    for source_line in str(text).splitlines() or [""]:
        current = []
        used = 0
        for char in source_line:
            char_width = 0 if unicodedata.combining(char) else (
                2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            )
            if current and used + char_width > width:
                output.append("".join(current).rstrip())
                current = []
                used = 0
            current.append(char)
            used += char_width
        output.append("".join(current).rstrip())
    return output or [""]


def _node_identity(agent: AgentInfo) -> str:
    return " ".join(
        (agent.name, agent.harness, agent.model, str(agent.log_path))
    ).casefold()


def _assign_agents_to_nodes(
    agents: List[AgentInfo],
    view: Dict[str, Any],
) -> Dict[str, List[AgentInfo]]:
    candidates = []
    order = 0
    for layer in view["layers"]:
        for node_id in layer:
            node = view["nodes"][node_id]
            keywords = [
                keyword for keyword in node.get("match_keywords", [])
                if isinstance(keyword, str)
            ]
            if keywords:
                candidates.append((order, node_id, keywords))
            order += 1
    assignments: Dict[str, List[AgentInfo]] = {
        node_id: [] for _, node_id, _ in candidates
    }
    for agent in agents:
        identity = _node_identity(agent)
        scored = []
        for node_order, node_id, keywords in candidates:
            score = sum(
                1 for keyword in keywords if keyword_matches(keyword, identity)
            )
            if score:
                scored.append((score, -node_order, node_id))
        if scored:
            _, _, best_node = max(scored)
            assignments[best_node].append(agent)
    return assignments


def _node_runtime(assigned: List[AgentInfo]) -> Tuple[str, str, str]:
    running = [
        agent for agent in assigned
        if agent.status in {"running", "waiting", "unconfirmed"}
    ]
    done = [
        agent for agent in assigned
        if agent.status in {"complete", "partial", "failed", "blocked", "reported", "no_report"}
    ]
    if running:
        detail = next(
            (agent.stage or agent.latest for agent in running if agent.stage or agent.latest),
            "執行中",
        )
        prefix = f"{len(running)} 任務執行中 · " if len(running) > 1 else ""
        return "running", "●", f"{prefix}{detail}"
    if done:
        latest = max(done, key=lambda agent: done_timestamp(agent) or 0.0)
        detail = latest.stage or latest.latest or "已完成"
        return "done", "✓", f"完成 {len(done)} · {detail}"
    return "idle", "○", "無任務"


def _architecture_color(node: Dict[str, Any], state: str) -> str:
    if state == "running":
        return "#7dcfff"
    if state == "done":
        return "#9ece6a"
    kind = str(node.get("kind") or "worker")
    return {
        "coordinator": "#bb9af7",
        "support": "#e0af68",
        "actor": "#c0caf5",
    }.get(kind, "#565f89")


def _architecture_card(
    node_id: str,
    node: Dict[str, Any],
    assigned: List[AgentInfo],
    card_width: int,
) -> Tuple[List[str], int, str]:
    state, marker, progress = _node_runtime(assigned)
    color = _architecture_color(node, state)
    content_width = max(1, card_width - 4)
    raw_lines = [
        f"{node.get('label', node_id)}",
        f"模型：{node.get('model', '未知')}",
        f"角色：{node.get('role', '未知')}",
        f"狀態：{marker} {state}",
        f"進度：{progress}",
    ]
    wrapped = []
    for raw_line in raw_lines:
        wrapped.extend(wrap_display(raw_line, content_width))
    rows = [f"[{color}]┌{'─' * (card_width - 2)}┐[/]"]
    for text in wrapped:
        padding = max(0, content_width - display_width(text))
        rows.append(
            f"[{color}]│[/] {escape(text)}{' ' * padding} [{color}]│[/]"
        )
    rows.append(f"[{color}]└{'─' * (card_width - 2)}┘[/]")
    return rows, card_width, color


def _centered_label(text: str, width: int) -> str:
    visible = truncate_display(text, width)
    padding = max(0, (width - display_width(visible)) // 2)
    return " " * padding + f"[#565f89]{escape(visible)}[/]"


def _architecture_connector(
    top_nodes: List[str],
    bottom_nodes: List[str],
    edges: List[List[str]],
    positions: Dict[str, int],
    width: int,
) -> List[str]:
    down = []
    up = []
    top = set(top_nodes)
    bottom = set(bottom_nodes)
    for edge in edges:
        source, target = edge[0], edge[1]
        if source in top and target in bottom:
            down.append(edge)
        elif source in bottom and target in top:
            up.append(edge)
    if not down and not up:
        return []

    stem = [" "] * width
    link = [" "] * width
    involved = set()
    for edge in down + up:
        involved.add(positions[edge[0]])
        involved.add(positions[edge[1]])
    for x in involved:
        stem[x] = "│"
    for edge in down + up:
        left, right = sorted((positions[edge[0]], positions[edge[1]]))
        for x in range(left, right + 1):
            if link[x] == " ":
                link[x] = "─"
    for edge in down:
        x = positions[edge[1]]
        link[x] = "↕" if link[x] == "▲" else "▼"
    for edge in up:
        x = positions[edge[1]]
        link[x] = "↕" if link[x] == "▼" else "▲"

    labels = sorted({edge[2] for edge in down + up if len(edge) > 2 and edge[2]})
    label_text = ("↕ " if down and up else "") + " · ".join(labels)
    return [
        _centered_label(label_text, width) if label_text else " " * width,
        f"[#7dcfff]{''.join(stem).rstrip()}[/]",
        f"[#7dcfff]{''.join(link).rstrip()}[/]",
    ]


def _architecture_event_time(agent: AgentInfo) -> str:
    timestamp = done_timestamp(agent) if is_done_agent(agent) else agent.log_mtime
    if not timestamp or timestamp <= 0:
        return "--:--:--"
    return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Taipei")).strftime("%H:%M:%S")


def _render_architecture_events(
    agents: List[AgentInfo],
    width: int,
    limit: int,
) -> List[str]:
    ordered = sorted(
        agents,
        key=lambda agent: (
            (done_timestamp(agent) if is_done_agent(agent) else agent.log_mtime) or 0.0
        ),
        reverse=True,
    )[:limit]
    inner = max(8, width - 4)
    event_title = " 最近事件 "
    top_dashes = max(1, width - 2 - display_width(event_title))
    lines = [
        f"[#414868]┌{event_title}{'─' * top_dashes}┐[/]",
    ]
    if not ordered:
        lines.append(_boxed_architecture_line("尚無此專案的任務事件", width))
    else:
        for agent in ordered:
            state = "done" if is_done_agent(agent) else "running"
            text = f"{_architecture_event_time(agent)} {agent.name} [{state}] {agent.latest}"
            for wrapped in wrap_display(text, max(1, inner - 2)):
                lines.append(_boxed_architecture_line(wrapped, width))
    lines.append(f"[#414868]└{'─' * (width - 2)}┘[/]")
    return lines


def _boxed_architecture_line(text: str, width: int) -> str:
    content_width = max(1, width - 4)
    visible = truncate_display(clean_text(text), content_width)
    padding = max(0, content_width - display_width(visible))
    return f"[#414868]│[/] {escape(visible)}{' ' * padding} [#414868]│[/]"


def get_worker_status(
    worker_id: str,
    active_agents: List[AgentInfo],
) -> Tuple[bool, int, str]:
    """Compatibility helper for checking a node in the current architecture."""
    ensure_architecture_data()
    view = ARCHITECTURE_DATA["architectures"]["now"]
    node = view["nodes"].get(worker_id)
    if not isinstance(node, dict):
        return False, 0, "○ 待命中"
    assignments = _assign_agents_to_nodes(active_agents, view)
    running = [
        agent for agent in assignments.get(worker_id, [])
        if agent.status in {"running", "waiting", "unconfirmed"}
    ]
    if running:
        return True, len(running), f"● 執行中 ({len(running)})"
    return False, 0, "○ 待命中"


def is_node_active(node_key: str, active_agents: List[AgentInfo]) -> bool:
    active, _, _ = get_worker_status(node_key, active_agents)
    return active


def render_architecture_view(
    agents: List[AgentInfo],
    width: int = 80,
    view_id: str = "now",
    project_id: Optional[str] = None,
) -> str:
    """Render one project's configurable coordinator/worker graph."""
    ensure_architecture_data()
    projects = ARCHITECTURE_DATA["projects"]
    project_ids = list(projects)
    selected = project_id or project_ids[0]
    if selected not in projects:
        raise ValueError(f"unknown architecture project: {selected}")
    view = ARCHITECTURE_DATA["architectures"].get(view_id)
    if view is None:
        raise ValueError(f"unknown architecture view: {view_id}")

    project_agents = [
        agent for agent in agents
        if (agent.project or PROJECT_OTHER) == selected
        and (agent.status == "running" or task_bucket(agent) != "history")
    ]
    assignments = _assign_agents_to_nodes(project_agents, view)
    available_width = max(20, width - 4)
    layers = view["layers"]
    specs = []
    layer_widths = []
    for layer in layers:
        count = len(layer)
        candidate_width = (available_width - 2 * (count - 1)) // count
        stacked = candidate_width < 12
        card_width = available_width if stacked else candidate_width
        layer_width = card_width if stacked else count * card_width + 2 * (count - 1)
        specs.append((stacked, card_width))
        layer_widths.append(layer_width)
    diagram_width = max(layer_widths)
    offsets = [(diagram_width - width_value) // 2 for width_value in layer_widths]
    positions: Dict[str, int] = {}
    for layer, (stacked, card_width), offset in zip(layers, specs, offsets):
        if stacked:
            for node_id in layer:
                positions[node_id] = offset + card_width // 2
        else:
            for index, node_id in enumerate(layer):
                positions[node_id] = offset + index * (card_width + 2) + card_width // 2

    project_index = project_ids.index(selected) + 1
    lines = [
        f"[bold #7aa2f7]● {escape(str(projects[selected]['label']))} 架構 — {escape(str(view['label']))}[/]",
    ]
    for caption_line in wrap_display(str(view.get("caption", "")), max(20, width - 2)):
        lines.append(f"[#565f89]{escape(caption_line)}[/]")
    legend = (
        f"P 切換專案 ({project_index}/{len(project_ids)}) · N 現在 · F 未來"
        " · ● running · ✓ done · ○ idle"
    )
    lines.append(f"[#565f89]{escape(truncate_display(legend, max(20, width - 2)))}[/]")
    lines.append("")

    for layer_index, (layer, (stacked, card_width), offset) in enumerate(
        zip(layers, specs, offsets)
    ):
        cards = {
            node_id: _architecture_card(
                node_id,
                view["nodes"][node_id],
                assignments.get(node_id, []),
                card_width,
            )
            for node_id in layer
        }
        if stacked:
            for node_index, node_id in enumerate(layer):
                for row in cards[node_id][0]:
                    lines.append((" " * offset + row).rstrip())
                if node_index + 1 < len(layer):
                    lines.append((" " * (offset + card_width // 2) + "[#7dcfff]│[/]").rstrip())
        else:
            height = max(len(cards[node_id][0]) for node_id in layer)
            for row_index in range(height):
                padded_rows = []
                for node_id in layer:
                    rows, width_value, color = cards[node_id]
                    if row_index < len(rows) - 1:
                        padded_rows.append(rows[row_index])
                    elif row_index == height - 1:
                        padded_rows.append(rows[-1])
                    else:
                        padded_rows.append(
                            f"[{color}]│[/] {' ' * (width_value - 4)} [{color}]│[/]"
                        )
                row = "  ".join(padded_rows)
                lines.append((" " * offset + row).rstrip())

        if layer_index + 1 < len(layers):
            lines.extend(
                _architecture_connector(
                    layer,
                    layers[layer_index + 1],
                    view["edges"],
                    positions,
                    diagram_width,
                )
            )

    lines.append("")
    try:
        event_limit = max(1, int(view.get("event_limit", 4)))
    except (TypeError, ValueError):
        event_limit = 4
    lines.extend(_render_architecture_events(project_agents, diagram_width, event_limit))
    return "\n".join(lines)


# ==============================================================================
# 架構樹頁：~/Projects/status/decisions/*.md 的決策節點森林
# ==============================================================================

DECISIONS_REFRESH_SECONDS = 60
DECISION_COLUMNS = (
    "date", "id", "parent", "kind", "spec", "summary",
    "decision", "evidence", "status", "agent", "merge_target",
)
DECISION_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DECISION_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
DECISION_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
DECISION_SKIP_FILES = frozenset({"FORMAT.md"})
DECISION_STATUS_MARKUP = {
    "done": ("#9ece6a", "✓"),
    "running": ("#e0af68", "●"),
    "waiting": ("#7aa2f7", "○"),
}
DECISION_KIND_MARKUP = {
    "trunk": ("#9ece6a", "trunk"),
    "branch": ("", ""),
    "merge-back": ("#bb9af7", "merge-back"),
    "abandoned": ("#f7768e", "abandoned"),
}
DECISION_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9@_./:#=\-+%]+")
DECISION_NO_LINE_START = "）)」』】》〉，、。；：！？%,.;:!?"


def wrap_decision_text(text: str, width: int) -> List[str]:
    """Wrap CJK text without splitting ASCII words or starting lines with punctuation."""
    if width <= 0:
        return [""]
    source = str(text)
    tokens: List[str] = []
    index = 0
    while index < len(source):
        match = DECISION_ASCII_TOKEN_RE.match(source, index)
        if match:
            tokens.append(match.group(0))
            index = match.end()
        else:
            tokens.append(source[index])
            index += 1
    lines: List[str] = []
    current = ""
    used = 0
    for token in tokens:
        token_width = display_width(token)
        if token_width > width:
            if current:
                lines.append(current)
                current, used = "", 0
            for char in token:
                char_width = display_width(char)
                if current and used + char_width > width:
                    lines.append(current)
                    current, used = "", 0
                current += char
                used += char_width
            continue
        if used + token_width > width and current:
            lines.append(current)
            current, used = "", 0
            if token in DECISION_NO_LINE_START and lines and display_width(lines[-1]) > 1:
                moved = lines[-1][-1]
                lines[-1] = lines[-1][:-1]
                current = moved
                used = display_width(moved)
        current += token
        used += token_width
    if current or not lines:
        lines.append(current)
    return lines


def decisions_directory() -> Path:
    """Resolve the decisions folder at call time so tests can redirect it."""
    return _env_path(
        "AGENT_WATCH_DECISIONS", Path.home() / "Projects" / "status" / "decisions"
    )


@dataclass
class DecisionNode:
    """One row of a decisions.md table; append-status rows replace the earlier row."""

    line: str
    row: int
    date: str
    id: str
    parent: str
    kind: str
    spec: str
    summary: str
    decision: str
    evidence: str
    status: str
    agent: str
    merge_target: str
    children: List["DecisionNode"] = field(default_factory=list)
    current: bool = False

    @property
    def is_done(self) -> bool:
        return self.status == "done"


@dataclass
class DecisionLine:
    """One decisions file: its roots, its current node, and its unparsable rows."""

    name: str
    title: str
    nodes: List[DecisionNode] = field(default_factory=list)
    roots: List[DecisionNode] = field(default_factory=list)
    current: Optional[DecisionNode] = None
    problems: List[str] = field(default_factory=list)


@dataclass
class DecisionBoard:
    """Every parsed line plus the directory-level errors of the last read."""

    directory: Path
    lines: List[DecisionLine] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    refreshed_at: float = 0.0


def parse_decision_table(
    text: str,
    name: str = "",
) -> Tuple[List[DecisionNode], List[str], str]:
    """Parse one decisions file. Bad rows become problem strings, never exceptions."""
    nodes: List[DecisionNode] = []
    problems: List[str] = []
    by_id: Dict[str, DecisionNode] = {}
    order: List[str] = []
    title = ""
    header_seen = False
    for row_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not title and stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            continue
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(DECISION_SEPARATOR_RE.match(cell) for cell in cells):
            continue
        if not header_seen:
            header_seen = True
            if any(cell.casefold() in DECISION_COLUMNS for cell in cells):
                continue
            problems.append(f"L{row_number} 第一列不是標準表頭，直接當資料列解讀")
        if len(cells) != len(DECISION_COLUMNS):
            problems.append(
                f"L{row_number} 欄數 {len(cells)}，需 {len(DECISION_COLUMNS)}："
                f"{truncate_display(stripped, 72)}"
            )
            continue
        values = dict(zip(DECISION_COLUMNS, cells))
        node_id = values["id"]
        if not node_id:
            problems.append(f"L{row_number} 缺少 id：{truncate_display(stripped, 72)}")
            continue
        if not DECISION_ID_RE.match(node_id):
            problems.append(
                f"L{row_number} id「{truncate_display(node_id, 24)}」格式不對"
                "（小寫英數字與 -）"
            )
            continue
        status = values["status"].casefold()
        if not DECISION_DATE_RE.match(values["date"]):
            problems.append(
                f"L{row_number} id {node_id} 日期格式異常：{values['date'] or '-'}"
            )
        if status not in DECISION_STATUS_MARKUP:
            problems.append(
                f"L{row_number} id {node_id} 未知狀態「{values['status'] or '-'}」"
                "（當成未完成）"
            )
        kind = values["kind"].casefold()
        if kind not in DECISION_KIND_MARKUP:
            problems.append(
                f"L{row_number} id {node_id} 未知種類「{values['kind'] or '-'}」"
            )
        node = DecisionNode(
            line=name,
            row=row_number,
            date=values["date"],
            id=node_id,
            parent=values["parent"],
            kind=kind,
            spec=values["spec"],
            summary=values["summary"],
            decision=values["decision"],
            evidence=values["evidence"],
            status=status,
            agent=values["agent"],
            merge_target=values["merge_target"],
        )
        previous = by_id.get(node_id)
        if previous is None:
            order.append(node_id)
        elif (
            previous.parent != node.parent
            or previous.kind != node.kind
            or previous.spec != node.spec
            or previous.merge_target != node.merge_target
        ):
            problems.append(
                f"L{row_number} id {node_id} 的 parent／kind／spec 與前一列不同，"
                "沿用最後一列"
            )
        by_id[node_id] = node
    return [by_id[node_id] for node_id in order], problems, title


def pick_current_node(nodes: List[DecisionNode]) -> Optional[DecisionNode]:
    """Latest node whose status is not done; all-done lines fall back to the newest."""
    if not nodes:
        return None
    pending = [node for node in nodes if not node.is_done]
    pool = pending or list(nodes)
    return max(pool, key=lambda node: (node.date, node.row))


def build_decision_line(name: str, text: str) -> DecisionLine:
    """Link parent references into a forest and mark the current node."""
    nodes, problems, title = parse_decision_table(text, name)
    line = DecisionLine(name=name, title=title or name, nodes=nodes, problems=problems)
    index = {node.id: node for node in nodes}
    roots: List[DecisionNode] = []
    attached: List[DecisionNode] = []
    for node in nodes:
        if node.parent in ("", "-"):
            roots.append(node)
        elif node.parent in index:
            index[node.parent].children.append(node)
            attached.append(node)
        else:
            problems.append(f"L{node.row} id {node.id} 的 parent「{node.parent}」找不到")
    reachable: Set[str] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node.id in reachable:
            continue
        reachable.add(node.id)
        stack.extend(node.children)
    for node in attached:
        if node.id not in reachable:
            problems.append(f"L{node.row} id {node.id} 的 parent 鏈形成迴圈")
    line.roots = sorted(roots, key=lambda n: (0 if n.kind == "trunk" else 1, n.date, n.row))
    for node in nodes:
        node.children.sort(key=lambda child: (child.date, child.row))
    tree_nodes = [node for node in nodes if node.id in reachable]
    line.nodes = tree_nodes
    line.current = pick_current_node(tree_nodes)
    if line.current is not None:
        line.current.current = True
    return line


def load_decision_board(directory: Optional[Path] = None, now: Optional[float] = None) -> DecisionBoard:
    """Read every decisions file. One broken file never hides the other lines."""
    folder = Path(directory) if directory is not None else decisions_directory()
    board = DecisionBoard(
        directory=folder,
        refreshed_at=time.time() if now is None else now,
    )
    if folder.exists() and not folder.is_dir():
        board.errors.append(f"決策路徑不是資料夾：{folder}")
        return board
    try:
        paths = sorted(
            path for path in folder.glob("*.md")
            if path.is_file()
            and ".bak-" not in path.name
            and path.name not in DECISION_SKIP_FILES
        )
    except OSError as exc:
        board.errors.append(f"決策目錄無法讀取：{folder}（{exc}）")
        return board
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            board.errors.append(f"{path.name} 無法讀取：{exc}")
            continue
        try:
            line = build_decision_line(path.stem, text)
        except Exception as exc:  # 單一檔案的意外不能讓整頁消失
            board.errors.append(f"{path.name} 解析失敗：{exc}")
            continue
        if line.nodes or line.problems:
            board.lines.append(line)
    board.lines.sort(key=lambda item: item.name.casefold())
    return board


class DecisionService:
    """Cache the decisions forest; reread the folder at most once every 60 seconds."""

    def __init__(self, directory: Optional[Path] = None):
        self.directory = Path(directory) if directory is not None else decisions_directory()
        self._board: Optional[DecisionBoard] = None

    def board(self, now: Optional[float] = None, force: bool = False) -> DecisionBoard:
        current = time.time() if now is None else now
        if (
            force
            or self._board is None
            or current - self._board.refreshed_at >= DECISIONS_REFRESH_SECONDS
        ):
            self._board = load_decision_board(self.directory, now=current)
        return self._board


def _walk_decision_nodes(
    nodes: List[DecisionNode],
    prefix: str = "",
    top: bool = True,
):
    """Depth-first walk yielding (node, ancestor prefix, branch glyph, child indent)."""
    for index, node in enumerate(nodes):
        last = index == len(nodes) - 1
        if top:
            branch = ""
            cont = prefix
        else:
            branch = "└─ " if last else "├─ "
            cont = prefix + ("   " if last else "│  ")
        yield node, prefix, branch, cont
        yield from _walk_decision_nodes(node.children, cont, False)


def _decision_line_header(line: DecisionLine) -> str:
    header = (
        f"[bold #7aa2f7]▌ {escape(line.name)}[/] "
        f"[#565f89]{len(line.nodes)} 節點[/]"
    )
    if line.current is not None:
        header += (
            f"[#565f89] · 目前 [/]"
            f"[bold #e0af68]{escape(line.current.id)}（{escape(line.current.status)}）[/]"
        )
    elif line.nodes:
        header += "[#565f89] · 目前：無法判定[/]"
    return header


def _render_decision_nodes(line: DecisionLine, width: int) -> List[str]:
    rows: List[str] = []
    for node, prefix, branch, _cont in _walk_decision_nodes(line.roots):
        color, glyph = DECISION_STATUS_MARKUP.get(node.status, ("#f7768e", "?"))
        status_text = f"{glyph} {node.status or '?'}"
        status_markup = f"[{color}]{escape(status_text)}[/]"
        id_markup = f"[#c4c9d4]{escape(node.id)}[/]"
        agent_text = f" @{node.agent}" if node.agent not in ("", "-") else ""
        agent_markup = (
            f" [#7dcfff]@{escape(node.agent)}[/]" if agent_text else ""
        )
        marker_text = " 目前 " if node.current else " "
        marker_markup = "[bold #0f1117 on #9ece6a] 目前 [/] " if node.current else " "
        kind_text = ""
        kind_markup = ""
        if node.kind not in ("branch", ""):
            kind_color, kind_label = DECISION_KIND_MARKUP.get(
                node.kind, ("#f7768e", node.kind or "?")
            )
            label = kind_label
            if node.kind == "merge-back" and node.merge_target not in ("", "-"):
                label = f"{kind_label}→{node.merge_target}"
            kind_text = f" {label}"
            kind_markup = f" [{kind_color}]{escape(label)}[/]"
        base_text = f"{prefix}{branch}{node.date} "
        base_markup = f"{prefix}{branch}[#565f89]{escape(node.date)}[/] "
        head_markup = ""
        for show_marker, show_agent, show_kind in (
            (True, True, True), (True, True, False), (True, False, False), (False, False, False),
        ):
            plain = (
                base_text + (marker_text if show_marker else " ") + status_text + " "
                + node.id + (agent_text if show_agent else "") + (kind_text if show_kind else "")
            )
            if display_width(plain) <= width:
                head_markup = (
                    base_markup + (marker_markup if show_marker else " ") + status_markup + " "
                    + id_markup + (agent_markup if show_agent else "")
                    + (kind_markup if show_kind else "")
                )
                break
        if not head_markup:
            head_markup = escape(
                truncate_display(
                    base_text + marker_text + status_text + " " + node.id + agent_text + kind_text,
                    width,
                )
            )
        rows.append(head_markup)
        summary_indent = prefix + ("   " if branch else "") + "  "
        summary_width = max(12, width - display_width(summary_indent))
        summary_style = "[bold #c4c9d4]" if node.current else "[#a9b1bd]"
        for chunk in wrap_decision_text(node.summary or "-", summary_width):
            rows.append(f"{summary_indent}{summary_style}{escape(chunk)}[/]")
    return rows


def render_decision_tree(
    board: DecisionBoard,
    width: int = 80,
    now: Optional[float] = None,
) -> str:
    """Render every line's decision forest with the current node highlighted."""
    current_time = time.time() if now is None else now
    available = max(20, width - 6)
    total = 0
    counts = {"running": 0, "waiting": 0, "other": 0}
    for line in board.lines:
        for node, _prefix, _branch, _cont in _walk_decision_nodes(line.roots):
            total += 1
            if node.status == "running":
                counts["running"] += 1
            elif node.status == "waiting":
                counts["waiting"] += 1
            elif node.status != "done":
                counts["other"] += 1
    header_plain = (
        f"架構樹  {len(board.lines)} 條線 · {total} 個節點 · "
        f"進行中 {counts['running']} · 等待 {counts['waiting']}"
    )
    header = (
        f"[bold #7aa2f7]架構樹[/] [#565f89]{len(board.lines)} 條線 · {total} 個節點 · "
        f"進行中 {counts['running']} · 等待 {counts['waiting']}[/]"
    )
    if counts["other"]:
        header += f" [#f7768e]狀態異常 {counts['other']}[/]"
        header_plain += f" 狀態異常 {counts['other']}"
    time_text = f"讀取 {time.strftime('%H:%M:%S', time.localtime(current_time))}"
    if display_width(header_plain) + 3 + display_width(time_text) <= available:
        refresh_text = f" · 每 {DECISIONS_REFRESH_SECONDS} 秒重讀"
        if display_width(header_plain) + 3 + display_width(time_text + refresh_text) <= available:
            time_text += refresh_text
        header += f"[#565f89] · {time_text}[/]"
    rows = [header]
    for error in board.errors:
        rows.append(f"[#f7768e]{escape(truncate_display(error, available))}[/]")
    if not board.lines:
        rows.append("[#565f89]找不到決策檔（decisions/*.md）[/]")
    for index, line in enumerate(board.lines):
        rows.append("")
        rows.append(_decision_line_header(line))
        if not line.nodes:
            rows.append("[#565f89]  （沒有可顯示的節點）[/]")
        rows.extend(_render_decision_nodes(line, available))
        if line.problems:
            rows.append(f"[#f7768e]  無法解析 {len(line.problems)} 列[/]")
            for problem in line.problems[:6]:
                rows.append(
                    f"[#f7768e]    {escape(truncate_display(problem, available - 4))}[/]"
                )
            if len(line.problems) > 6:
                rows.append(f"[#f7768e]    另有 {len(line.problems) - 6} 列[/]")
    return "\n".join(rows)


# ==============================================================================
# Tab 1: Work Tab Renderers (In-place Static Text)
# ==============================================================================

STATUS_BADGE_MARKUP = {
    "running": "[bold #0f1117 on #7dcfff] [進行中] [/]",
    "complete": "[bold #0f1117 on #9ece6a] [完成] [/]",
    "reported": "[#c0caf5 on #565f89] [已回報] [/]",
    "waiting": "[bold #0f1117 on #ff9e3b] [等待] [/]",
    "failed": "[bold #ffffff on #b8324b] [失敗] [/]",
    "blocked": "[bold #0f1117 on #ff9e3b] [受阻] [/]",
    "partial": "[bold #0f1117 on #e0af68] [部分完成] [/]",
    "no_report": "[bold #0f1117 on #ff9e3b] [無報告] [/]",
    "unconfirmed": "[#c0caf5 on #565f89] [結果未確認] [/]",
    "error": "[#c0caf5 on #565f89] [異常] [/]",
}


def display_width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(char) else
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )


def truncate_display(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    output = []
    used = 0
    limit = max(0, width - 1)
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        )
        if used + char_width > limit:
            break
        output.append(char)
        used += char_width
    return "".join(output) + "…"


def format_completion_time(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "時間未確認"
    return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Taipei")).strftime("%H:%M")


def compact_elapsed(agent: AgentInfo) -> str:
    elapsed = agent.elapsed.lstrip("~")
    if agent.start_time is None or not re.fullmatch(r"\d+:\d{2}:\d{2}", elapsed):
        return agent.elapsed
    hours, minutes, seconds = (int(part) for part in elapsed.split(":"))
    total = hours * 60 + minutes
    return ("~" if agent.elapsed.startswith("~") else "") + (f"{total}m" if total else f"{seconds}s")


def render_board_active(agents: List[AgentInfo], width: int) -> str:
    lines = []
    for agent in agents:
        badge = STATUS_BADGE_MARKUP.get(agent.status, "")
        available = max(16, width - 4)
        badge_width = display_width(Text.from_markup(badge).plain) + 1 if badge else 0
        timer = f" · 已跑 {compact_elapsed(agent)}"
        identity_width = max(8, available - badge_width - 12)
        identity = truncate_display(f"{agent.harness} · {agent.model}", max(1, identity_width - display_width(timer))) + timer
        name_width = max(1, available - badge_width - display_width(identity) - 4)
        name = truncate_display(agent.name, name_width)
        title = f"▶ {name}  {identity}"
        lines.append(f"[#7dcfff]{escape(title)}[/] {badge}")
        detail = agent.stage or agent.latest
        if detail and detail != UNKNOWN_IDENTITY:
            lines.append(f"  [#a9b1d6]{escape(truncate_display(detail, max(12, width - 6)))}[/]")
        if agent.error:
            lines.append(f"  [#ff9e3b]{escape(truncate_display(agent.error, max(12, width - 6)))}[/]")
    return "\n".join(lines)


def render_board_recent(agents: List[AgentInfo], width: int, expanded: bool = False) -> str:
    lines = []
    for agent in agents if expanded else agents[:3]:
        marker = "⚠" if agent.status == "partial" else "✓"
        label = " 已回報" if agent.status == "reported" else ""
        text = f"{marker} {format_completion_time(agent.completion_time)} {agent.name}{label} │ {agent.latest}"
        lines.append(f"[#e0af68]{escape(truncate_display(text, max(16, width - 4)))}[/]")
    if not expanded and len(agents) > 3:
        lines.append(f"[#565f89]  … 另 {len(agents) - 3} 筆  \\[e] 展開[/]")
    return "\n".join(lines)


def render_history_summary(agents: List[AgentInfo], now: float) -> str:
    history = [a for a in agents if task_bucket(a, now) == "history"]
    groups = group_agents_by_project(history)
    counts = "／".join(f"{project_label(project)} {len(tasks)}" for project, tasks in groups.items())
    return f"歷史 {len(history)} 筆" + (f"（{counts}）" if counts else "")


ATTENTION_FAILURE_STATUSES = frozenset({
    "waiting", "blocked", "failed", "no_report", "unconfirmed", "error",
})
SUPERSEDED_SUCCESS_STATUSES = frozenset({"complete", "reported", "partial"})
SUPERSEDED_REPLACEMENT_STATUSES = SUPERSEDED_SUCCESS_STATUSES | {"running"}
SUPERSEDED_SUFFIX_RE = re.compile(r"(?:-retry|-claude|-rerun|-redo|-fix|-v\d+|-?\d+)$", re.IGNORECASE)


def _normalized_task_name(name: str) -> str:
    return SUPERSEDED_SUFFIX_RE.sub("", name.casefold())


def _replacement_time(agent: AgentInfo) -> float:
    if agent.completion_time is not None:
        return agent.completion_time
    return agent.start_time or agent.log_mtime or 0.0


def compute_superseded(agents: List[AgentInfo]) -> Dict[str, str]:
    """Map an actionable old task key to a successful or running replacement key."""
    result: Dict[str, str] = {}
    replaceable = [
        agent for agent in agents
        if agent.status in ATTENTION_FAILURE_STATUSES or agent.status == "question"
    ]
    replacements = [
        agent for agent in agents
        if agent.status in SUPERSEDED_REPLACEMENT_STATUSES
    ]

    for old in replaceable:
        target = None
        hint = old.superseded_hint.casefold()
        if hint:
            matches = [
                agent for agent in replacements
                if agent.key != old.key and agent.name.casefold() == hint
            ]
            if matches:
                target = max(matches, key=_replacement_time)

        if target is None:
            matches = [
                agent for agent in replacements
                if agent.key != old.key
                and agent.name.casefold() == old.name.casefold()
                and agent.start_time is not None
                and (old.start_time is None or agent.start_time > old.start_time)
            ]
            if matches:
                target = max(matches, key=lambda agent: agent.start_time or 0.0)

        if target is None and old.completion_time is not None:
            old_family = _normalized_task_name(old.name)
            matches = [
                agent for agent in agents
                if agent.key != old.key
                and agent.status in SUPERSEDED_SUCCESS_STATUSES
                and agent.completion_time is not None
                and agent.completion_time > old.completion_time
                and _normalized_task_name(agent.name) == old_family
                and agent.name.casefold() != old.name.casefold()
            ]
            if matches:
                target = max(matches, key=lambda agent: agent.completion_time or 0.0)

        if target is not None:
            result[old.key] = target.key
    return result


BOARD_COLORS = {
    "running": "#e0af68",
    "recent": "#9ece6a",
    "history": "#737b91",
    "attention": "#f7768e",
}
BOARD_STATUS_LABELS = {
    "running": "進行中", "waiting": "等候中", "blocked": "卡住",
    "failed": "失敗", "no_report": "無報告", "unconfirmed": "待確認",
    "error": "資料錯誤", "complete": "剛完成", "reported": "已回報",
    "partial": "部分完成", "question": "等回答",
}
WIDE_BOARD_COLUMNS = 120


def board_bucket(agent: AgentInfo, now: float) -> str:
    # Keep this stateless helper for callers and legacy tests.
    if agent.status in {"question", *ATTENTION_FAILURE_STATUSES}:
        return "attention"
    return task_bucket(agent, now)


class WatchState:
    """Dashboard-owned state. Registry and question files are never modified."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else _env_path(
            "AGENT_WATCH_STATE", Path.home() / "Projects/night/agent-watch-state.json",
        )
        self.data: Dict[str, Any] = dict(version=1, tasks={}, projects={})
        self.warning = ""
        self.revision = 0
        self._observed: Dict[str, Tuple[str, Optional[float]]] = {}
        self._superseded: Dict[str, str] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._validate(data)
            self.data = data
        except FileNotFoundError:
            self.warning = "state 檔不存在，使用空狀態"
        except (OSError, ValueError, TypeError, UnicodeError):
            self.warning = "state 檔損壞或無法讀取，使用空狀態"

    @staticmethod
    def _validate(data: Any) -> None:
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("unsupported state")
        for section in ("tasks", "projects"):
            if not isinstance(data.get(section), dict):
                raise ValueError("invalid section")
            for entry in data[section].values():
                if not isinstance(entry, dict):
                    raise ValueError("invalid entry")
                for name in ("hidden", "done_manual", "read", "pinned", "restored", "collapsed", "dismissed"):
                    if name in entry and type(entry[name]) is not bool:
                        raise ValueError("invalid flag")
                if "tags" in entry and (
                    not isinstance(entry["tags"], list)
                    or not all(isinstance(tag, str) for tag in entry["tags"])
                ):
                    raise ValueError("invalid tags")
        if sum(bool(entry.get("pinned")) for entry in data["tasks"].values()) > 5:
            raise ValueError("too many pins")

    def task(self, key: str) -> Dict[str, Any]:
        return self.data["tasks"].get(key, {})

    def save(self, data: Dict[str, Any]) -> bool:
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(self.path.parent),
                prefix="." + self.path.name + "-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError) as exc:
            self.warning = "state 寫入失敗：" + str(exc)
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self.data = data
        self.revision += 1
        self.warning = ""
        return True

    @staticmethod
    def stamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def dismissed_signature(agent: AgentInfo) -> str:
        question_id = (agent.question or {}).get("id") or ""
        completion = agent.completion_time or ""
        return f"{agent.status}|{question_id}|{completion}"

    def _dismissed(self, agent: AgentInfo) -> bool:
        entry = self.task(agent.key)
        return bool(
            entry.get("dismissed")
            and entry.get("dismissed_sig") == self.dismissed_signature(agent)
        )

    def _question_is_stale(self, agent: AgentInfo, now: float) -> bool:
        question = agent.question
        if not question_is_active(question, agent.completion_time):
            return False
        if agent.process_alive is True:
            return False
        asked = question.get("asked_at_ts") or question.get("mtime") or 0
        return bool(asked and now - asked > STALE_ATTENTION_SECONDS)

    def stale_info(self, agent: AgentInfo, now: float) -> str:
        """Return the history label for a dismissed, expired, or replaced task."""
        if agent.status == "running":
            return ""
        if self._dismissed(agent):
            return "已清除"
        if agent.key in self._superseded:
            return "已被取代"
        if self._question_is_stale(agent, now):
            return "已過期"
        if agent.status in ATTENTION_FAILURE_STATUSES:
            timestamp = agent.completion_time or agent.log_mtime
            if timestamp and now - timestamp > STALE_ATTENTION_SECONDS:
                return "已過期"
        return ""

    def update_task(self, key: str, **values: Any) -> bool:
        data = copy.deepcopy(self.data)
        entry = data["tasks"].setdefault(key, {})
        entry.update(values, updated_at=self.stamp())
        return self.save(data)

    def _prune_old_tasks(
        self, data: Dict[str, Any], current_keys: Set[str], now: float,
    ) -> Tuple[bool, str]:
        expired = []
        for key, entry in data["tasks"].items():
            if key in current_keys:
                continue
            updated_at = entry.get("updated_at")
            if not isinstance(updated_at, str):
                continue
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    continue
                updated_ts = updated.timestamp()
            except (ValueError, OverflowError, OSError):
                continue
            if now - updated_ts > STATE_TASK_RETENTION_SECONDS:
                expired.append(key)
        if not expired:
            return False, ""

        try:
            source_mode = stat.S_IMODE(self.path.stat().st_mode)
            original = self.path.read_bytes()
            on_disk = json.loads(original)
            self._validate(on_disk)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            return False, "state 清理略過，原檔不可讀：" + str(exc)
        if on_disk != self.data:
            return False, "state 清理略過：檔案在本次載入後已變更"

        backup_path = self.path.with_name(
            self.path.name + ".bak-" + datetime.fromtimestamp(now).astimezone().strftime("%Y%m%d")
        )
        fd = -1
        try:
            fd = os.open(
                str(backup_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, source_mode,
            )
            os.fchmod(fd, source_mode)
            with os.fdopen(fd, "wb") as backup:
                fd = -1
                backup.write(original)
                backup.flush()
                os.fsync(backup.fileno())
        except FileExistsError:
            return False, "state 清理略過：當日備份已存在"
        except OSError as exc:
            return False, "state 清理略過，無法建立備份：" + str(exc)
        finally:
            if fd >= 0:
                os.close(fd)

        for key in expired:
            data["tasks"].pop(key, None)
        return True, ""

    def reconcile(
        self, agents: List[AgentInfo], now: float, *, registry_complete: bool = False,
    ) -> None:
        """Reconcile run state after a complete registry snapshot."""
        self._superseded = compute_superseded(agents)
        data = copy.deepcopy(self.data)
        changed = False
        cleanup_warning = ""
        if registry_complete:
            changed, cleanup_warning = self._prune_old_tasks(
                data, {agent.key for agent in agents}, now,
            )
        observed = dict(self._observed)
        for agent in agents:
            previous = self._observed.get(agent.key)
            observed[agent.key] = (agent.status, agent.start_time)
            entry = data["tasks"].get(agent.key)
            if entry is not None:
                rerun = (
                    agent.status == "running"
                    and any(entry.get(flag) for flag in (
                        "hidden", "done_manual", "restored", "read", "dismissed",
                    ))
                ) or (
                    previous is not None and (
                        (previous[0] == "running" and agent.status != "running")
                        or previous[1] != agent.start_time
                    )
                ) or (
                    entry.get("run_start") is not None and agent.start_time is not None
                    and entry["run_start"] != agent.start_time
                )
                if rerun:
                    entry.update(hidden=False, done_manual=False, restored=False, read=False,
                                 dismissed=False, run_start=agent.start_time, updated_at=self.stamp())
                    changed = True
            project = data["projects"].get(agent.project, {})
            if project.get("collapsed") and self.bucket(agent, now) in ("running", "attention"):
                project.update(collapsed=False, collapsed_at=self.stamp())
                changed = True
        saved = not changed or self.save(data)
        if saved:
            self._observed = observed
            if cleanup_warning:
                self.warning = cleanup_warning

    def bucket(self, agent: AgentInfo, now: float) -> str:
        if agent.status == "running":
            return "running"
        active_question = question_is_active(agent.question, agent.completion_time)
        if (
            active_question and not self._dismissed(agent)
            and agent.key not in self._superseded
            and not self._question_is_stale(agent, now)
        ):
            return "attention"
        disposition = self.stale_info(agent, now)
        if disposition:
            return "history"
        bucket = board_bucket(agent, now)
        if bucket in ("attention", "running"):
            return bucket
        entry = self.task(agent.key)
        if entry.get("hidden"):
            return "hidden"
        if entry.get("done_manual") or self.data["projects"].get(agent.project, {}).get("collapsed"):
            return "history"
        return "recent" if entry.get("restored") else bucket

    def apply(self, operation: str, agent: AgentInfo, agents: List[AgentInfo], now: float,
              tags: Optional[List[str]] = None) -> Tuple[bool, str]:
        entry = self.task(agent.key)
        bucket = self.bucket(agent, now)
        disposition = self.stale_info(agent, now)
        if operation == "dismiss":
            if agent.status == "running" or bucket == "running":
                return False, "進行中的任務不能清除"
            if bucket != "attention" and disposition not in ("已過期", "已被取代"):
                return False, "目前項目不在需處理區，也不是已過期或被取代項目"
            if disposition == "已清除":
                return False, "此項目已清除"
            ok = self.update_task(
                agent.key,
                dismissed=True,
                dismissed_at=self.stamp(),
                dismissed_sig=self.dismissed_signature(agent),
                run_start=agent.start_time,
            )
            if ok and agent.question:
                return True, "已清除；不會刪除或修改 QUESTION.md／ANSWER.md"
            return ok, "已清除" if ok else self.warning

        if operation in ("hide", "done") and bucket in ("running", "attention"):
            return False, "進行中或需處理的任務不能隱藏／歸到已完成"
        if operation == "collapse":
            if any(a.project == agent.project and self.bucket(a, now) in ("running", "attention") for a in agents):
                return False, "專案仍有進行中或需處理的任務，不能收起"
            data = copy.deepcopy(self.data)
            data["projects"][agent.project] = dict(collapsed=True, collapsed_at=self.stamp())
            ok = self.save(data)
        else:
            if operation == "pin" and not entry.get("pinned"):
                if sum(bool(t.get("pinned")) for t in self.data["tasks"].values()) >= 5:
                    return False, "最多只能釘選 5 個任務，請先取消一個"
            changes: Dict[str, Any] = {
                "hide": dict(hidden=True, restored=False),
                "done": dict(done_manual=True, restored=False),
                "restore": dict(hidden=False, done_manual=False, restored=True, dismissed=False),
                "undismiss": dict(hidden=False, done_manual=False, restored=True, dismissed=False),
                "read": dict(read=not entry.get("read", False)),
                "pin": dict(pinned=not entry.get("pinned", False)),
                "tags": dict(tags=list(dict.fromkeys(tag.strip() for tag in (tags or []) if tag.strip()))),
            }.get(operation, {})
            if not changes:
                return False, "未知操作"
            if operation in ("restore", "undismiss"):
                data = copy.deepcopy(self.data)
                data["tasks"].setdefault(agent.key, {}).update(
                    changes, updated_at=self.stamp(), run_start=agent.start_time,
                )
                data["projects"].setdefault(agent.project, {})["collapsed"] = False
                ok = self.save(data)
            else:
                ok = self.update_task(agent.key, run_start=agent.start_time, **changes)
        return ok, "已儲存" if ok else self.warning

    def dismiss_stale(self, agents: List[AgentInfo], now: float) -> int:
        """Clear only expired or superseded tasks after the caller confirms."""
        count = 0
        for agent in agents:
            if self.stale_info(agent, now) not in ("已過期", "已被取代"):
                continue
            ok, _ = self.apply("dismiss", agent, agents, now)
            count += int(ok)
        return count

def history_tasks(agents: List[AgentInfo], state: WatchState, now: float,
                  query: str = "", project: str = "", tag: str = "",
                  read: str = "", hidden: str = "") -> List[AgentInfo]:
    result = []
    for agent in agents:
        if state.bucket(agent, now) not in ("history", "hidden"):
            continue
        entry = state.task(agent.key)
        if project and agent.project != project:
            continue
        if tag and tag not in entry.get("tags", []):
            continue
        if read and bool(entry.get("read")) != (read == "read"):
            continue
        if hidden and bool(entry.get("hidden")) != (hidden == "hidden"):
            continue
        searchable = " ".join((agent.name, agent.latest, agent.project, project_label(agent.project),
                               " ".join(entry.get("tags", []))))
        if query.casefold() not in searchable.casefold():
            continue
        result.append(agent)
    return sorted(result, key=lambda a: (-(done_timestamp(a) or 0), a.key))


def render_task_row(agent: AgentInfo, width: int, now: float, selected: bool = False,
                    bucket: Optional[str] = None, history_label: str = "") -> Text:
    """Reserve separate elapsed/status columns before fitting task identity."""
    width = max(1, width)
    bucket = bucket or board_bucket(agent, now)
    color = BOARD_COLORS[bucket]
    marker = "› " if selected else "  "
    status = BOARD_STATUS_LABELS.get(agent.status, "待確認")
    if bucket in ("history", "hidden"):
        status = history_label or "已完成"
    if bucket in ("recent", "history"):
        line = "{}{} {} │ {} │ {}".format(
            marker, format_completion_time(agent.completion_time),
            agent.name, status, agent.latest,
        )
        return Text(truncate_display(line, width), style=color)

    elapsed = "已跑 " + compact_elapsed(agent)
    elapsed_width = max(14, display_width(elapsed))
    status_width = 8
    # Three separators plus the cursor. Shrink model, then title, then agent.
    budget = width - 2 - 9 - elapsed_width - status_width
    name_width = min(display_width(agent.name), max(1, budget - 10), 26)
    identity_width = max(0, budget - name_width)
    identity = agent.harness + " · " + agent.model
    if display_width(identity) > identity_width:
        model_width = identity_width - display_width(agent.harness + " · ")
        if model_width >= 1:
            identity = agent.harness + " · " + truncate_display(agent.model, model_width)
        else:
            identity = truncate_display(agent.harness, identity_width)
    name = truncate_display(agent.name, name_width)
    name += " " * max(0, name_width - display_width(name))
    identity += " " * max(0, identity_width - display_width(identity))
    result = Text(marker + name + " │ " + identity + " │ ", style=color)
    result.append(elapsed + " " * (elapsed_width - display_width(elapsed)), style="bold " + color)
    result.append(" │ " + status + " " * max(0, status_width - display_width(status)))
    detail = agent.stage or agent.latest or "尚無進度"
    wait_text = ""
    if agent.question:
        if agent.question.get("error"):
            detail = "問題格式錯誤：" + agent.question["error"]
        else:
            detail = agent.question.get("question") or detail
        if agent.status == "question" and not agent.question.get("answered"):
            wait_text = "  " + format_question_wait(
                question_wait_seconds(agent.question, now)
            )
    detail_width = max(1, width - display_width(wait_text))
    result.append("\n" + truncate_display("  " + detail, detail_width), style="#a9b1bd")
    if wait_text:
        overdue = question_wait_seconds(agent.question, now) >= QUESTION_OVERDUE_SECONDS
        result.append(wait_text, style="bold #f7768e" if overdue else "bold #e0af68")
    return result


def render_home_quota(
    raw: list, width: int, stale: bool = False, now: Optional[float] = None,
) -> str:
    """Show the most urgent window per provider, using the full chart's algorithm."""
    parts = []
    items = ordered_quota_items(raw)
    for provider in QUOTA_PROVIDER_NAMES:
        name = QUOTA_PROVIDER_NAMES[provider]
        candidates = []
        balances = []
        for item in items:
            if str(item.get("provider", "")).casefold() != provider:
                continue
            if isinstance(item.get("balance"), str) and item["balance"].strip():
                balances.append(item["balance"])
            if quota_item_has_data(item):
                for label, used, window in valid_quota_windows(item):
                    progress = quota_time_progress(window, now)
                    color, tag = quota_window_status(used, progress)
                    severity = 2 if tag == "告急" else 1 if tag else 0
                    candidates.append((severity, label, used, progress, color, tag))
        if candidates:
            _, label, used, progress, color, tag = max(candidates, key=lambda entry: entry[0])
            label = {"5 小時": "5h", "7 天": "7d"}.get(label, label)
            bar = render_quota_chart(used, progress, 6)
            value = f"{escape(label)} {bar} [{color}]{used:g}%{' ' + tag if tag else ''}[/]"
        elif balances:
            value = escape(balances[0])
        else:
            value = "[#737b91]無資料[/]"
        parts.append(f"{name} {value}")
    if stale:
        parts[0] = "[#f7768e]過期 [/]" + parts[0]
    separator = "   "
    lines = []
    current = ""
    for part in parts:
        candidate = current + (separator if current else "") + part
        if current and display_width(Text.from_markup(candidate).plain) > width:
            lines.append(current)
            current = part
        else:
            current = candidate
    return "\n".join(lines + [current])


def render_work_board(
    agents: List[AgentInfo], width: int = 80, now: Optional[float] = None,
    expand_recent: bool = False,
) -> str:
    current = time.time() if now is None else now
    lines = []
    for project, tasks in work_board_groups(agents, current).items():
        lines.append(f"[bold #bb9af7]▌ {escape(project_label(project))}[/]")
        active = [a for a in tasks if task_bucket(a, current) != "recent"]
        recent = [a for a in tasks if task_bucket(a, current) == "recent"]
        if active:
            lines.append(render_board_active(active, width))
        if recent:
            lines.append(render_board_recent(recent, width, expand_recent))
    lines.append(escape(render_history_summary(agents, current)))
    return "\n".join(lines)


def render_active_flow(active_agents: List[AgentInfo], width: int = 80) -> str:
    """Render active flow as bordered text cards without recreating widgets."""
    if not active_agents:
        return "[#565f89]  (目前沒有進行中的 agent 任務)[/#565f89]"

    lines = []
    card_width = min(max(width - 4, 46), 100)
    inner_width = card_width - 2
    text_width = max(24, inner_width - 3)
    groups = group_agents_by_project(active_agents)
    flat_index = 0
    grouped_items = list(groups.items())
    for group_index, (project_id, agents) in enumerate(grouped_items):
        lines.append(
            f"[bold #bb9af7]▌ {escape(project_label(project_id))} ({len(agents)})[/bold #bb9af7]"
        )
        for item_index, a in enumerate(agents):
            flat_index += 1
            num_str = f"#{flat_index:02d}"
            badge = STATUS_BADGE_MARKUP.get(a.status, "[#565f89][未知][/#565f89]")
            name = truncate_display(f"{num_str}  {a.name}", max(12, text_width - 16))
            model = truncate_display(f"模型：{a.model}", text_width)
            harness = truncate_display(f"Harness：{a.harness}", text_width)
            latest = truncate_display(f"最新：{a.latest}", text_width)
            stage = truncate_display(f"目前階段：{a.stage or UNKNOWN_IDENTITY}", text_width)

            lines.append(f"[#414868]╭{'─' * inner_width}╮[/#414868]")
            lines.append(f"[#414868]│[/#414868] [bold #e0af68]{escape(name)}[/bold #e0af68] {badge}")
            lines.append(f"[#414868]│[/#414868] [#c0caf5]{escape(harness)}[/#c0caf5]")
            lines.append(f"[#414868]│[/#414868] [#7aa2f7]{escape(model)}[/#7aa2f7]")
            lines.append(f"[#414868]│[/#414868] [#e0af68]{escape(stage)}[/#e0af68]")
            reason = a.error or ("已結束、無結果報告" if a.status == "no_report" else "")
            if reason:
                issue = truncate_display(f"原因：{reason}", text_width)
                lines.append(f"[#414868]│[/#414868] [#ff9e3b]{escape(issue)}[/#ff9e3b]")
            else:
                lines.append(f"[#414868]│[/#414868] [#a9b1d6]{escape(latest)}[/#a9b1d6]")
            lines.append(f"[#414868]╰{'─' * inner_width}╯[/#414868]")

            if item_index + 1 < len(agents):
                lines.append("                  [bold #7aa2f7]│\n                  ▼ ──▶[/bold #7aa2f7]")
        if group_index + 1 < len(grouped_items):
            lines.append("")

    return "\n".join(lines)


def render_elapsed_timers(active_agents: List[AgentInfo]) -> str:
    if not active_agents:
        return "[#565f89]計時：目前沒有進行中的工作[/#565f89]"
    entries = [f"#{idx + 1:02d} {a.elapsed}" for idx, a in enumerate(active_agents)]
    return "[#7dcfff]計時　" + escape("  ·  ".join(entries)) + "[/#7dcfff]"


def _render_done_agent(
    agent: AgentInfo,
    index: int,
    width: int,
    recent: bool,
) -> List[str]:
    badge = STATUS_BADGE_MARKUP.get(agent.status, "[#565f89][未知][/#565f89]")
    name = truncate_display(agent.name, max(12, width - 34))
    result = truncate_display(agent.latest, max(12, width - 12))
    finished = format_completion_time(agent.completion_time)
    marker = "●" if recent else "✓"
    title_color = "#9ece6a" if recent else "#c0caf5"
    recent_tag = "[bold #0f1117 on #9ece6a] 剛完成 [/] " if recent else ""
    return [
        f"[bold {title_color}]{marker} #{index:02d}[/bold {title_color}] "
        f"{recent_tag}[bold #c0caf5]{escape(name)}[/bold #c0caf5] {badge}",
        f"    [#c0caf5]Harness：{escape(truncate_display(agent.harness, 18))} | "
        f"模型：{escape(truncate_display(agent.model, max(12, width - 32)))}[/#c0caf5]",
        f"    [#e0af68]目前階段：{escape(truncate_display(agent.stage or UNKNOWN_IDENTITY, max(12, width - 18)))}"
        f" | 費時：{escape(agent.elapsed)} | 完成：{escape(finished)}[/#e0af68]",
        f"    [#9aa5ce]成果：{escape(result)}[/#9aa5ce]",
        "",
    ]


def render_recent_done_tasks(
    done_agents: List[AgentInfo],
    width: int = 80,
    now: Optional[float] = None,
) -> str:
    """Render the 2-hour completion queue above active work."""
    recent, _ = split_recent_done(done_agents, now)
    if not recent:
        return ""
    groups = group_agents_by_project(recent)
    ordered = sorted(
        groups.items(),
        key=lambda item: max((done_timestamp(agent) or 0.0) for agent in item[1]),
        reverse=True,
    )
    lines = ["[bold #0f1117 on #9ece6a] 剛完成 · 最近 2 小時 · 最新在上 [/]"]
    index = 0
    for group_index, (project_id, agents) in enumerate(ordered):
        lines.append(f"[bold #9ece6a]▌ {escape(project_label(project_id))} ({len(agents)})[/]")
        for agent in agents:
            index += 1
            lines.extend(_render_done_agent(agent, index, width, recent=True))
        if group_index + 1 < len(ordered):
            lines.append("")
    return "\n".join(lines).strip()


def render_done_tasks(done_agents: List[AgentInfo], width: int = 80) -> str:
    """Render older completed tasks grouped by project."""
    if not done_agents:
        return "[#565f89]  (目前沒有其他已完成的 agent 任務)[/#565f89]"

    ordered_agents = sorted(
        done_agents,
        key=lambda agent: done_timestamp(agent) or 0.0,
        reverse=True,
    )
    groups = group_agents_by_project(ordered_agents)
    lines = []
    index = 0
    for group_index, (project_id, agents) in enumerate(groups.items()):
        lines.append(f"[bold #bb9af7]▌ {escape(project_label(project_id))} ({len(agents)})[/]")
        for agent in agents:
            index += 1
            lines.extend(_render_done_agent(agent, index, width, recent=False))
        if group_index + 1 < len(groups):
            lines.append("")
    return "\n".join(lines).strip()


def notify_completion(agent: AgentInfo):
    """Notify on a terminal result or a stalled run."""
    name_escaped = agent.name.replace('"', '\\"')
    labels = {"complete": "已完成", "partial": "部分完成", "failed": "失敗", "blocked": "受阻", "waiting": "等待中"}
    label = labels.get(agent.status, agent.status)
    script = f'display notification "{name_escaped} {label}" with title "Agent Watch"'
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        pass
    try:
        subprocess.Popen(
            ["afplay", f"/System/Library/Sounds/{'Glass' if agent.status == 'complete' else 'Basso' if agent.status == 'failed' else 'Ping'}.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        pass


def notify_question_overdue(agent: AgentInfo):
    """Notify once when a QUESTION.md has waited past the 30-minute limit."""
    question = agent.question or {}
    label = question.get("id") or "問題"
    message = "%s 的 %s 已等超過 30 分鐘" % (agent.name, label)
    script = 'display notification "%s" with title "Agent Watch · 等回答"' % message.replace('"', '\\"')
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        pass
    try:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Basso.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        pass


def run_once_summary(
    runs_path: Path = DEFAULT_RUNS_TSV,
    done_path: Path = DEFAULT_DONE_TSV,
) -> int:
    """Print one-time plain text summary to stdout and exit 0."""
    now = time.time()
    log_parser = LogParser()
    agents, errors = load_all_agents(runs_path, done_path, log_parser, now)
    quota_svc = QuotaService()
    quota_text, quota_raw = quota_svc.fetch(force=True)
    _, _, quota_stale, _ = quota_svc.snapshot()
    if quota_stale:
        quota_text += "（資料過期）"

    print("=" * 80)
    print(f" AGENT WATCH v8 (單次摘要)  |  時間: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}")
    print(render_compact_quota(quota_text, 80))
    print(format_bucket_counts(agents, now))
    print("【工作看板】")
    # Plain output has no viewport: retain full task names for search and review.
    board_width = max([80] + [
        display_width(a.name) + display_width(a.harness) + display_width(a.model) + 40
        for a in agents if task_bucket(a, now) != "history"
    ])
    print(Text.from_markup(render_work_board(agents, board_width, now)).plain)

    if errors:
        print("資料提示:")
        for err in errors:
            print(f"  * {err}")

    print("\n【分頁 2：架構狀態】")
    view = ARCHITECTURE_DATA["architectures"]["now"]
    for project_id, project in ARCHITECTURE_DATA["projects"].items():
        project_agents = [
            agent for agent in agents
            if (agent.project or PROJECT_OTHER) == project_id
        ]
        assignments = _assign_agents_to_nodes(project_agents, view)
        print(f"  ── {project['label']} ──")
        for layer in view["layers"]:
            for node_id in layer:
                node = view["nodes"][node_id]
                state, marker, progress = _node_runtime(assignments.get(node_id, []))
                print(
                    f"  * {node.get('label', node_id)} [{node.get('model', '未知')}]"
                    f" {marker} {state} · {progress}"
                )

    print("\n【分頁 3：額度詳情】")
    for line in render_plain_quota_details(quota_raw):
        print(line)

    candidates = [a for a in agents if a.log_mtime > 0 and a.log_lines]
    followed = max(candidates, key=lambda a: a.log_mtime) if candidates else None
    print("\n================================================================================")
    print(f" 最近更新 LOG: {followed.name if followed else '無可讀 LOG'}")
    print("--------------------------------------------------------------------------------")
    if followed and followed.log_lines:
        for line in followed.log_lines[-8:]:
            print(f"  {line}")
    else:
        print("  (無即時 log)")
    print("================================================================================")
    return 0


# ==============================================================================
# Textual App Implementation (Flicker-Free, Multi-tab)
# ==============================================================================

APP_CSS = """
Screen {
    background: #101216;
    color: #c4c9d4;
}
#top-bar {
    height: 1;
    background: #1b1e24;
}
#top-title {
    width: 9;
    color: #a9b1bd;
}
.nav-tab {
    width: 11;
    height: 1;
    color: #a9b1bd;
}
.nav-tab.current {
    color: #9ece6a;
    text-style: bold;
}
#clock {
    width: 1fr;
    text-align: right;
    padding-right: 1;
}
#main-tabs {
    height: 1fr;
}
#main-tabs ContentTabs {
    display: none;
}
TabPane {
    padding: 0;
}
#compact-quota-line {
    height: auto;
    max-height: 2;
    padding: 0 1;
    background: #181b20;
}
#work-page {
    layout: vertical;
    height: 1fr;
    padding: 0 1;
}
#work-page.wide {
    layout: horizontal;
}
#work-scroll {
    width: 1fr;
    height: 1fr;
    min-height: 3;
    scrollbar-size: 1 1;
}
#work-page.wide #work-scroll {
    width: 2fr;
    margin-right: 1;
}
#data-errors {
    height: auto;
    color: #f7768e;
}
#attention-box, .project-section, #pinned-box {
    height: auto;
    border: solid #444b59;
    padding: 0 1;
    border-title-color: #a9b1bd;
}
#attention-box {
    border: solid #f7768e;
    border-title-color: #f7768e;
}
#attention-list, #work-board {
    height: auto;
}
#pinned-box {
    color: #737b91;
}
.empty-row, .more-recent, #top-stats, #history-summary {
    height: 1;
    color: #737b91;
}
.task-row {
    height: auto;
    min-height: 1;
}
.task-row:focus, .task-row.selected {
    background: #222831;
}
.project-section.selected, #attention-box.selected {
    border: solid #9ece6a;
    border-title-color: #9ece6a;
}
#work-log-box {
    height: 7;
    min-height: 7;
    border: solid #444b59;
    border-title-color: #a9b1bd;
    padding: 0 1;
}
#work-page.wide #work-log-box {
    width: 1fr;
    height: 1fr;
}
#bottom-log-title {
    height: 1;
    color: #c4c9d4;
    text-style: bold;
}
#preview-scroll {
    height: 1fr;
    scrollbar-size: 1 1;
}
#bottom-log-content {
    height: auto;
    color: #a9b1bd;
}
#key-bar {
    height: 1;
    background: #1b1e24;
    color: #a9b1bd;
    padding-left: 1;
}
#arch-display, #quota-display, #history-display, #tree-display {
    height: auto;
}
#tree-scroll {
    padding: 0 1;
}
#state-message {
    height: auto;
    max-height: 2;
    color: #e0af68;
    padding: 0 1;
}
#history-page {
    padding: 0 1;
}
#history-filters {
    height: 3;
}
#history-filters Select {
    width: 1fr;
}
#history-search {
    height: 3;
    display: none;
}
#history-table {
    height: 1fr;
    color: #a9b1bd;
}
TagScreen {
    align: center middle;
}
#tag-panel {
    width: 70;
    max-width: 100%;
    height: 7;
    border: solid #9ece6a;
    background: #101216;
    padding: 0 1;
}
HelpScreen {
    background: #101216;
}
#help-scroll {
    border: solid #9ece6a;
    border-title-color: #9ece6a;
    padding: 1 2;
}
#help-content {
    height: auto;
}
TerminalScreen {
    background: #101216;
}
#terminal-panel {
    height: 1fr;
    border: solid #9ece6a;
    border-title-color: #9ece6a;
    background: #101216;
    padding: 0 1;
}
#terminal-title {
    height: 1;
    color: #9ece6a;
    text-style: bold;
}
#terminal-scroll {
    height: 1fr;
    scrollbar-size: 1 1;
}
#terminal-content {
    height: auto;
    color: #c4c9d4;
}
#terminal-search {
    height: 3;
    display: none;
}
#terminal-hint {
    height: 1;
    color: #737b91;
}
QuestionScreen {
    align: center middle;
    background: #101216;
}
#question-panel {
    width: 90%;
    height: auto;
    max-height: 90%;
    border: solid #f7768e;
    border-title-color: #f7768e;
    background: #101216;
    padding: 0 1;
}
#question-agent {
    height: 1;
    color: #f7768e;
    text-style: bold;
}
#question-text, #question-error, #question-raw {
    height: auto;
    margin-bottom: 1;
}
#question-error {
    color: #f7768e;
}
#question-raw {
    color: #a9b1bd;
}
#question-options {
    height: auto;
    max-height: 8;
}
#question-note {
    height: 3;
}
#question-hint {
    height: 1;
    color: #737b91;
}
DismissConfirmScreen {
    align: center middle;
    background: #101216;
}
#dismiss-confirm-panel {
    width: 72;
    max-width: 100%;
    height: auto;
    border: solid #e0af68;
    background: #101216;
    padding: 0 1;
}
#dismiss-confirm-text {
    height: auto;
}
"""


def render_data_errors(errors: List[str], agents: List[AgentInfo], width: int) -> str:
    messages = list(errors)
    messages.extend(f"{a.name}：{a.error}" for a in agents if a.error)
    unique = list(dict.fromkeys(messages))
    if not unique:
        return ""
    lines = [
        f"[#ff9e3b]資料問題：{escape(truncate_display(message, max(16, width - 8)))}[/#ff9e3b]"
        for message in unique[:2]
    ]
    if len(unique) > 2:
        lines.append(f"[#ff9e3b]另有 {len(unique) - 2} 項資料問題[/#ff9e3b]")
    return "\n".join(lines)


class RunDataService:
    """One background reader owns the parser; UI reads complete snapshots."""

    def __init__(self, runs_path: Path, done_path: Path, parser: LogParser):
        self.runs_path = runs_path
        self.done_path = done_path
        self.parser = parser
        self._lock = threading.Lock()
        self._fetching = False
        self._has_snapshot = False
        self._snapshot: Tuple[List[AgentInfo], List[str]] = ([], [])
        self.on_ready: Optional[Callable[[], None]] = None

    def snapshot(self) -> Tuple[List[AgentInfo], List[str]]:
        with self._lock:
            agents, errors = self._snapshot
            return [copy.copy(agent) for agent in agents], list(errors)

    @property
    def has_snapshot(self) -> bool:
        with self._lock:
            return self._has_snapshot

    def refresh_in_background(self) -> bool:
        with self._lock:
            if self._fetching:
                return False
            self._fetching = True

        def read() -> None:
            try:
                result = load_all_agents(self.runs_path, self.done_path, self.parser)
            except Exception as exc:
                result = ([], [f"讀取工作資料失敗：{exc}"])
            with self._lock:
                self._snapshot = result
                self._has_snapshot = True
                self._fetching = False
            if self.on_ready is not None:
                self.on_ready()

        threading.Thread(target=read, daemon=True, name="agent-watch-data").start()
        return True


class NavTab(Static):
    def __init__(self, label: str, tab_id: str):
        super().__init__(label, classes="nav-tab", markup=False)
        self.tab_id = tab_id

    def on_click(self) -> None:
        self.app.action_switch_tab(self.tab_id)


class HistoryTable(DataTable):
    # ScrollView otherwise consumes Space before it reaches the App binding.
    BINDINGS = [("space", "app.toggle_history_group", "收合專案組")]


class TaskRow(Static, can_focus=True):
    class Selected(Message):
        def __init__(self, key: str):
            super().__init__()
            self.key = key

    def __init__(self, agent: AgentInfo, now: float):
        super().__init__(classes="task-row")
        self.agent = agent
        self.now = now
        self.selected = False

    def render(self) -> Text:
        agent = copy.copy(self.agent)
        entry = self.app.state.task(agent.key)
        bucket = self.app.state.bucket(agent, self.now)
        if bucket not in ("running", "attention") and not entry.get("read"):
            agent.name = "● " + agent.name
        if entry.get("tags"):
            agent.name += " [" + ", ".join(entry["tags"]) + "]"
        return render_task_row(
            agent, self.content_size.width, self.now, self.selected, bucket,
            self.app.state.stale_info(agent, self.now),
        )

    def on_focus(self) -> None:
        self.post_message(self.Selected(self.agent.key))

    def on_click(self) -> None:
        self.focus()
        self.post_message(self.Selected(self.agent.key))


# Dashboard-wide actions that must stay inert while the full-screen help table is
# up. Textual 8.2.8 already drops App bindings behind a modal screen, but the App
# is the last resort in the binding chain (and priority bindings skip that
# truncation), so the intent is stated explicitly and tested.
HELP_BLOCKED_ACTIONS = frozenset({
    "switch_tab",               # 1-4
    "toggle_recent",            # e
    "cycle_project",            # p
    "show_architecture_now",    # n
    "show_architecture_future",  # f
    "hide_task", "finish_task", "collapse_project", "toggle_pin", "toggle_read",
    "edit_tags", "restore_task", "dismiss_task", "dismiss_stale",
    "toggle_history_mode", "toggle_history_group", "search_history",
    "next_task", "previous_task", "open_task",
})


class TagScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss(None)", "取消")]

    def __init__(self, tags: List[str]):
        super().__init__()
        self.tags = tags

    def compose(self) -> ComposeResult:
        with Vertical(id="tag-panel") as panel:
            panel.border_title = "自訂標籤"
            yield Label("用逗號分隔；留空清除。Enter 儲存，Esc 取消。")
            yield Input(value=", ".join(self.tags), id="tag-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss([tag.strip() for tag in re.split("[,，]", event.value) if tag.strip()])


class HelpScreen(ModalScreen):
    BINDINGS = [
        ("escape", "dismiss", "返回"),
        ("question_mark", "dismiss", "返回"),
        # The table itself advertises `q` as the way out, so keep it live here.
        ("q", "app.quit", "離開"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-scroll") as panel:
            panel.border_title = "快捷鍵 · Esc / ? 返回"
            yield Static(
                "[bold #9ece6a]AGENT WATCH v10 · 快捷鍵[/]\n\n"
                "\\[1] 工作    \\[2] 歷史    \\[3] 額度    \\[4] 架構    \\[5] 架構樹\n\n"
                "[bold]工作頁[/]\n"
                "  j / ↓        下一筆任務\n"
                "  k / ↑        上一筆任務\n"
                "  Tab / Shift+Tab  移動焦點\n"
                "  滑鼠點選      選中任務並預覽\n"
                "  Enter        深入畫面：全螢幕唯讀終端機（Esc 返回）\n"
                "               有 QUESTION.md 時開選項彈窗\n"
                "  e            展開／收起各專案的剛完成清單\n\n"
                "  h / d        隱藏／歸到已完成（僅已結束且無需處理）\n"
                "  x            清除所選需處理項目；不會刪除問題檔\n"
                "  X            確認後清除已過期／已被取代項目\n"
                "  c            收起所選任務的專案（有進行中／需處理時拒絕）\n"
                "  i            釘選／取消釘選（最多 5 個）\n"
                "  t            編輯自訂標籤（逗號分隔）\n"
                "  u            切換已讀／未讀\n\n"
                "[bold]歷史頁[/]\n"
                "  /            搜尋；Enter 回清單；Esc 清除搜尋\n"
                "  Tab          移至專案／標籤／已讀／隱藏篩選\n"
                "  g            切換表格／依專案分組\n"
                "  Space / Enter  收合／展開選中的專案組\n"
                "  j / k / ↑ / ↓  選取列\n"
                "  r            撈回主頁（含已清除任務）\n"
                "  i / t / u    釘選／標籤／已讀切換\n\n"
                "[bold]架構頁[/]\n"
                "  p            切換專案\n"
                "  n / f        現在／未來架構\n\n"
                "[bold]架構樹頁[/]\n"
                "  R            立刻重讀 decisions/*.md（平常每 60 秒自動）\n"
                "  ↑↓ / PgUp / PgDn  捲動（滑鼠滾輪亦可）\n\n"
                "[bold]全域[/]\n"
                "  ?            開啟／關閉此表\n"
                "  Esc          關閉此表\n"
                "  q            離開看板",
                id="help-content",
            )


class TerminalScreen(ModalScreen):
    """Full-screen read-only tail of a run log; Esc returns without touching the run."""

    BINDINGS = [
        ("escape", "back", "返回"),
        ("slash", "focus_search", "搜尋"),
        ("j", "scroll_down", "下一行"),
        ("k", "scroll_up", "上一行"),
        ("home", "scroll_home", "頂端"),
        ("end", "scroll_end", "末端"),
    ]

    def __init__(self, agent: AgentInfo):
        super().__init__()
        self.agent = agent
        self.query_text = ""
        self._last_markup = None

    def compose(self) -> ComposeResult:
        with Vertical(id="terminal-panel") as panel:
            panel.border_title = "深入畫面 · Esc 返回"
            yield Static(self.title_text(), id="terminal-title", markup=False)
            with VerticalScroll(id="terminal-scroll"):
                yield Static("讀取 run.log…", id="terminal-content", markup=False)
            yield Input(placeholder="搜尋日誌；Enter 套用，Esc 清除", id="terminal-search")
            yield Static(
                "j/k ↑↓ 捲動 · PgUp/PgDn 翻頁 · / 搜尋 · Esc 返回（唯讀，不會影響任務）",
                id="terminal-hint",
            )

    def title_text(self) -> str:
        name = "無 log" if str(self.agent.log_path) == "/dev/null" else Path(self.agent.log_path).name
        return "%s · %s %s · %s（唯讀）" % (
            self.agent.name, self.agent.harness, self.agent.model, name,
        )

    def on_mount(self) -> None:
        self.refresh_content()
        self.query_one("#terminal-scroll", VerticalScroll).focus()
        self.set_interval(1.0, self.refresh_content)

    def refresh_content(self) -> None:
        scroll = self.query_one("#terminal-scroll", VerticalScroll)
        at_bottom = scroll.scroll_offset.y >= max(0, scroll.max_scroll_y - 1)
        lines = humanize_log_lines(read_log_tail(Path(self.agent.log_path)))
        content = self.query_one("#terminal-content", Static)
        if lines:
            text = render_terminal_text(lines, self.query_text)
        else:
            text = Text("（沒有可讀的日誌內容）", style="#737b91")
        markup = text.plain + "|" + self.query_text
        if markup == self._last_markup:
            return
        self._last_markup = markup
        content.update(text)
        if at_bottom:
            self.call_after_refresh(scroll.scroll_end, animate=False)

    def action_focus_search(self) -> None:
        search = self.query_one("#terminal-search", Input)
        search.display = True
        search.focus()

    def action_back(self) -> None:
        search = self.query_one("#terminal-search", Input)
        if search.display or self.query_text:
            search.value = ""
            search.display = False
            self.query_text = ""
            self._last_markup = None
            self.refresh_content()
            self.query_one("#terminal-scroll", VerticalScroll).focus()
            return
        self.dismiss()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "terminal-search":
            self.query_text = event.value.strip()
            self._last_markup = None
            self.refresh_content()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "terminal-search":
            event.input.display = False
            self.query_text = event.value.strip()
            self._last_markup = None
            self.refresh_content()
            self.query_one("#terminal-scroll", VerticalScroll).focus()

    def action_scroll_down(self) -> None:
        self.query_one("#terminal-scroll", VerticalScroll).scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self.query_one("#terminal-scroll", VerticalScroll).scroll_up(animate=False)

    def action_scroll_home(self) -> None:
        self.query_one("#terminal-scroll", VerticalScroll).scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        self.query_one("#terminal-scroll", VerticalScroll).scroll_end(animate=False)


class QuestionScreen(ModalScreen):
    """SPEC §3.3 options popup; malformed questions only show their raw text."""

    BINDINGS = [("escape", "dismiss(None)", "取消")]

    def __init__(self, agent: AgentInfo, question: Dict[str, Any]):
        super().__init__()
        self.agent = agent
        self.question = question

    def compose(self) -> ComposeResult:
        question = self.question
        with Vertical(id="question-panel") as panel:
            panel.border_title = "等回答 · %s" % self.agent.name
            wait = format_question_wait(question_wait_seconds(question, time.time()))
            yield Static(
                "%s · %s%s" % (
                    self.agent.name, wait,
                    " · 已等超過 30 分鐘" if question_wait_seconds(
                        question, time.time()) >= QUESTION_OVERDUE_SECONDS else "",
                ),
                id="question-agent", markup=False,
            )
            if question.get("error"):
                yield Static(
                    "問題格式錯誤：%s（只顯示原文，不能選）" % question["error"],
                    id="question-error", markup=False,
                )
                yield Static(question.get("raw", ""), id="question-raw", markup=False)
            else:
                yield Static(question.get("question", ""), id="question-text", markup=False)
                yield OptionList(
                    *[Option("%s  %s" % (code, label), id=code)
                      for code, label in question.get("options", [])],
                    id="question-options",
                )
                yield Input(
                    placeholder="可留一行備註（Tab 到這裡，Enter 送出）",
                    id="question-note",
                )
            yield Static(
                "選項 ↑↓ 移動 · Enter 送出並寫 ANSWER.md · Esc 取消",
                id="question-hint",
            )

    def on_mount(self) -> None:
        options = self.query("#question-options")
        if options:
            options.first().focus()
    def _dismiss_with(self, choice: str, note: str = "") -> None:
        self.dismiss(dict(choice=choice, note=note))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._dismiss_with(str(event.option.id or ""))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "question-note":
            return
        event.stop()
        options = self.query("#question-options")
        codes = [code for code, _ in self.question.get("options", [])]
        if not options or not codes:
            return
        index = options.first().highlighted
        choice = codes[index if index is not None else 0]
        self._dismiss_with(choice, event.value)


def render_terminal_text(lines: List[str], query: str = "") -> Text:
    """Render humanized tail lines; a search query is highlighted per line."""
    text = Text()
    query_folded = query.casefold()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        if not query_folded:
            text.append(line, style="#c4c9d4")
            continue
        folded = line.casefold()
        start = 0
        position = folded.find(query_folded, start)
        if position < 0:
            text.append(line, style="#c4c9d4")
            continue
        while position >= 0:
            text.append(line[start:position], style="#c4c9d4")
            text.append(line[position:position + len(query)], style="bold #e0af68")
            start = position + len(query)
            position = folded.find(query_folded, start)
        text.append(line[start:], style="#c4c9d4")
    return text


class DismissConfirmScreen(ModalScreen):
    BINDINGS = [("enter", "confirm", "確認"), ("escape", "cancel", "取消")]

    def __init__(self, count: int):
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="dismiss-confirm-panel"):
            yield Static(
                f"清除 {self.count} 筆已過期／已被取代任務？\nEnter 確認；Esc 取消。",
                id="dismiss-confirm-text", markup=False,
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class AgentWatchApp(App):
    """Textual terminal dashboard for monitoring agents with tabs and flicker-free updates."""

    CSS = APP_CSS
    BINDINGS = [
        ("q", "quit", "離開 (Quit)"),
        ("1", "switch_tab('tab-work')", "工作 (1)"),
        ("2", "switch_tab('tab-history')", "歷史 (2)"),
        ("3", "switch_tab('tab-quota')", "額度 (3)"),
        ("4", "switch_tab('tab-arch')", "架構 (4)"),
        ("5", "switch_tab('tab-tree')", "架構樹 (5)"),
        ("j", "next_task", "下一筆"),
        ("down", "next_task", "下一筆"),
        ("k", "previous_task", "上一筆"),
        ("up", "previous_task", "上一筆"),
        ("enter", "open_task", "深入畫面"),
        ("question_mark", "show_help", "快捷鍵"),
        ("e", "toggle_recent", "展開剛完成 (E)"),
        ("p", "cycle_project", "切換專案 (P)"),
        ("n", "show_architecture_now", "現在 (N)"),
        ("f", "show_architecture_future", "未來 (F)"),
        ("h", "hide_task", "隱藏"),
        ("d", "finish_task", "歸到已完成"),
        ("x", "dismiss_task", "清除需處理項目"),
        ("shift+x", "dismiss_stale", "清除過期／取代項目"),
        ("c", "collapse_project", "收起專案"),
        ("i", "toggle_pin", "釘選"),
        ("t", "edit_tags", "標籤"),
        ("u", "toggle_read", "已讀"),
        ("r", "restore_task", "撈回主頁"),
        ("g", "toggle_history_mode", "表格／分組"),
        # Textual 8 sends uppercase letters as "R"; "shift+r" would never match.
        ("R", "reload_decisions", "重讀架構樹"),
        ("space", "toggle_history_group", "收合專案組"),
        ("slash", "search_history", "搜尋"),
        ("escape", "clear_history_search", "清除搜尋"),
    ]

    def __init__(
        self,
        runs_path: Path = DEFAULT_RUNS_TSV,
        done_path: Path = DEFAULT_DONE_TSV,
        state_path: Optional[Path] = None,
        decisions_path: Optional[Path] = None,
    ):
        super().__init__()
        self.runs_path = runs_path
        self.done_path = done_path
        self.state = WatchState(state_path)
        self.state_message = self.state.warning
        self.log_parser = LogParser()
        self.data_svc = RunDataService(runs_path, done_path, self.log_parser)
        self.quota_svc = QuotaService()
        self.decision_svc = DecisionService(decisions_path)
        self.notified_keys: Set[str] = set()
        self.notified_questions: Set[str] = set()
        self.previous_status: Dict[str, str] = {}
        self.is_first_load = True
        self.last_snapshot: Any = None
        self.redraw_count: int = 0
        self.last_timer_snapshot: Any = None
        self.timer_update_count: int = 0
        self._ui_ready = False
        self.ui_error = ""
        self.current_agents: List[AgentInfo] = []
        self.architecture_view_id = "now"
        ensure_architecture_data()
        self.architecture_project_id = next(iter(ARCHITECTURE_DATA["projects"]))
        self.current_active_agents: List[AgentInfo] = []
        self.expand_recent = False
        self.selected_key: Optional[str] = None
        self._row_widgets: Dict[str, TaskRow] = {}
        self._board_structure: Any = None
        self.layout_mode = "stacked"
        self.history_grouped = False
        self.history_collapsed: Set[str] = set()
        self.history_query = ""
        self.history_filters = dict(project="", tag="", read="", hidden="")
        self.history_rows: List[Tuple[str, str]] = []
        self._history_snapshot: Any = None
        self._history_options: Any = None
        self._now = time.time()
        self._preview_read_key: Optional[str] = None
        self._last_preview_key: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Label(" AW v10", id="top-title")
            for number, label, tab_id in (
                (1, "工作", "tab-work"), (2, "歷史", "tab-history"),
                (3, "額度", "tab-quota"), (4, "架構", "tab-arch"),
                (5, "架構樹", "tab-tree"),
            ):
                yield NavTab(f"[{number}] {label}", tab_id)
            yield Label("", id="clock")
        yield Static("額度：載入中...", id="compact-quota-line")
        yield Static(self.state_message, id="state-message", markup=False)

        with TabbedContent(initial="tab-work", id="main-tabs"):
            with TabPane("[1] 工作", id="tab-work"):
                with Vertical(id="work-page"):
                    with VerticalScroll(id="work-scroll"):
                        yield Static(id="data-errors")
                        with Vertical(id="attention-box") as attention:
                            attention.border_title = "需處理"
                            yield Vertical(id="attention-list")
                        with Vertical(id="pinned-box") as pinned:
                            pinned.border_title = "釘選 · 0/5"
                            yield Static("尚無釘選", classes="empty-row")
                        yield Vertical(id="work-board")
                        yield Label("", id="top-stats")
                        yield Static(id="history-summary")
                    with Vertical(id="work-log-box") as preview:
                        preview.border_title = "預覽 · 跟隨游標"
                        yield Label("尚未選中任務", id="bottom-log-title", markup=False)
                        with VerticalScroll(id="preview-scroll"):
                            yield Static("尚無即時日誌", id="bottom-log-content", markup=False)

            with TabPane("[2] 歷史", id="tab-history"):
                with Vertical(id="history-page"):
                    yield Input(placeholder="/ 搜尋名稱、專案、標籤、結果摘要", id="history-search")
                    with Horizontal(id="history-filters"):
                        yield Select([("專案：全部", "")], value="", allow_blank=False, id="filter-project")
                        yield Select([("標籤：全部", "")], value="", allow_blank=False, id="filter-tag")
                        yield Select([("已讀：全部", ""), ("已讀", "read"), ("未讀", "unread")],
                                     value="", allow_blank=False, id="filter-read")
                        yield Select([("隱藏：全部", ""), ("已隱藏", "hidden"), ("未隱藏", "visible")],
                                     value="", allow_blank=False, id="filter-hidden")
                    yield Static(id="history-display", markup=False)
                    yield HistoryTable(id="history-table", cursor_type="row", zebra_stripes=True)
            with TabPane("[3] 額度", id="tab-quota"):
                with VerticalScroll(id="quota-scroll"):
                    yield Static(id="quota-display")
            with TabPane("[4] 架構", id="tab-arch"):
                with VerticalScroll(id="arch-scroll"):
                    yield Static(id="arch-display")
            with TabPane("[5] 架構樹", id="tab-tree"):
                with VerticalScroll(id="tree-scroll"):
                    yield Static(id="tree-display")

        yield Static("1–5 分頁 │ j/k ↑↓ 選取 │ Enter 深入 │ e 展開 │ ? 快捷鍵 │ q 離開", id="key-bar")

    def action_toggle_recent(self) -> None:
        self.expand_recent = not self.expand_recent
        self.last_snapshot = None
        self.update_data()

    def on_resize(self, event) -> None:
        if self._ui_ready:
            self.update_layout(event.size.width)
            self.call_after_refresh(self.refresh_after_resize)

    def refresh_after_resize(self) -> None:
        self.last_snapshot = None
        self.update_data()

    def update_layout(self, width: Optional[int] = None) -> None:
        columns = self.size.width if width is None else width
        self.layout_mode = "split" if columns >= WIDE_BOARD_COLUMNS else "stacked"
        self.query_one("#work-page").set_class(self.layout_mode == "split", "wide")

    def update_board(self, agents: List[AgentInfo], now: float, width: int) -> None:
        attention = [a for a in agents if self.state.bucket(a, now) == "attention"]
        pinned = [a for a in agents if self.state.task(a.key).get("pinned")
                  and self.state.bucket(a, now) not in ("attention", "hidden")
                  and not self.state.task(a.key).get("done_manual")
                  and not self.state.data["projects"].get(a.project, {}).get("collapsed")]
        pinned_keys = {a.key for a in pinned}
        groups = group_agents_by_project(
            [a for a in agents if self.state.bucket(a, now) in ("running", "recent")
             and a.key not in pinned_keys],
        )
        for tasks in groups.values():
            tasks.sort(key=lambda a: (
                0 if self.state.bucket(a, now) == "running" else 1,
                -(a.start_time or 0) if a.status == "running" else -(done_timestamp(a) or 0),
                a.key,
            ))
        groups = dict(sorted(groups.items(), key=lambda item: (
            min(0 if a.status == "running" else 1 for a in item[1]),
            -max((done_timestamp(a) or 0 for a in item[1] if is_done_agent(a)), default=0),
            project_sort_key(item[0]),
        )))
        visible_groups = []
        for project, tasks in groups.items():
            running = [a for a in tasks if self.state.bucket(a, now) == "running"]
            recent = [a for a in tasks if self.state.bucket(a, now) == "recent"]
            visible = running + (recent if self.expand_recent else recent[:3])
            visible_groups.append(
                (project, visible, len(recent) - min(3, len(recent)), len(recent))
            )
        structure = (
            tuple(a.key for a in attention),
            tuple(a.key for a in pinned),
            tuple(
                (project, tuple(a.key for a in tasks), extra, recent_count)
                for project, tasks, extra, recent_count in visible_groups
            ),
            self.expand_recent,
        )
        if structure == self._board_structure:
            self.update_board_timers(agents, now, width)
            self.update_selection()
            return
        self._board_structure = structure
        restore_focus = isinstance(self.focused, TaskRow) or self.selected_key is None
        board = self.query_one("#work-board", Vertical)
        board.remove_children()
        attention_list = self.query_one("#attention-list", Vertical)
        attention_list.remove_children()
        pinned_box = self.query_one("#pinned-box", Vertical)
        pinned_box.remove_children()
        pinned_box.border_title = "釘選 · %d/5" % sum(
            bool(entry.get("pinned")) for entry in self.state.data["tasks"].values()
        )
        self._row_widgets = {}

        def row(agent: AgentInfo) -> TaskRow:
            widget = TaskRow(agent, now)
            self._row_widgets[agent.key] = widget
            return widget

        attention_list.mount(*(
            [row(a) for a in attention] if attention
            else [Static("目前沒有需處理任務", classes="empty-row")]
        ))
        self.query_one("#attention-box").border_title = (
            f"需處理 · {len(attention)} · 等回答／卡住／失敗／無報告"
        )
        pinned_box.mount(*([row(a) for a in pinned] if pinned else [
            Static("尚無可顯示的釘選", classes="empty-row"),
        ]))
        sections = []
        for project, tasks, extra, recent_count in visible_groups:
            children = [row(a) for a in tasks]
            if extra and not self.expand_recent:
                children.append(Static(
                    f"  … 另 {extra} 筆  [e] 展開", markup=False, classes="more-recent",
                ))
            section = Vertical(*children, classes="project-section")
            running_count = sum(self.state.bucket(a, now) == "running" for a in tasks)
            section.border_title = (
                f"{project_label(project)} · 進行中 {running_count} · 剛完成 {recent_count}"
            )
            sections.append(section)
        if sections:
            board.mount(*sections)
        else:
            board.mount(Static("目前沒有進行中或剛完成的任務", classes="empty-row"))
        if self.selected_key not in self._row_widgets:
            self.selected_key = next(iter(self._row_widgets), None)
        self.call_after_refresh(self.restore_selection, restore_focus)
        history_count = sum(self.state.bucket(agent, now) in ("history", "hidden") for agent in agents)
        self.query_one("#history-summary", Static).update(f"歷史 {history_count} 筆")

    def update_board_timers(self, agents: List[AgentInfo], now: float, width: int) -> None:
        for agent in agents:
            widget = self._row_widgets.get(agent.key)
            if widget is not None:
                widget.agent = agent
                widget.now = now
                widget.refresh(layout=True)

    def restore_selection(self, restore_focus: bool) -> None:
        self.update_selection()
        if restore_focus and self.query_one("#main-tabs", TabbedContent).active == "tab-work":
            row = self._row_widgets.get(self.selected_key)
            if row is not None:
                row.focus(scroll_visible=False)

    def on_task_row_selected(self, event: TaskRow.Selected) -> None:
        if event.key in self._row_widgets:
            self.selected_key = event.key
            self.update_selection()

    def update_selection(self) -> None:
        for widget in self.query(".project-section, #attention-box, #pinned-box"):
            widget.remove_class("selected")
        for key, row in self._row_widgets.items():
            row.selected = key == self.selected_key
            row.set_class(row.selected, "selected")
            row.refresh()
            if row.selected and row.parent is not None:
                panel = row.parent
                if panel.id == "attention-list":
                    panel = panel.parent
                panel.add_class("selected")
        self.update_preview()

    def update_preview(self) -> None:
        followed = next((a for a in self.current_agents if a.key == self.selected_key), None)
        title = self.query_one("#bottom-log-title", Label)
        content = self.query_one("#bottom-log-content", Static)
        if followed is None:
            title.update("尚未選中任務")
            content.update("目前沒有可預覽的任務")
            return
        if followed.key != self._last_preview_key:
            self._preview_read_key = None
            self._last_preview_key = followed.key
        title.update(followed.name)
        lines = []
        for line in followed.log_lines:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                event = None
            summary = summarize_jsonl_step(event) if isinstance(event, dict) else clean_text(line)
            if summary:
                lines.extend(summary.splitlines())
        limit = 24 if self.layout_mode == "split" else 3
        content.update("\n".join(lines[-limit:]) if lines else followed.latest or "尚無即時日誌")
        if (
            self.query_one("#main-tabs", TabbedContent).active == "tab-work"
            and self.state.bucket(followed, self._now) not in ("running", "attention")
            and self._preview_read_key != followed.key
            and not self.state.task(followed.key).get("read")
        ):
            # Reading is tied to a visible preview, not to history highlighting.
            if self.state.update_task(followed.key, read=True, run_start=followed.start_time):
                self._preview_read_key = followed.key
                if followed.key in self._row_widgets:
                    self._row_widgets[followed.key].refresh()
            self.show_state_message(self.state.warning)
        self.call_after_refresh(self.scroll_preview_to_end)

    def show_state_message(self, message: str) -> None:
        self.state_message = message
        if self._ui_ready:
            self.query_one("#state-message", Static).update(message)

    def selected_task(self) -> Optional[AgentInfo]:
        tab = self.query_one("#main-tabs", TabbedContent).active
        key = self.selected_key
        if tab == "tab-history":
            table = self.query_one("#history-table", DataTable)
            if table.cursor_row >= len(self.history_rows):
                return None
            kind, key = self.history_rows[table.cursor_row]
            if kind != "task":
                return None
        elif tab != "tab-work":
            return None
        return next((a for a in self.current_agents if a.key == key), None)

    def operate(self, operation: str, tags: Optional[List[str]] = None,
                task: Optional[AgentInfo] = None) -> None:
        agent = task or self.selected_task()
        if agent is None:
            self.show_state_message("請先選擇一筆任務")
            return
        ok, message = self.state.apply(operation, agent, self.current_agents, self._now, tags)
        self.show_state_message(message)
        if ok:
            if operation == "read":
                self._preview_read_key = agent.key
            self.last_snapshot = None
            self.update_data()
            if operation == "restore":
                self.selected_key = agent.key
                self.action_switch_tab("tab-work")

    def action_hide_task(self) -> None:
        self.operate("hide")

    def action_finish_task(self) -> None:
        self.operate("done")

    def action_dismiss_task(self) -> None:
        self.operate("dismiss")

    def action_dismiss_stale(self) -> None:
        if self.query_one("#main-tabs", TabbedContent).active != "tab-work":
            return
        candidates = [
            agent for agent in self.current_agents
            if self.state.stale_info(agent, self._now) in ("已過期", "已被取代")
        ]
        if not candidates:
            self.show_state_message("目前沒有已過期或已被取代的項目")
            return

        def confirmed(accepted: Optional[bool]) -> None:
            if not accepted:
                self.show_state_message("已取消清除")
                return
            count = self.state.dismiss_stale(self.current_agents, self._now)
            self.show_state_message(f"已清除 {count} 筆已過期／已被取代任務")
            self.last_snapshot = None
            self.update_data()

        self.push_screen(DismissConfirmScreen(len(candidates)), confirmed)

    def action_collapse_project(self) -> None:
        self.operate("collapse")

    def action_toggle_pin(self) -> None:
        self.operate("pin")

    def action_toggle_read(self) -> None:
        self.operate("read")

    def action_restore_task(self) -> None:
        if self.query_one("#main-tabs", TabbedContent).active == "tab-history":
            self.operate("restore")

    def action_edit_tags(self) -> None:
        agent = self.selected_task()
        if agent is None:
            return

        def saved(tags: Optional[List[str]]) -> None:
            if tags is not None:
                self.operate("tags", tags, agent)

        self.push_screen(TagScreen(self.state.task(agent.key).get("tags", [])), saved)

    def update_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        options = (
            tuple(sorted({a.project for a in self.current_agents}, key=project_sort_key)),
            tuple(sorted({tag for a in self.current_agents for tag in self.state.task(a.key).get("tags", [])})),
        )
        if options != self._history_options:
            self._history_options = options
            for field, values, label in (("project", options[0], "專案"), ("tag", options[1], "標籤")):
                selector = self.query_one("#filter-" + field, Select)
                selected = self.history_filters[field]
                selector.set_options([(label + "：全部", "")] + [
                    (project_label(value) if field == "project" else value, value) for value in values
                ])
                self.history_filters[field] = selected if selected in values else ""
                selector.value = self.history_filters[field]
        tasks = history_tasks(self.current_agents, self.state, self._now,
                              self.history_query, **self.history_filters)
        snapshot = (
            self.history_grouped, tuple(sorted(self.history_collapsed)), self.size.width,
            self.history_query, tuple(self.history_filters.items()),
            tuple((a.key, a.completion_time, a.latest, a.status, a.project,
                   self.state.stale_info(a, self._now),
                   json.dumps(self.state.task(a.key), sort_keys=True)) for a in tasks),
        )
        if snapshot == self._history_snapshot:
            return
        self._history_snapshot = snapshot
        old_index = table.cursor_row
        selected = self.history_rows[old_index] if old_index < len(self.history_rows) else None
        table.clear(columns=True)
        widths = [11, 10, 7, 15, max(17, self.size.width - 63), 5]
        for name, width in zip(("時間", "專案", "標籤", "名稱", "結果摘要", "已讀"), widths):
            table.add_column(name, width=width)
        self.history_rows = []

        def add_task(agent: AgentInfo) -> None:
            entry = self.state.task(agent.key)
            timestamp = done_timestamp(agent)
            stamp = datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M") if timestamp else "時間未知"
            status = self.state.stale_info(agent, self._now) or (
                "已完成" if agent.status == "complete" else BOARD_STATUS_LABELS.get(agent.status, agent.status)
            )
            disposition = "隱藏 · " if entry.get("hidden") else ""
            cells = (stamp, project_label(agent.project), ", ".join(entry.get("tags", [])),
                     agent.name, disposition + status + " · " + agent.latest,
                     "已讀" if entry.get("read") else "●未讀")
            table.add_row(*(Text(truncate_display(value, width),
                                style="#737b91" if entry.get("read") else "bold #c4c9d4")
                            for value, width in zip(cells, widths)))
            self.history_rows.append(("task", agent.key))

        if self.history_grouped:
            for project, members in group_agents_by_project(tasks).items():
                closed = project in self.history_collapsed
                cells = ("▸ 專案" if closed else "▾ 專案", project_label(project), "",
                         "%d 筆" % len(members), "Space / Enter 展開" if closed else "Space / Enter 收合", "")
                table.add_row(*(Text(truncate_display(value, width), style="bold #9ece6a")
                                for value, width in zip(cells, widths)))
                self.history_rows.append(("project", project))
                for agent in ([] if closed else members):
                    add_task(agent)
        else:
            for agent in tasks:
                add_task(agent)
        index = self.history_rows.index(selected) if selected in self.history_rows else min(
            old_index, max(0, len(self.history_rows) - 1),
        )
        table.move_cursor(row=index)
        mode = "依專案分組" if self.history_grouped else "表格"
        summary = "歷史 · %s · %d 筆" % (mode, len(tasks))
        if not tasks:
            summary += " · " + ("搜尋／篩選無符合任務" if self.history_query or any(
                self.history_filters.values()) else "目前沒有歷史任務")
        self.query_one("#history-display", Static).update(summary)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id and event.select.id.startswith("filter-") and event.value is not Select.BLANK:
            field = event.select.id[len("filter-"):]
            if self.history_filters[field] != event.value:
                self.history_filters[field] = str(event.value)
                self.update_history()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "history-search":
            self.history_query = event.value
            self.update_history()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "history-search":
            self.query_one("#history-table", DataTable).focus()

    def action_search_history(self) -> None:
        if self.query_one("#main-tabs", TabbedContent).active == "tab-history":
            search = self.query_one("#history-search", Input)
            search.display = True
            search.focus()

    def action_clear_history_search(self) -> None:
        if self.query_one("#main-tabs", TabbedContent).active == "tab-history":
            search = self.query_one("#history-search", Input)
            search.value = ""
            search.display = False
            self.query_one("#history-table", DataTable).focus()

    def action_toggle_history_mode(self) -> None:
        if self.query_one("#main-tabs", TabbedContent).active == "tab-history":
            self.history_grouped = not self.history_grouped
            self.update_history()
            self.query_one("#history-table", DataTable).focus()

    def action_toggle_history_group(self) -> None:
        if self.query_one("#main-tabs", TabbedContent).active != "tab-history":
            return
        table = self.query_one("#history-table", DataTable)
        if table.cursor_row < len(self.history_rows):
            kind, key = self.history_rows[table.cursor_row]
            if kind == "task":
                if not self.history_grouped:
                    return
                # 分組模式下游標停在任務列時，收合該任務所屬專案並把游標移到專案列。
                agent = next((a for a in self.current_agents if a.key == key), None)
                if agent is None:
                    return
                project = agent.project
            else:
                project = key
            if project in self.history_collapsed:
                self.history_collapsed.remove(project)
            else:
                self.history_collapsed.add(project)
            self.update_history()
            if kind == "task" and ("project", project) in self.history_rows:
                table.move_cursor(row=self.history_rows.index(("project", project)))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "history-table":
            self.action_toggle_history_group()

    def scroll_preview_to_end(self) -> None:
        self.query_one("#preview-scroll", VerticalScroll).scroll_end(animate=False)

    def move_task(self, delta: int) -> None:
        tab = self.query_one("#main-tabs", TabbedContent).active
        if tab == "tab-history":
            table = self.query_one("#history-table", DataTable)
            table.move_cursor(row=max(0, min(table.row_count - 1, table.cursor_row + delta)))
            return
        if tab != "tab-work":
            return
        keys = list(self._row_widgets)
        if not keys:
            return
        index = keys.index(self.selected_key) if self.selected_key in keys else 0
        key = keys[max(0, min(len(keys) - 1, index + delta))]
        self.selected_key = key
        self._row_widgets[key].focus()
        self.update_selection()

    def action_next_task(self) -> None:
        self.move_task(1)

    def action_previous_task(self) -> None:
        self.move_task(-1)

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_open_task(self) -> None:
        """SPEC §2.4: Enter opens the deep view, or the question popup."""
        agent = self.selected_task()
        if agent is None:
            if self.query_one("#main-tabs", TabbedContent).active == "tab-work":
                self.show_state_message("請先選擇一筆任務")
            return
        if agent.status == "question" and agent.question:
            self.push_screen(
                QuestionScreen(agent, agent.question),
                lambda result: self.answer_question(agent, result),
            )
            return
        self.open_terminal(agent)

    def open_terminal(self, agent: AgentInfo) -> None:
        if not self.state.task(agent.key).get("read"):
            self.state.update_task(agent.key, read=True, run_start=agent.start_time)
            if agent.key in self._row_widgets:
                self._row_widgets[agent.key].refresh()
            self.show_state_message(self.state.warning)
        self.push_screen(TerminalScreen(agent))

    def question_notice_token(self, agent: AgentInfo) -> str:
        question = agent.question or {}
        return "question::%s::%s::%s" % (
            agent.key, question.get("id") or "", int(question.get("asked_at_ts") or 0),
        )

    def answer_question(self, agent: AgentInfo, result: Optional[Dict[str, Any]]) -> None:
        if not result:
            return
        ok, message = write_answer_file(
            agent.log_path.parent, agent.question or {}, result.get("choice", ""),
            result.get("note", ""), now=time.time(),
        )
        if ok:
            self.state.update_task(agent.key, read=True, run_start=agent.start_time)
            self.show_state_message("已寫入 ANSWER.md：" + result.get("choice", ""))
            self.last_snapshot = None
            self.update_data()
        else:
            self.show_state_message(message)

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Keep dashboard shortcuts inert while the shortcut table is open.

        `q` intentionally stays enabled: the table itself advertises it, and
        leaving the dashboard is not a change to the hidden screen.
        """
        if action in HELP_BLOCKED_ACTIONS and isinstance(self.screen, ModalScreen):
            return False
        if isinstance(self.focused, Input) and action not in ("clear_history_search",):
            return False
        return True

    def action_switch_tab(self, tab_id: str) -> None:
        # Hiding a focused pane otherwise moves focus to its next task, whose
        # TabPane.Focused event would immediately activate the work tab again.
        if isinstance(self.focused, (TaskRow, DataTable, Input, Select)):
            self.set_focus(None)
        self.query_one("#main-tabs", TabbedContent).active = tab_id
        for tab in self.query(NavTab):
            tab.set_class(tab.tab_id == tab_id, "current")
        if tab_id == "tab-work":
            self.call_after_refresh(self.restore_selection, True)
            self.query_one("#key-bar", Static).update(
                "Enter 深入 h 隱藏 d 完成 c 收專案 i 釘 t 標籤 u 已讀 │ e 展開 │ 1–4 ? q"
            )
        elif tab_id == "tab-history":
            self.update_history()
            self.query_one("#history-table", DataTable).focus()
            self.query_one("#key-bar", Static).update(
                "/ 搜尋 Tab 篩選 g 表格/分組 Space 收合 r 撈回 │ i t u │ 1–4 ? q"
            )
        elif tab_id == "tab-tree":
            self.update_tree_display(force=True)
            self.query_one("#key-bar", Static).update(
                "R 重新讀取（每 60 秒自動）│ ↑↓ 捲動 │ 1–5 分頁 │ ? 快捷鍵 q 離開"
            )
        else:
            self.query_one("#key-bar", Static).update("1–4 分頁 │ p 專案 n 現在 f 未來 │ ? 快捷鍵 q 離開")

    def update_tree_display(self, force: bool = False) -> None:
        """Repaint the decisions tree; force rereads the files instead of the cache."""
        if not self._ui_ready:
            return
        try:
            board = self.decision_svc.board(self._now, force=force)
            markup = render_decision_tree(board, self.size.width, now=self._now)
        except Exception as exc:  # 壞資料只降級這一頁
            markup = f"[#f7768e]架構樹無法顯示：{escape(str(exc))}[/]"
        self.query_one("#tree-display", Static).update(markup)

    def action_reload_decisions(self) -> None:
        self.update_tree_display(force=True)
        self.show_state_message(
            "架構樹已重讀：" + time.strftime("%H:%M:%S", time.localtime(self._now))
        )

    def action_show_architecture_now(self) -> None:
        self.set_architecture_view("now")

    def action_show_architecture_future(self) -> None:
        self.set_architecture_view("future")

    def action_cycle_project(self) -> None:
        project_ids = list(ARCHITECTURE_DATA["projects"])
        index = project_ids.index(self.architecture_project_id)
        self.set_architecture_project(project_ids[(index + 1) % len(project_ids)])

    def set_architecture_view(self, view_id: str) -> None:
        if view_id not in ARCHITECTURE_DATA["architectures"]:
            raise ValueError(f"unknown architecture view: {view_id}")
        self.architecture_view_id = view_id
        if self._ui_ready:
            self.query_one("#arch-display", Static).update(
                render_architecture_view(
                    self.current_agents,
                    self.size.width,
                    self.architecture_view_id,
                    self.architecture_project_id,
                )
            )

    def set_architecture_project(self, project_id: str) -> None:
        projects = ARCHITECTURE_DATA["projects"]
        if project_id not in projects:
            raise ValueError(f"unknown architecture project: {project_id}")
        self.architecture_project_id = project_id
        self.architecture_view_id = str(projects[project_id].get("default_view", "now"))
        if self._ui_ready:
            self.query_one("#arch-display", Static).update(
                render_architecture_view(
                    self.current_agents,
                    self.size.width,
                    self.architecture_view_id,
                    self.architecture_project_id,
                )
            )

    def on_mount(self) -> None:
        self._ui_ready = True
        self.update_layout()
        self.action_switch_tab("tab-work")
        self.data_svc.on_ready = self._data_ready
        self.quota_svc.refresh_in_background()
        self.data_svc.refresh_in_background()
        self.update_data()
        self.update_tree_display(force=True)
        self.set_interval(2.0, self.poll_data)
        self.set_interval(float(DECISIONS_REFRESH_SECONDS), self.refresh_decisions)

    def refresh_decisions(self) -> None:
        """60 秒一次的架構樹重讀；新增的決策檔不用改設定就會出現。"""
        if self._ui_ready:
            self.update_tree_display(force=True)

    def poll_data(self) -> None:
        self.quota_svc.refresh_in_background()
        self.data_svc.refresh_in_background()
        self.update_data()

    def _data_ready(self) -> None:
        try:
            self.call_from_thread(self.update_data)
        except (RuntimeError, CancelledError):
            # The reader can finish after the app has closed.
            pass

    def make_snapshot(
        self,
        agents: List[AgentInfo],
        quota_summary: str,
        quota_raw: list,
        quota_stale: bool = False,
        quota_error: str = "",
        errors: Tuple[str, ...] = (),
        now: Optional[float] = None,
    ) -> tuple:
        """Snapshot displayed data without elapsed counters."""
        current = time.time() if now is None else now
        agent_snaps = tuple(
            (
                a.key,
                a.status,
                a.harness,
                a.model,
                a.latest,
                a.log_mtime,
                tuple(a.log_lines[-8:]),
                a.error,
                a.pid,
                a.completion_time,
                a.is_done_file,
                a.stage,
                a.project,
                a.start_time,
                self.state.bucket(a, current),
            )
            for a in agents
        )
        quota_data = json.dumps(quota_raw, ensure_ascii=False, sort_keys=True, default=str)
        return (agent_snaps, quota_summary, quota_data, quota_stale, quota_error, errors,
                self.state.revision)

    def update_quota_charts(self, raw: list, stale: bool, now: float) -> None:
        """Move quota clocks without redrawing unchanged task data."""
        charts = (
            render_home_quota(raw, max(1, self.size.width - 2), stale, now),
            render_full_quota_view(raw, self.size.width, stale, now),
        )
        if charts != getattr(self, "_last_quota_charts", None):
            self.query_one("#compact-quota-line", Static).update(charts[0])
            self.query_one("#quota-display", Static).update(charts[1])
            self._last_quota_charts = charts

    def update_data(self, now: Optional[float] = None) -> bool:
        """
        Update static content only when its data changes; update elapsed numbers separately.
        Returns True when non-timer content changes.
        """
        if now is None:
            now = time.time()
        self._now = now
        if self._ui_ready:
            self.query_one("#clock", Label).update(
                time.strftime("%H:%M:%S", time.localtime(now))
            )

        registry_complete = False
        try:
            if self._ui_ready:
                registry_complete = self.data_svc.has_snapshot
                agents, errors = self.data_svc.snapshot()
                for agent in agents:
                    if agent.completion_time is None:
                        agent.elapsed = format_elapsed(agent.start_time, now)
                        if agent.elapsed_estimated:
                            agent.elapsed = "~" + agent.elapsed
            else:
                agents, errors = load_all_agents(self.runs_path, self.done_path, self.log_parser, now)
                registry_complete = True
        except Exception as e:
            agents, errors = [], [f"讀取工作資料失敗：{e}"]
            registry_complete = False
        quota_summary, quota_raw, quota_stale, quota_error = self.quota_svc.snapshot()
        if quota_stale and not quota_raw:
            quota_summary = "額度：讀取失敗"
        error_snapshot = tuple(errors) + ((quota_error,) if quota_stale and quota_error else ())
        if self.ui_error:
            error_snapshot += (self.ui_error,)
        self.state.reconcile(agents, now, registry_complete=registry_complete and not errors)
        if self.state.warning and self.state.warning != self.state_message:
            self.show_state_message(self.state.warning)

        # Handle completion notifications
        first_load = self.is_first_load
        if not first_load:
            for a in agents:
                prev = self.previous_status.get(a.key)
                notice_key = f"{a.key}::{a.start_time}::{a.status}"
                if (
                    prev is not None and prev != a.status
                    and a.status in {"complete", "partial", "failed", "blocked", "waiting"}
                    and notice_key not in self.notified_keys
                ):
                    notify_completion(a)
                    self.notified_keys.add(notice_key)
        else:
            for a in agents:
                if a.status == "complete":
                    self.notified_keys.add(a.key)
            self.is_first_load = False

        overdue_questions: Dict[str, AgentInfo] = {}
        for a in agents:
            if a.status != "question" or not a.question or self.state.bucket(a, now) != "attention":
                continue
            if question_wait_seconds(a.question, now) < QUESTION_OVERDUE_SECONDS:
                continue
            overdue_questions[self.question_notice_token(a)] = a
        if first_load:
            self.notified_questions.update(overdue_questions)
        else:
            for token, a in overdue_questions.items():
                if token not in self.notified_questions:
                    notify_question_overdue(a)
                    self.notified_questions.add(token)

        self.previous_status = {a.key: a.status for a in agents}
        active = [a for a in agents if self.state.bucket(a, now) in ("attention", "running")]
        self.current_agents = agents
        self.current_active_agents = active
        timer_snapshot = tuple((a.key, a.elapsed) for a in active)
        timer_changed = timer_snapshot != self.last_timer_snapshot
        if timer_changed:
            self.last_timer_snapshot = timer_snapshot
            self.timer_update_count += 1

        current_snapshot = self.make_snapshot(
            agents,
            quota_summary,
            quota_raw,
            quota_stale,
            quota_error,
            error_snapshot,
            now,
        )
        data_changed = current_snapshot != self.last_snapshot
        if not data_changed:
            if self._ui_ready:
                self.update_quota_charts(quota_raw, quota_stale, now)
            if timer_changed and self._ui_ready:
                try:
                    self.update_board_timers(agents, now, self.size.width)
                except Exception as e:
                    self.ui_error = f"計時畫面更新失敗：{e}"
            return False

        self.last_snapshot = current_snapshot
        self.redraw_count += 1

        counts = {key: 0 for key in ("attention", "running", "recent", "history", "hidden")}
        for agent in agents:
            counts[self.state.bucket(agent, now)] += 1
        stats_str = (
            f"需處理 {counts['attention']} │ 進行中 {counts['running']} │ "
            f"剛完成 {counts['recent']} │ 歷史 {counts['history']} │ "
            f"隱藏 {counts['hidden']} │ 總計 {len(agents)}"
        )
        width = self.size.width if self._ui_ready else 80

        if not self._ui_ready:
            return True

        try:
            self.query_one("#top-stats", Label).update(stats_str)
            self.update_quota_charts(quota_raw, quota_stale, now)
            data_errors = self.query_one("#data-errors", Static)
            errors_markup = render_data_errors(list(error_snapshot), agents, width)
            data_errors.update(errors_markup)
            data_errors.display = bool(errors_markup)
            self.update_board(agents, now, width)
            self.update_preview()
            self.update_history()

            arch_markup = render_architecture_view(
                agents,
                width,
                self.architecture_view_id,
                self.architecture_project_id,
            )
            self.query_one("#arch-display", Static).update(arch_markup)
            self.update_tree_display()

        except Exception as e:
            self.ui_error = f"畫面更新失敗：{e}"
            try:
                self.query_one("#data-errors", Static).update(
                    f"[#ff9e3b]{escape(self.ui_error)}[/#ff9e3b]"
                )
            except Exception:
                self.ui_error = f"畫面更新失敗，錯誤提示元件也無法更新：{e}"

        return True


def main():
    parser = argparse.ArgumentParser(description="agent-watch v8")
    parser.add_argument("--once", action="store_true", help="印出單次純文字摘要後 exit 0")
    parser.add_argument("--runs", default=str(DEFAULT_RUNS_TSV), help="agent-runs.tsv 路徑")
    parser.add_argument("--done", default=str(DEFAULT_DONE_TSV), help="agent-runs-done.tsv 路徑")
    args = parser.parse_args()

    runs_p = Path(args.runs).expanduser()
    done_p = Path(args.done).expanduser()

    if args.once:
        return run_once_summary(runs_p, done_p)

    app = AgentWatchApp(runs_path=runs_p, done_path=done_p)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
