#!/usr/bin/env python3
"""agent-watch v8: Textual dashboard for reliable run status and quota monitoring."""

import argparse
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from rich.markup import escape
from rich.text import Text

# Textual imports (conditional for --once flag speed)
try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Collapsible, Footer, Label, Static, TabbedContent, TabPane
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
    projects = data.get("projects")
    architectures = data.get("architectures")
    if not isinstance(projects, dict) or not projects:
        raise ValueError("arch.json must define at least one project")
    if not isinstance(architectures, dict) or "now" not in architectures:
        raise ValueError("arch.json must define architectures.now")

    for project_id, project in projects.items():
        if not isinstance(project, dict) or not project.get("label"):
            raise ValueError(f"project {project_id!r} has no label")
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


ARCHITECTURE_DATA = load_architecture_data()
PROJECT_OTHER = "other"
RECENT_DONE_SECONDS = 2 * 60 * 60
MAGPIE_TIMEOUT_SECONDS = 10
QUOTA_REFRESH_SECONDS = 60
QUOTA_PROVIDER_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode-go": "OpenCode Go",
    "openrouter": "OpenRouter",
}


def keyword_matches(keyword: str, text: str) -> bool:
    token = keyword.casefold()
    if re.fullmatch(r"[a-z0-9]{1,3}", token):
        return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None
    return token in text


def infer_project(
    name: str,
    log_path: Optional[Path] = None,
    last_path: Optional[Path] = None,
) -> str:
    """Map a task to the configured project table; unknown tasks use other."""
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
        if any(
            isinstance(keyword, str) and keyword_matches(keyword, path_text)
            for keyword in path_keywords
        ):
            return project_id
    return PROJECT_OTHER


def project_label(project_id: str) -> str:
    config = ARCHITECTURE_DATA.get("projects", {}).get(project_id)
    if isinstance(config, dict) and config.get("label"):
        return str(config["label"])
    return "其他" if project_id == PROJECT_OTHER else project_id


def project_sort_key(project_id: str) -> Tuple[int, str]:
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
    return clean_text(str(data))[:60]


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


class LogParser:
    """Parse changed log bytes only; log text never determines the final status."""

    MAX_INITIAL_READ = 2 * 1024 * 1024
    START_SCAN_BYTES = 64 * 1024

    def __init__(self):
        self.cache: Dict[str, dict] = {}

    @staticmethod
    def _summarize(raw_line: bytes, mtime: float) -> Tuple[Optional[float], Optional[str], Optional[str]]:
        line = clean_text(raw_line.decode("utf-8", errors="replace"))
        if not line:
            return None, None, None

        timestamp = parse_timestamp(line)
        model = None
        if is_noise(line):
            return timestamp, model, None

        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                summary = summarize_jsonl_step(data) if isinstance(data, dict) else line
            except (json.JSONDecodeError, ValueError):
                summary = line
        else:
            summary = line

        match_clock = CLOCK_RE.search(line)
        clock = match_clock.group(1) if match_clock else time.strftime("%H:%M:%S", time.localtime(mtime))
        return timestamp, model, f"{clock}  {summary}"

    def parse(self, path: Path) -> Tuple[Optional[float], str, List[str], float, str]:
        if str(path) == "/dev/null":
            return None, UNKNOWN_IDENTITY, [], 0.0, ""

        try:
            st = path.stat()
            if not stat.S_ISREG(st.st_mode):
                return None, UNKNOWN_IDENTITY, [], 0.0, "不是一般檔案"
        except OSError as e:
            return None, UNKNOWN_IDENTITY, [], 0.0, e.strerror or "無法讀取"

        cache_key = str(path.resolve())
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
                            if isinstance(record, dict) and record.get("timestamp"):
                                start_time = parse_timestamp(str(record["timestamp"]))
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


