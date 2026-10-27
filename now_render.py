"""現況頁渲染：把 line_status 的每線一筆畫成單欄卡片。

只輸出純文字（不含 rich markup），每行都量過東亞字寬，保證不超過指定欄寬；
45 欄手機終端不需要換另一種版型。分頁與按鍵（卡 3）之後再把這個字串放進 Static。
"""

import re
from collections import Counter
from typing import Iterable, List, Sequence, Tuple

import watch
from line_status import ACTIVE, BLOCKED, DONE, IDLE, WAITING, LineSnapshot, human_title

STATE_ORDER = {BLOCKED: 0, WAITING: 1, ACTIVE: 2, DONE: 3, IDLE: 4}
STATE_LABEL = {BLOCKED: "被擋", WAITING: "等你", ACTIVE: "進行中", DONE: "完成", IDLE: "空閒"}
BADGE = {BLOCKED: "[被擋]", WAITING: "[等你]", ACTIVE: "[進行中]", DONE: "[完成]",
         IDLE: "[空閒]"}
MIN_WIDTH = 24
MAX_WIDTH = 45
INDENT = "  "
# Longest first: the quiet board keeps its full sentence when it fits and falls back
# to shorter wording on narrow terminals, so the line never exceeds the width asked for.
QUIET_LINES = (
    "沒事，現在沒有等你、被擋或進行中的線。",
    "沒事，沒有等你、被擋或進行中的線。",
    "沒事：沒有等你或被擋的線。",
    "沒事，沒有要你處理的。",
    "沒事。",
)
QUIET_LINE = QUIET_LINES[0]
EMPTY_BLOCKED = "沒有卡點。"
EMPTY_NEXT = "沒有寫下一步。"
LIVE_STATES = (BLOCKED, WAITING, ACTIVE)
CLOSERS = "。，、；：！？）」』】》…,.;:!?)"
# Keep closing punctuation glued to the word before it, so no line starts with a comma or stop.
TOKEN_RE = re.compile(
    r"[ \t\u3000]+|(?:[!-~]+|.)(?:[%s\]]*)" % re.escape(CLOSERS), re.S)


def _wrap(text: str, width: int) -> List[str]:
    """Wrap by display width, but keep ASCII words (paths, ids) in one piece."""
    if width <= 0:
        return [""]
    output: List[str] = []
    for source in str(text).splitlines() or [""]:
        current, used, pending_space = "", 0, False
        for token in TOKEN_RE.findall(source):
            if token.isspace():
                pending_space = bool(current)
                continue
            add = (" " if pending_space else "") + token
            pending_space = False
            width_add = watch.display_width(add)
            if current and used + width_add > width:
                output.append(current.rstrip())
                current, used, add, width_add = "", 0, token, watch.display_width(token)
            if width_add > width:
                for char in add:
                    char_width = watch.display_width(char)
                    if current and used + char_width > width:
                        output.append(current.rstrip())
                        current, used = "", 0
                    current += char
                    used += char_width
            else:
                current += add
                used += width_add
        output.append(current.rstrip())
    _move_openers(output, width)
    return output or [""]


OPENERS = "（「『【《("


def _move_openers(lines: List[str], width: int) -> None:
    """A line should not end on an opening bracket; slide it onto the next line."""
    for index in range(len(lines) - 1):
        while lines[index] and lines[index][-1] in OPENERS:
            char = lines[index][-1]
            if watch.display_width(char) + watch.display_width(lines[index + 1]) > width:
                break
            lines[index] = lines[index][:-1].rstrip()
            lines[index + 1] = char + lines[index + 1]


def _normalise(text: str) -> str:
    """Compare sentences without punctuation or spacing, to drop repeats."""
    return re.sub(r"[\s。，、；：！？,.;:!?（）()「」『』]", "", str(text or ""))


def _field(label: str, text: str, width: int) -> List[str]:
    """One labelled sentence; continuation lines align under the sentence."""
    prefix = f"{INDENT}{label}："
    hanging = " " * watch.display_width(prefix)
    available = max(1, width - watch.display_width(prefix))
    parts = _wrap(text, available)
    return [prefix + parts[0]] + [hanging + part for part in parts[1:]]


def _body(snapshot: LineSnapshot) -> List[Tuple[str, str]]:
    """Which human sentences this card shows; placeholders and repeats are skipped."""
    rows: List[Tuple[str, str]] = [("現在", snapshot.doing or "")]
    blocked = (snapshot.blocked_on or "").strip()
    if blocked and blocked != EMPTY_BLOCKED:
        rows.append(("卡在", blocked))
    following = (snapshot.next_step or "").strip()
    if following and following != EMPTY_NEXT:
        rows.append(("下一步", following))
    kept: List[Tuple[str, str]] = []
    seen = set()
    for label, text in rows:
        key = _normalise(text)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append((label, text))
    return kept


def _card(snapshot: LineSnapshot, width: int) -> List[str]:
    badge = BADGE.get(snapshot.state, BADGE[IDLE])
    title_width = max(4, width - watch.display_width(badge) - 1)
    # Belt and braces: the data layer already cleans titles, never show a code even if it slips.
    clean_title = human_title(snapshot.title, snapshot.name) or "未命名線"
    title = watch.truncate_display(clean_title, title_width)
    rows = [f"{badge} {title}"]
    for label, text in _body(snapshot):
        rows.extend(_field(label, text, width))
    return rows


def _header(records: Sequence[LineSnapshot], width: int) -> List[str]:
    counts = Counter(record.state for record in records)
    parts = [f"現況 {len(records)} 條"]
    for state in (BLOCKED, WAITING, ACTIVE, DONE, IDLE):
        if counts.get(state):
            parts.append(f"{STATE_LABEL[state]} {counts[state]}")
    return _wrap(" · ".join(parts), width)


def _quiet_line(width: int) -> str:
    """The longest "nothing to do" sentence that still fits this terminal."""
    for candidate in QUIET_LINES:
        if watch.display_width(candidate) <= width:
            return candidate
    return QUIET_LINES[-1]


def render_now(records: Iterable[LineSnapshot], width: int = MAX_WIDTH) -> str:
    """Single-column cards, attention first, quiet board collapsed to one line."""
    width = max(MIN_WIDTH, int(width or MAX_WIDTH))
    # Stable sort: keep the data layer's order (newest first) inside each state.
    ordered = sorted(
        records, key=lambda item: STATE_ORDER.get(item.state, len(STATE_ORDER)))
    live = [item for item in ordered if item.state in LIVE_STATES]
    if not live:
        # Nothing needs a human: no grey cards, just one honest line that fits.
        return _quiet_line(width)
    lines = _header(ordered, width)
    lines.append("-" * width)
    for snapshot in live:
        lines.append("")
        lines.extend(_card(snapshot, width))
    finished = [item.title for item in ordered if item.state == DONE]
    if finished:
        lines.append("")
        lines.extend(_field(f"已完成 {len(finished)} 條", "、".join(finished), width))
    return "\n".join(lines)
