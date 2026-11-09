#!/usr/bin/env python3
"""Read-only view of MAINLINE-STATUS.md: one line per mainline item.

The owner's status file is the source of truth for "what is每條主線做到哪、卡在哪".
This module only parses it; it never writes, moves or rewrites the file. The
dashboard shows one line per item (狀態／離開條件／證據路徑) and falls back to an
explicit placeholder when the file is missing or unreadable, so a rename or a
missing mount never takes the TUI down.

Two shapes are understood:

* a markdown table whose header has a 狀態 column (項目／卡／任務 for the name,
  離開條件／在等什麼 for the exit condition, 證據／證據路徑 for the evidence), or
* one pipe-separated line per item (``- 項目｜VERIFIED｜離開：…｜證據：…``).

Statuses are normalised to VERIFIED／PARTIAL／UNKNOWN／BLOCKED when the cell
carries one of those words; older wording (DONE／FAIL／跑中…) is mapped instead
of invented, and anything unrecognised is shown as written.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
import re

STATUS_WORDS = ("VERIFIED", "PARTIAL", "UNKNOWN", "BLOCKED")

# Legacy wording seen in the pre-2026-11-10 status file and in card REPORT.md
# headers. The map never adds a status the file does not imply.
LEGACY_STATUS = {
    "DONE": "VERIFIED",
    "PASS": "VERIFIED",
    "OK": "VERIFIED",
    "FAIL": "BLOCKED",
    "FAILED": "BLOCKED",
    "ERROR": "BLOCKED",
    "RUNNING": "PARTIAL",
    "跑中": "PARTIAL",
    "進行中": "PARTIAL",
    "未派": "UNKNOWN",
    "未做": "UNKNOWN",
    "PENDING": "UNKNOWN",
}

ITEM_HEADERS = ("項目", "卡片", "卡", "任務", "工作")
STATUS_HEADERS = ("狀態", "status")
EXIT_HEADERS = ("離開條件", "在等什麼", "在等", "等待", "卡在", "離開")
EVIDENCE_HEADERS = ("證據路徑", "證據", "報告路徑", "報告", "路徑", "evidence")

_PIPE_SPLIT_RE = re.compile(r"\s*[|｜]\s*")
_SEPARATOR_RE = re.compile(r"^[\s:|-]+$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class StatusItem:
    """One line of MAINLINE-STATUS.md, already reduced to what the board shows."""

    item: str
    status: str
    exit_condition: str
    evidence: str
    line_no: int = 0


@dataclass(frozen=True)
class StatusBoard:
    """A parsed status file; ``error`` carries the placeholder reason instead."""

    items: Tuple[StatusItem, ...]
    path: Path
    error: str = ""
    mtime: Optional[float] = None


def clean_cell(value: str) -> str:
    """Drop table/emphasis markup but keep the cell's own text (ACCEPTANCE_OK stays)."""
    text = (value or "").replace("\\|", "|").replace("`", "").strip()
    # Only edge markers are emphasis; an inner underscore is part of a name.
    text = text.strip("*_~ \t")
    return " ".join(text.split())


def normalize_status(value: str) -> str:
    """A canonical status word when the cell has one, else the cell as written."""
    text = clean_cell(value)
    upper = text.upper()
    for word in STATUS_WORDS:
        if word in upper:
            return word
    if upper in LEGACY_STATUS:
        return LEGACY_STATUS[upper]
    return text or "UNKNOWN"


def evidence_path(value: str) -> str:
    """Prefer the first backticked path in a cell; otherwise the whole cell."""
    text = clean_cell(value)
    for span in _BACKTICK_RE.findall(value or ""):
        candidate = clean_cell(span)
        if "/" in candidate or candidate.endswith(".md"):
            return candidate
    return text


def _header_column(header: List[str], keywords: Tuple[str, ...]) -> Optional[int]:
    for index, cell in enumerate(header):
        folded = clean_cell(cell).casefold()
        if any(keyword.casefold() in folded for keyword in keywords):
            return index
    return None


def _row_cells(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell for cell in stripped.split("|")]


def _table_items(lines: List[str]) -> List[StatusItem]:
    items: List[StatusItem] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.lstrip().startswith("|"):
            index += 1
            continue
        header = _row_cells(line)
        status_col = _header_column(header, STATUS_HEADERS)
        item_col = _header_column(header, ITEM_HEADERS)
        if status_col is None or item_col is None:
            index += 1
            continue
        exit_col = _header_column(header, EXIT_HEADERS)
        evidence_col = _header_column(header, EVIDENCE_HEADERS)
        index += 1
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            row_line = lines[index]
            line_no = index + 1
            index += 1
            if _SEPARATOR_RE.match(row_line.strip()):
                continue
            cells = _row_cells(row_line)
            if len(cells) <= max(status_col, item_col):
                continue
            name = clean_cell(cells[item_col])
            if not name:
                continue

            def cell(column: Optional[int]) -> str:
                if column is None or column >= len(cells):
                    return ""
                return clean_cell(cells[column])

            items.append(StatusItem(
                item=name,
                status=normalize_status(cells[status_col]),
                exit_condition=cell(exit_col),
                evidence=evidence_path(cells[evidence_col]) if evidence_col is not None else "",
                line_no=line_no,
            ))
    return items


def _pipe_line_items(lines: List[str]) -> List[StatusItem]:
    """Fallback for one-line-per-item files: ``- 項目｜VERIFIED｜離開：…｜證據：…``."""
    items: List[StatusItem] = []
    for index, line in enumerate(lines):
        stripped = line.strip().lstrip("-*• ").strip()
        if "|" not in stripped and "｜" not in stripped:
            continue
        parts = [part for part in _PIPE_SPLIT_RE.split(stripped) if part.strip()]
        if len(parts) < 3:
            continue
        status_index = next(
            (i for i, part in enumerate(parts)
             if clean_cell(part).upper() in STATUS_WORDS), None,
        )
        if status_index is None or status_index == 0:
            continue
        items.append(StatusItem(
            item=clean_cell(parts[status_index - 1]),
            status=normalize_status(parts[status_index]),
            exit_condition=clean_cell(parts[status_index + 1]) if len(parts) > status_index + 1 else "",
            evidence=evidence_path(parts[status_index + 2]) if len(parts) > status_index + 2 else "",
            line_no=index + 1,
        ))
    return items


def parse_status_document(text: str) -> List[StatusItem]:
    """Every item row in the document, table rows first, then pipe-only lines."""
    lines = (text or "").splitlines()
    items = _table_items(lines)
    if items:
        return items
    return _pipe_line_items(lines)


def _read_error(exc: OSError) -> str:
    if isinstance(exc, FileNotFoundError):
        return "檔案不存在"
    if isinstance(exc, IsADirectoryError):
        return "路徑是目錄"
    if isinstance(exc, PermissionError):
        return "沒有讀取權限"
    return str(exc)


def load_status_board(path: Path) -> StatusBoard:
    """Read the file once; any read problem becomes a placeholder, never a crash."""
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        info = target.stat()
    except OSError as exc:
        return StatusBoard((), target, error=_read_error(exc))
    items = parse_status_document(text)
    if not items:
        return StatusBoard(
            (), target, error="找不到含狀態欄的項目列", mtime=info.st_mtime,
        )
    return StatusBoard(tuple(items), target, mtime=info.st_mtime)