def read_progress_stage(directory: Path) -> str:
    """Return the last non-empty line in a sibling PROGRESS.md, if present."""
    try:
        content = (directory / "PROGRESS.md").read_text(encoding="utf-8", errors="replace")
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
                return True, float(commit_time)
            except ValueError:
                return True, None

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
        return "complete" if upper in ("DONE", "COMPLETE") else upper.lower()

    if normalized_lines:
        first_status = re.match(
            r"^(?:(?:status|狀態|結論|判定|result|結果)\s*[:：]\s*)?"
            r"(DONE|COMPLETE|FAILED|BLOCKED|PARTIAL)\b",
            normalized_lines[0],
            re.IGNORECASE,
        )
        if first_status:
            return status_value(first_status.group(1))

    for line in normalized_lines:
        status_field = re.search(
            r"(?:status|狀態|結論|判定|result|結果)\s*[:：]\s*"
            r"(DONE|COMPLETE|FAILED|BLOCKED|PARTIAL)\b",
            line,
            re.IGNORECASE,
        )
        if status_field:
            return status_value(status_field.group(1))

    for line in normalized_lines:
        status_line = re.match(
            r"^(DONE|COMPLETE|FAILED|BLOCKED|PARTIAL)\b",
            line,
            re.IGNORECASE,
        )
        if status_line:
            return status_value(status_line.group(1))

    for line in normalized_lines:
        done_marker = re.search(r"\b(DONE|COMPLETE)\b", line)
        if done_marker:
            prefix = line[: done_marker.start()]
            english_negation = re.search(
                r"\b(?:not|never|no|isn't|wasn't)\b[^.!?]*$",
                prefix,
                re.IGNORECASE,
            )
            chinese_negation = re.search(r"(?:不是|並非|未|沒有)\s*$", prefix)
            if not english_negation and not chinese_negation:
                return "complete"

    for line in lines[:20]:
        if re.search(r"(?<!未)(?<!沒)(?<!有)(?<!法)(?<!能)(?:已完成|完成)", line):
            return "complete"
    return None


def is_pid_alive(pid: int) -> Optional[bool]:
    """None means inspection failed, not that the process has ended."""
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
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
) -> str:
    if pid is not None:
        if process_alive is None:
            process_alive = is_pid_alive(pid)
        if process_alive:
            return "running"
        if process_alive is None:
            return "unconfirmed"
    if is_done_file:
        return (last_status or "reported") if last_valid else "no_report"
    run_start = start_time if start_time is not None else log_mtime
    last_is_fresh = last_valid and last_mtime > run_start
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
    stage = read_progress_stage(result_path.parent)

    data_err = row_error or log_err or last_err
    if not is_null_log and not log_path.exists():
        data_err = f"log 檔案不存在：{log_path}"
    last_status = last_status_from_content(last_content) if last_valid else None
    pid_alive = is_pid_alive(pid) if pid is not None else None
    resolved_harness, resolved_model = resolve_run_identity(
        log_path,
        pid,
        harness,
        model,
        pid_alive,
    )

    run_start_candidates = [value for value in (start_time, registration_time) if value is not None]
    run_start = max(run_start_candidates) if run_start_candidates else log_mtime
    elapsed_start = start_time if start_time is not None else registration_time
    last_is_fresh = last_valid and last_mtime > run_start
    run_started = run_start if run_start > 0 else None
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
    )

    is_terminal = status in {"complete", "failed", "blocked", "partial", "reported"}
    completion_time = None
    if is_done_file and status != "running":
        completion_time = last_mtime if last_valid else log_mtime or None
    elif is_terminal:
        if last_is_fresh:
            completion_time = last_mtime
        elif status == "complete" and has_new_commit:
            completion_time = commit_time or (last_mtime if last_valid else 0) or log_mtime or now
    if completion_time is not None and elapsed_start is not None and elapsed_start > completion_time:
        elapsed_start = None
    elapsed = format_elapsed(elapsed_start, completion_time if completion_time is not None else now)
    key = f"{name}::{log_path}::{result_path}"

    if is_terminal and last_content:
        clean_last_lines = [
            clean_text(l) for l in last_content.splitlines() if clean_text(l) and not is_noise(clean_text(l))
        ]
        conclusion_lines = [
            line for line in clean_last_lines
            if not line.startswith("#") and not re.fullmatch(
                r"(?:(?:STATUS|狀態|判定)\s*[:：]\s*)?"
                r"(?:DONE|COMPLETE|COMPLETED|PARTIAL|FAILED|BLOCKED|REPORTED|完成|部分完成|失敗|受阻)",
                line.replace("**", "").strip(), re.IGNORECASE,
            )
        ]
        latest = (conclusion_lines or ["完成" if status == "complete" else "任務已回報"])[0]
    elif log_lines:
        latest = log_lines[-1].partition("  ")[2] or log_lines[-1]
    else:
        latest = data_err or "尚無訊息"

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
        project=infer_project(name, log_path, result_path),
    )


