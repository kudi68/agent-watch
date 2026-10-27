"""現況資料層：status/*.md 表頭＋decisions 目前節點 → 每線一筆。只讀，不寫檔。"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import watch

FRONTMATTER_OPEN, FRONTMATTER_CLOSE = "<!-- dashboard:v1", "-->"
FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]{1,3}\d{1,4}(?:-\d+)?|tier-(?:flash|mid|sol)|hl-[a-z0-9-]+"
    r"|aw-[a-z0-9-]+|p\d+-\d+|#[0-9]+)(?![A-Za-z0-9])")
BLOCKED, WAITING, ACTIVE, DONE, IDLE = "blocked", "waiting", "active", "done", "idle"
STATE_ORDER = {BLOCKED: 0, WAITING: 1, ACTIVE: 2, DONE: 3, IDLE: 4}
STATE_LABEL = {BLOCKED: "被擋", WAITING: "等你", ACTIVE: "進行中", DONE: "完成", IDLE: "空閒"}
DECISION_STATE = {"running": ACTIVE, "waiting": WAITING, "done": DONE}
BLOCKED_WORDS = ("被擋", "卡住", "blocked", "stuck")
WAITING_WORDS = ("等你", "等待", "waiting", "待回覆")
ACTIVE_WORDS = ("進行中", "開工", "running", "active")
DONE_WORDS = ("完成", "告一段落", "done", "closed")
FALLBACK_DOING = {BLOCKED: "卡住了，還沒寫原因。", WAITING: "停在等你回覆。",
                  ACTIVE: "正在進行中，還沒寫目前在做什麼。", DONE: "已告一段落。",
                  IDLE: "目前沒有進行中的事。"}


def parse_frontmatter(text: str) -> Dict[str, str]:
    match = re.search(r"^<!-- dashboard:v1\n(.*?)\n-->", text, re.S | re.M)
    fields: Dict[str, str] = {}
    for row in match.group(1).splitlines() if match else []:
        field = FIELD_RE.match(row.strip())
        if field:
            fields[field.group(1)] = field.group(2).strip().strip('"').replace('\\"', '"')
    return fields


def one_sentence(text: str, limit: int = 90) -> str:
    flat = re.split(r"(?<=[。！？])", " ".join(str(text or "").split()), 1)[0]
    return flat[:limit].rstrip() + "…" if len(flat) > limit else flat


def human_title(name: str, project: str = "", fallback: str = "") -> str:
    """Never leave an internal code (K1, P1-13) as the title."""
    for candidate in (name, project, fallback):
        cleaned = " ".join(CODE_RE.sub(" ", candidate or "").split()).strip(" ·-—:：,，")
        cleaned = re.sub(r"\s*決策節點$", "", cleaned)
        if cleaned:
            return cleaned
    return "未命名線"


def classify_state(raw_status: str, waiting_on: str = "", decision_status: str = "") -> str:
    text = (raw_status or "").casefold()
    if any(word in text for word in BLOCKED_WORDS):
        return BLOCKED
    if (waiting_on or "").strip() or any(word in text for word in WAITING_WORDS):
        return WAITING
    if not text.strip():
        return DECISION_STATE.get((decision_status or "").casefold(), IDLE)
    if any(word in text for word in ACTIVE_WORDS):
        return ACTIVE
    return DONE if any(word in text for word in DONE_WORDS) else IDLE


@dataclass
class LineSnapshot:
    name: str
    title: str
    state: str
    doing: str
    blocked_on: str
    next_step: str
    last_updated: str = ""
    decision_id: str = ""
    report: str = ""

    @property
    def state_label(self) -> str:
        return STATE_LABEL.get(self.state, STATE_LABEL[IDLE])


def _record(name: str, fields: Dict[str, str], line) -> LineSnapshot:
    waiting = (fields.get("waiting_on") or "").strip()
    node = line.current if line is not None else None
    state = classify_state(fields.get("status", ""), waiting, node.status if node else "")
    return LineSnapshot(
        name=name,
        title=human_title(fields.get("name", ""), fields.get("project", ""), name),
        state=state,
        doing=(one_sentence(node.summary) if node else "") or FALLBACK_DOING[state],
        blocked_on=one_sentence(waiting) or ("不知道卡在哪。" if state == BLOCKED else "沒有卡點。"),
        next_step=one_sentence(fields.get("next_step", "")) or "沒有寫下一步。",
        last_updated=fields.get("last_updated", "") or (node.date if node else ""),
        decision_id=node.id if node else "",
        report=(node.evidence if node else "").replace("`", "").split("；")[0].split(";")[0].strip(),
    )


def build_snapshots(
    status_dir: Optional[Path] = None,
    decisions_dir: Optional[Path] = None,
) -> List[LineSnapshot]:
    """Every status line plus decisions-only lines; waiting/blocked first."""
    folder = Path(status_dir) if status_dir is not None else Path.home() / "Projects" / "status"
    board = watch.load_decision_board(
        Path(decisions_dir) if decisions_dir is not None else watch.decisions_directory())
    lines = {line.name.casefold(): line for line in board.lines}
    records: List[LineSnapshot] = []
    seen = set()
    for path in sorted(folder.glob("*.md")):
        if ".bak-" in path.name:
            continue
        try:
            fields = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if fields:
            seen.add(path.stem.casefold())
            records.append(_record(path.stem, fields, lines.get(path.stem.casefold())))
    for key, line in lines.items():
        if key not in seen:
            status = line.current.status if line.current else ""
            records.append(_record(line.name, {"name": line.title, "status": status}, line))
    records.sort(key=lambda item: (
        STATE_ORDER.get(item.state, len(STATE_ORDER)),
        tuple(-ord(char) for char in item.last_updated),
        item.title.casefold(),
    ))
    return records