def load_tsv_agents(
    tsv_path: Path,
    is_done_file: bool,
    log_parser: LogParser,
    now: float,
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
        if len(parts) not in (3, 4, 5, 6):
            errors.append(f"{tsv_path.name} 第 {line_num} 行格式錯誤（需 3 至 6 欄）")
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
            )
        )

    return agents, errors


def load_all_agents(
    runs_path: Path = DEFAULT_RUNS_TSV,
    done_path: Path = DEFAULT_DONE_TSV,
    log_parser: Optional[LogParser] = None,
    now: Optional[float] = None,
) -> Tuple[List[AgentInfo], List[str]]:
    if log_parser is None:
        log_parser = LogParser()
    if now is None:
        now = time.time()

    running_agents, err1 = load_tsv_agents(runs_path, False, log_parser, now)
    done_agents, err2 = load_tsv_agents(done_path, True, log_parser, now)

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
    return all_agents, err1 + err2


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
        if age is not None and 0 <= age <= RECENT_DONE_SECONDS:
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
        if ended is not None and 0 <= current - ended <= RECENT_DONE_SECONDS:
            return "recent"
        return "history"
    return "attention"


def count_buckets(agents: List[AgentInfo], now: Optional[float] = None) -> Dict[str, int]:
    current = time.time() if now is None else now
    counts = dict(attention=0, running=0, recent=0, history=0, total=len(agents))
    for agent in agents:
        counts[task_bucket(agent, current)] += 1
    return counts


def format_bucket_counts(agents: List[AgentInfo], now: float) -> str:
    counts = count_buckets(agents, now)
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

def get_quota_color_and_tag(pct: float) -> Tuple[str, str]:
    """
    Returns (color_name, hex_code) based on quota usage:
    - Over 85%: Red (#f7768e)
    - Over 80%: Yellow (#e0af68)
    - <= 80%: Green (#9ece6a)
    """
    if pct > 85:
        return "red", "#f7768e"
    elif pct > 80:
        return "yellow", "#e0af68"
    else:
        return "green", "#9ece6a"


def get_quota_color(pct: float) -> str:
    """Return color name ('red', 'yellow', 'green') for percentage."""
    return get_quota_color_and_tag(pct)[0]


def render_quota_bar(pct: float, width: int = 10) -> str:
    """Render horizontal bar chart for quota percentage with threshold colors."""
    clamped = max(0.0, min(100.0, pct))
    filled_len = int(round(clamped / 100.0 * width))
    empty_len = width - filled_len
    _, hex_color = get_quota_color_and_tag(clamped)
    filled_part = f"[{hex_color}]{'█' * filled_len}[/]" if filled_len > 0 else ""
    empty_part = f"[#414868]{'░' * empty_len}[/]" if empty_len > 0 else ""
    return f"\\[{filled_part}{empty_part}\\]"


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


def format_reset_time(value: Any) -> str:
    """Format an ISO or epoch reset time in Taipei time; never infer a missing value."""
    epoch = quota_epoch(value)
    if epoch is None:
        return "未提供"
    reset = datetime.fromtimestamp(epoch, ZoneInfo("Asia/Taipei"))
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    if reset.date() == now.date():
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
        return "month"
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
    has_time = any(quota_epoch(item.get(key)) is not None for key in ("asOf", "fetchedAt"))
    return bool(
        (isinstance(balance, str) and balance.strip())
        or (has_time and valid_quota_windows(item))
    )


def quota_data_time(item: Dict[str, Any]) -> str:
    if quota_epoch(item.get("asOf")) is not None:
        return format_quota_source_time(item["asOf"])
    fetched = quota_epoch(item.get("fetchedAt"))
    if fetched is not None:
        clock = datetime.fromtimestamp(fetched, ZoneInfo("Asia/Taipei")).strftime("%H:%M:%S")
        return f"magpie 未提供（本次讀取 {clock}）"
    return "時間未確認"


def parse_quota_json(data: list) -> str:
    """Keep the home summary short; source and reset times live in details."""
    if not isinstance(data, list):
        return "額度：無資料"
    parts = []
    seen = set()
    for item in data:
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
        abbreviations = {"5 小時": "5h", "7 天": "7d", "month": "月"}
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


def render_full_quota_view(quota_data: list, width: int = 80, stale: bool = False) -> str:
    """Render each quota window on its own short line for narrow terminals."""
    lines = [
        "[bold #7aa2f7]● 服務額度用量[/bold #7aa2f7]",
        "[#565f89]每 60 秒背景更新；使用率超過 80% 黃色、超過 85% 紅色[/#565f89]",
    ]
    if stale:
        lines.append("[bold #ff9e3b]部分來源讀取失敗；只顯示本次取得且有時間的有效數字[/bold #ff9e3b]")
    if not quota_data:
        lines.append("[#ff9e3b]無資料[/#ff9e3b]")
        return "\n".join(lines)

    rendered = False
    for item in quota_data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("provider") or "未知服務")
        plan = item.get("plan")
        title = name if not plan else f"{name} ({plan})"
        lines.append(f"[bold #c0caf5]{escape(title)}[/bold #c0caf5]")
        source_time = quota_data_time(item)
        lines.append(f"  [#565f89]資料時間：{escape(source_time)}[/#565f89]")
        windows = valid_quota_windows(item)
        if not quota_item_has_data(item):
            lines.append(f"  [#ff9e3b]{escape(str(item.get('reason') or '無資料'))}[/#ff9e3b]")
            rendered = True
            continue
        if isinstance(item.get("balance"), str) and item["balance"].strip():
            lines.append(f"  帳戶餘額 {escape(item['balance'])}")
            rendered = True
        for label, used, window in windows:
            reset = format_reset_time(window.get("resetsAt") or window.get("resets_at"))
            pct = int(round(used))
            bar = render_quota_bar(used, 8)
            lines.append(
                f"  {label} {bar} {pct:>3}%  重置 {escape(reset)}"
            )
            rendered = True

    if not rendered:
        lines.append("[#ff9e3b]無資料[/#ff9e3b]")

    return "\n".join(lines)


def render_plain_quota_details(quota_data: list) -> List[str]:
    """Render sanitized plain-text quota details for --once."""
    lines = []
    for item in quota_data if isinstance(quota_data, list) else []:
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
    if agent.start_time is None or not re.fullmatch(r"\d+:\d{2}:\d{2}", agent.elapsed):
        return agent.elapsed
    hours, minutes, seconds = (int(part) for part in agent.elapsed.split(":"))
    total = hours * 60 + minutes
    return f"{total}m" if total else f"{seconds}s"


def render_board_active(agents: List[AgentInfo], width: int) -> str:
    lines = []
    for agent in agents:
        badge = STATUS_BADGE_MARKUP.get(agent.status, "")
        available = max(16, width - 4)
        badge_width = display_width(Text.from_markup(badge).plain) + 1 if badge else 0
        identity = f"{agent.harness} · {agent.model} · 已跑 {compact_elapsed(agent)}"
        identity = truncate_display(identity, max(8, available - badge_width - 12))
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
    """Trigger macOS notification and sound when an agent completes."""
    name_escaped = agent.name.replace('"', '\\"')
    script = f'display notification "{name_escaped} 已完成" with title "Agent Watch"'
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
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
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
    background: #0f1117;
    color: #c0caf5;
}

#top-bar {
    height: 3;
    background: #1a1b26;
    border-bottom: heavy #3b4261;
    padding: 0 1;
}

#top-title {
    color: #7aa2f7;
    text-style: bold;
    width: 20;
}

#top-stats {
    color: #a9b1d6;
    width: 1fr;
}

TabbedContent {
    height: 1fr;
}

TabPane {
    padding: 1;
}

#work-page {
    height: 1fr;
}

#work-scroll {
    height: 1fr;
    min-height: 3;
}

.flow-title {
    color: #bb9af7;
    text-style: bold;
    height: 1;
}

#data-errors {
    height: auto;
    color: #ff9e3b;
}

#recent-done-display {
    height: auto;
    margin-bottom: 1;
}

#compact-quota-line {
    background: #16161e;
    color: #7dcfff;
    padding: 0 1;
    height: auto;
}

#work-board, .project-section, .active-task-list, .recent-task-list, #history-summary {
    height: auto;
}

#active-flow-display {
    height: auto;
    margin-bottom: 1;
}

#done-collapsible {
    background: #1a1b26;
    border: solid #3b4261;
    margin-top: 1;
    margin-bottom: 1;
}

#done-tasks-display {
    height: auto;
    padding: 0 1;
}

#work-log-box {
    height: 9;
    min-height: 9;
    max-height: 9;
    background: #16161e;
    border-top: double #7aa2f7;
    padding: 0 1;
}

#bottom-log-title {
    color: #7dcfff;
    text-style: bold;
    height: 1;
    content-align: left middle;
}

#bottom-log-content {
    color: #a9b1d6;
    height: 6;
    overflow: hidden;
}

#arch-display {
    height: auto;
}

#quota-display {
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


class AgentWatchApp(App):
    """Textual terminal dashboard for monitoring agents with tabs and flicker-free updates."""

    CSS = APP_CSS
    BINDINGS = [
        ("q", "quit", "離開 (Quit)"),
        ("1", "switch_tab('tab-work')", "工作 (1)"),
        ("2", "switch_tab('tab-arch')", "架構 (2)"),
        ("3", "switch_tab('tab-quota')", "額度 (3)"),
        ("e", "toggle_recent", "展開剛完成 (E)"),
        ("p", "cycle_project", "切換專案 (P)"),
        ("n", "show_architecture_now", "現在 (N)"),
        ("f", "show_architecture_future", "未來 (F)"),
    ]

    def __init__(
        self,
        runs_path: Path = DEFAULT_RUNS_TSV,
        done_path: Path = DEFAULT_DONE_TSV,
    ):
        super().__init__()
        self.runs_path = runs_path
        self.done_path = done_path
        self.log_parser = LogParser()
        self.quota_svc = QuotaService()
        self.notified_keys: Set[str] = set()
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
        self.architecture_project_id = next(iter(ARCHITECTURE_DATA["projects"]))
        self.current_active_agents: List[AgentInfo] = []
        self.expand_recent = False
        self._board_active_widgets: Dict[str, Static] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Label("⚡ AGENT WATCH v8", id="top-title")
            yield Label("載入中...", id="top-stats")
        yield Static("額度：載入中...", id="compact-quota-line")

        with TabbedContent(initial="tab-work", id="main-tabs"):
            with TabPane("工作 (1)", id="tab-work"):
                with Vertical(id="work-page"):
                    with VerticalScroll(id="work-scroll"):
                        yield Label("● 工作看板", classes="flow-title")
                        yield Static(id="data-errors")
                        yield Vertical(id="work-board")
                        yield Static(id="history-summary")
                    with Vertical(id="work-log-box"):
                        yield Label("最新 LOG：無", id="bottom-log-title")
                        yield Static("尚無即時日誌", id="bottom-log-content")

            with TabPane("架構 (2)", id="tab-arch"):
                with VerticalScroll(id="arch-scroll"):
                    yield Static(id="arch-display")

            with TabPane("額度 (3)", id="tab-quota"):
                with VerticalScroll(id="quota-scroll"):
                    yield Static(id="quota-display")

        yield Footer()

    def action_toggle_recent(self) -> None:
        self.expand_recent = not self.expand_recent
        self.last_snapshot = None
        self.update_data()

    def on_resize(self) -> None:
        if self._ui_ready:
            self.last_snapshot = None
            self.update_data()

    def update_board(self, agents: List[AgentInfo], now: float, width: int) -> None:
        board = self.query_one("#work-board", Vertical)
        board.remove_children()
        self._board_active_widgets = {}
        sections = []
        for project, tasks in work_board_groups(agents, now).items():
            active = [a for a in tasks if task_bucket(a, now) != "recent"]
            recent = [a for a in tasks if task_bucket(a, now) == "recent"]
            counts = count_buckets(tasks, now)
            title = (
                f"▌ {project_label(project)}  需處理 {counts['attention']} · "
                f"進行中 {counts['running']} · 剛完成 {counts['recent']}"
            )
            active_widget = Static(render_board_active(active, width), classes="active-task-list")
            self._board_active_widgets[project] = active_widget
            sections.append(Vertical(
                Static(escape(title), classes="flow-title"),
                active_widget,
                Static(render_board_recent(recent, width, self.expand_recent), classes="recent-task-list"),
                classes="project-section",
            ))
        if sections:
            board.mount(*sections)
        self.query_one("#history-summary", Static).update(escape(render_history_summary(agents, now)))

    def update_board_timers(self, agents: List[AgentInfo], now: float, width: int) -> None:
        for project, tasks in work_board_groups(agents, now).items():
            widget = self._board_active_widgets.get(project)
            if widget is not None:
                active = [a for a in tasks if task_bucket(a, now) != "recent"]
                if active:
                    widget.update(render_board_active(active, width))

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch active tab on keys 1/2/3."""
        try:
            tc = self.query_one("#main-tabs", TabbedContent)
            tc.active = tab_id
        except Exception:
            pass

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
        self.quota_svc.refresh_in_background()
        self.update_data()
        self.set_interval(2.0, self.poll_data)

    def poll_data(self) -> None:
        self.quota_svc.refresh_in_background()
        self.update_data()

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
                task_bucket(a, now),
            )
            for a in agents
        )
        quota_data = json.dumps(quota_raw, ensure_ascii=False, sort_keys=True, default=str)
        return (agent_snaps, quota_summary, quota_data, quota_stale, quota_error, errors)

    def update_data(self, now: Optional[float] = None) -> bool:
        """
        Update static content only when its data changes; update elapsed numbers separately.
        Returns True when non-timer content changes.
        """
        if now is None:
            now = time.time()

        try:
            agents, errors = load_all_agents(self.runs_path, self.done_path, self.log_parser, now)
        except Exception as e:
            agents, errors = [], [f"讀取工作資料失敗：{e}"]
        quota_summary, quota_raw, quota_stale, quota_error = self.quota_svc.snapshot()
        if quota_stale and not quota_raw:
            quota_summary = "額度：讀取失敗"
        error_snapshot = tuple(errors) + ((quota_error,) if quota_stale and quota_error else ())
        if self.ui_error:
            error_snapshot += (self.ui_error,)

        # Handle completion notifications
        if not self.is_first_load:
            for a in agents:
                prev = self.previous_status.get(a.key)
                if prev in ("running", "waiting") and a.status == "complete" and a.key not in self.notified_keys:
                    notify_completion(a)
                    self.notified_keys.add(a.key)
        else:
            for a in agents:
                if a.status == "complete":
                    self.notified_keys.add(a.key)
            self.is_first_load = False

        self.previous_status = {a.key: a.status for a in agents}
        active = [a for a in agents if task_bucket(a, now) in ("attention", "running")]
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
            if timer_changed and self._ui_ready:
                try:
                    self.update_board_timers(agents, now, self.size.width)
                except Exception as e:
                    self.ui_error = f"計時畫面更新失敗：{e}"
            return False

        self.last_snapshot = current_snapshot
        self.redraw_count += 1

        stats_str = format_bucket_counts(agents, now)
        width = self.size.width if self._ui_ready else 80

        if not self._ui_ready:
            return True

        try:
            self.query_one("#top-stats", Label).update(stats_str)
            self.query_one("#compact-quota-line", Static).update(
                escape(render_compact_quota(quota_summary, max(1, width - 2)))
            )
            self.query_one("#data-errors", Static).update(
                render_data_errors(list(error_snapshot), agents, width)
            )
            self.update_board(agents, now, width)

            candidates = [a for a in agents if a.log_mtime > 0 and a.log_lines]
            followed = max(candidates, key=lambda a: a.log_mtime) if candidates else None
            log_title = self.query_one("#bottom-log-title", Label)
            log_content = self.query_one("#bottom-log-content", Static)
            log_width = max(20, width - 7)
            if followed:
                log_title.update(f"最新 LOG：{escape(truncate_display(followed.name, max(12, width - 20)))}")
                recent_lines = [
                    escape(truncate_display(line, log_width))
                    for line in followed.log_lines[-6:]
                ]
                log_content.update("\n".join(recent_lines) if recent_lines else "尚無即時日誌")
            else:
                log_title.update("最新 LOG：無可讀日誌")
                log_content.update("目前沒有可讀取的日誌內容")

            arch_markup = render_architecture_view(
                agents,
                width,
                self.architecture_view_id,
                self.architecture_project_id,
            )
            self.query_one("#arch-display", Static).update(arch_markup)

            quota_markup = render_full_quota_view(quota_raw, width, quota_stale)
            self.query_one("#quota-display", Static).update(quota_markup)

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
