"""tok/s per card and per model/tier from `magpie usage --csv` (spec needs-attention-v2 §E, plan N11).

magpie is the only token source. A card's run.log is read only as an anchor
(its session id); tokens are never estimated from text. Missing data is shown
as 「—」, never as 0.

Card figure (N2 §3.2): Σoutput_tokens ÷ Σduration_ms × 1000 over rows with
status 200 that were not rejected, summed first and divided once.

Daily TSV, long format, one row per (date, group, name):
    date  group  name  cards  mean  median  p10  p90
`group` is `model` (served_model, falling back to model) or `tier` (the
dispatch alias in requested_model). The numbers are tok/s of the cards seen
that day; an empty cell means no data. Rerunning the same day replaces that
day's rows and keeps the others.
"""

import csv
import io
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

MAGPIE_COMMAND = os.environ.get("AGENT_WATCH_MAGPIE", "magpie")
USAGE_ARGS = ("usage", "--csv", "today")
USAGE_TIMEOUT_SECONDS = 8
# The CSV read is cached at least this long; the UI may ask every second.
USAGE_CACHE_SECONDS = 10.0
MIN_CACHE_SECONDS = 5.0
MIN_SAMPLE_ROWS = 5
MAX_ROW_RATE = 1000.0
MAX_DROPPED_OUTPUT_SHARE = 0.2
WINDOW_SLACK_SECONDS = 2.0
SESSION_SCAN_BYTES = 64 * 1024
DASH = "—"
REQUIRED_COLUMNS = ("time", "output_tokens", "duration_ms", "status")
TSV_HEADER = ("date", "group", "name", "cards", "mean", "median", "p10", "p90")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CODEX_SESSION_RE = re.compile(
    r"^[ \t]*session id:[ \t]*([0-9A-Za-z][0-9A-Za-z_-]{15,})[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
OPENCODE_SESSION_RE = re.compile(r"\b(ses_[A-Za-z0-9]{8,})")
MODEL_WRAPPER_PREFIXES = ("magpie/",)
FALSE_WORDS = {"", "0", "false", "no", "none", "null"}


@dataclass(frozen=True)
class UsageRow:
    time: Optional[float]
    agent: str
    requested_model: str
    model: str
    output_tokens: Optional[float]
    duration_ms: Optional[float]
    ttft_ms: Optional[float]
    session: str


@dataclass(frozen=True)
class CardAnchor:
    """What ties a card to magpie rows: session ids first, else a time window."""
    key: str
    agent: str = ""
    alias: str = ""
    start: Optional[float] = None
    end: Optional[float] = None  # None: still running, the window ends now
    log_path: str = ""
    sessions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CardSpeed:
    requests: int
    output_tokens: int
    duration_ms: float
    decode_rate: Optional[float]
    model: str
    tier: str

    @property
    def rate(self) -> Optional[float]:
        if self.requests < MIN_SAMPLE_ROWS or self.output_tokens <= 0 or self.duration_ms <= 0:
            return None
        return self.output_tokens / self.duration_ms * 1000.0


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _timestamp(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _text(row: Dict[str, Any], key: str) -> str:
    return str(row.get(key) or "").strip()


def normalize_model_name(value: Any) -> str:
    """Remove known source wrappers while preserving the model/group namespace."""
    name = str(value or "").strip().casefold()
    while True:
        prefix = next((item for item in MODEL_WRAPPER_PREFIXES if name.startswith(item)), None)
        if prefix is None:
            return name
        name = name[len(prefix):]


def row_is_counted(row: Dict[str, Any]) -> bool:
    """Only answered requests count: status 200 and not rejected."""
    status = _number(row.get("status"))
    if status is None or int(status) != 200:
        return False
    return _text(row, "rejected").casefold() in FALSE_WORDS


def parse_usage_rows(
    source: Union[str, Iterable[Dict[str, Any]]],
) -> Tuple[List[UsageRow], str]:
    """Turn CSV text or dict rows into counted UsageRows; a missing column is an error."""
    if isinstance(source, str):
        reader = csv.DictReader(io.StringIO(source))
        fields = reader.fieldnames or []
        if not fields:
            return [], "magpie usage 沒有輸出資料"
        missing = [name for name in REQUIRED_COLUMNS if name not in fields]
        if missing:
            return [], "magpie usage 缺欄位：" + "、".join(missing)
        records: Iterable[Dict[str, Any]] = reader
    else:
        records = source
    rows = []
    for record in records:
        if not isinstance(record, dict) or not row_is_counted(record):
            continue
        rows.append(UsageRow(
            time=_timestamp(record.get("time")),
            agent=_text(record, "agent").casefold(),
            requested_model=_text(record, "requested_model"),
            model=_text(record, "served_model") or _text(record, "model"),
            output_tokens=_number(record.get("output_tokens")),
            duration_ms=_number(record.get("duration_ms")),
            ttft_ms=_number(record.get("ttft_ms")),
            session=_text(record, "session"),
        ))
    return rows, ""


def _usable(rows: Iterable[UsageRow]) -> List[UsageRow]:
    return [
        row for row in rows
        if row.output_tokens is not None and row.duration_ms is not None and row.duration_ms > 0
    ]


def decode_rate(rows: Sequence[UsageRow]) -> Optional[float]:
    """Σoutput ÷ Σ(duration − ttft), with N2 §3.2's guards; None means show only the main figure."""
    usable = _usable(rows)
    total_output = sum(row.output_tokens for row in usable)
    kept_output = 0.0
    kept_ms = 0.0
    kept = 0
    for row in usable:
        if row.ttft_ms is None:
            continue
        generating = row.duration_ms - row.ttft_ms
        if generating <= 0:
            continue
        if row.output_tokens / generating * 1000.0 > MAX_ROW_RATE:
            continue
        kept_output += row.output_tokens
        kept_ms += generating
        kept += 1
    if kept < MIN_SAMPLE_ROWS or kept_output <= 0 or kept_ms <= 0 or total_output <= 0:
        return None
    if (total_output - kept_output) / total_output > MAX_DROPPED_OUTPUT_SHARE:
        return None
    return kept_output / kept_ms * 1000.0


def _dominant(values: Iterable[Tuple[str, float]]) -> str:
    weights: Counter = Counter()
    for name, weight in values:
        if name:
            weights[name] += weight
    return weights.most_common(1)[0][0] if weights else ""


def compute_card_speed(rows: Sequence[UsageRow], alias: str = "") -> Optional[CardSpeed]:
    """Weighted card speed; None when no magpie row is joined to the card."""
    if not rows:
        return None
    usable = _usable(rows)
    output = sum(row.output_tokens for row in usable)
    duration = sum(row.duration_ms for row in usable)
    model = _dominant((row.model, row.output_tokens) for row in usable) or \
        _dominant((row.model, 1.0) for row in rows)
    tier = _dominant((row.requested_model, row.output_tokens) for row in usable) or alias
    return CardSpeed(
        requests=len(usable),
        output_tokens=int(output),
        duration_ms=duration,
        decode_rate=decode_rate(usable),
        model=model,
        tier=tier,
    )


def assign_rows(
    anchors: Sequence[CardAnchor], rows: Sequence[UsageRow], now: float,
) -> Dict[str, List[UsageRow]]:
    """Join rows to cards: session id first; a time window only when it is unambiguous."""
    joined: Dict[str, List[UsageRow]] = {}
    claimed = {session for anchor in anchors for session in anchor.sessions}
    for anchor in anchors:
        if anchor.sessions:
            wanted = set(anchor.sessions)
            matched = [row for row in rows if row.session and row.session in wanted]
            if matched:
                joined[anchor.key] = matched

    window_hits: Dict[str, List[int]] = {}
    owners: Dict[int, List[str]] = {}
    for anchor in anchors:
        if anchor.sessions or anchor.start is None or not anchor.agent or not anchor.alias:
            continue
        start = anchor.start - WINDOW_SLACK_SECONDS
        end = (now if anchor.end is None else anchor.end) + WINDOW_SLACK_SECONDS
        alias = normalize_model_name(anchor.alias)
        hits = [
            index for index, row in enumerate(rows)
            if row.time is not None and start <= row.time <= end
            and row.agent == anchor.agent
            and normalize_model_name(row.requested_model) == alias
            and not (row.session and row.session in claimed)
        ]
        window_hits[anchor.key] = hits
        for index in hits:
            owners.setdefault(index, []).append(anchor.key)
    for key, hits in window_hits.items():
        # Drop only rows that fit multiple cards; keep the unambiguous samples.
        unique_hits = [index for index in hits if len(owners[index]) == 1]
        if unique_hits:
            joined[key] = [rows[index] for index in unique_hits]
    return joined


def compute_all(
    anchors: Sequence[CardAnchor], rows: Sequence[UsageRow], now: float,
) -> Dict[str, CardSpeed]:
    aliases = {anchor.key: anchor.alias for anchor in anchors}
    result = {}
    for key, matched in assign_rows(anchors, rows, now).items():
        speed = compute_card_speed(matched, aliases.get(key, ""))
        if speed is not None:
            result[key] = speed
    return result


def extract_session_ids(text: str, agent: str = "") -> Tuple[str, ...]:
    """Read only session markers native to the card's harness."""
    text = ANSI_RE.sub("", text)
    found: List[str] = []
    harness = magpie_agent_name(agent)
    if harness == "codex":
        match = CODEX_SESSION_RE.search(text)
        if match:
            found.append(match.group(1))
    elif harness == "claude":
        for line in text.splitlines():
            line = line.strip()
            if not (line.startswith("{") and '"session_id"' in line):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            # Only Claude's init event identifies the session; later event
            # payloads may contain copied logs or tool output.
            if record.get("type") != "system" or record.get("subtype") != "init":
                continue
            value = record.get("session_id")
            if isinstance(value, str) and value.strip() and value.strip() not in found:
                found.append(value.strip())
    elif harness == "opencode":
        candidates = list(dict.fromkeys(OPENCODE_SESSION_RE.findall(text)))
        # An OpenCode log prints ses_ ids only incidentally; more than one is
        # not evidence of which one is this card's own session.
        if len(candidates) == 1:
            found.append(candidates[0])
    return tuple(found)


def magpie_agent_name(harness: str) -> str:
    """'Claude Code' → 'claude', 'OpenCode' → 'opencode'; unknown → ''."""
    words = str(harness or "").strip().casefold().split()
    if not words or words[0] in ("?", "未知", "unknown"):
        return ""
    return words[0].replace("-", "")


def run_magpie_usage(
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = USAGE_TIMEOUT_SECONDS,
) -> Tuple[str, str]:
    """The only subprocess: `magpie usage --csv today`. Returns (csv_text, error)."""
    try:
        result = runner(
            [MAGPIE_COMMAND, *USAGE_ARGS],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return "", "magpie usage 逾時"
    except Exception as error:
        return "", f"magpie usage 無法執行：{error}"
    if getattr(result, "returncode", 1) != 0:
        return "", f"magpie usage 結束碼 {getattr(result, 'returncode', '?')}"
    output = getattr(result, "stdout", "") or ""
    if not isinstance(output, str) or not output.strip():
        return "", "magpie usage 沒有輸出資料"
    return output, ""


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """Linear interpolation between closest ranks (numpy's default)."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_groups(
    speeds: Iterable[CardSpeed],
) -> List[Tuple[str, str, int, float, float, float, float]]:
    """(group, name, cards, mean, median, p10, p90) per model and per tier; cards without a figure are left out."""
    buckets: Dict[Tuple[str, str], List[float]] = {}
    for speed in speeds:
        rate = speed.rate
        if rate is None:
            continue
        for group, name in (("model", speed.model), ("tier", speed.tier)):
            if name:
                buckets.setdefault((group, name), []).append(rate)
    result = []
    for (group, name), rates in sorted(buckets.items()):
        result.append((
            group, name, len(rates), sum(rates) / len(rates),
            percentile(rates, 0.5), percentile(rates, 0.1), percentile(rates, 0.9),
        ))
    return result


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.1f}"
    return re.sub(r"[\t\r\n]+", " ", str(value))


def render_daily_tsv(existing: str, date: str, groups: Sequence[Tuple[Any, ...]]) -> str:
    kept = [
        line for line in existing.splitlines()
        if line.strip() and not line.startswith("date\t") and not line.startswith(date + "\t")
    ]
    lines = ["\t".join(TSV_HEADER), *kept]
    lines.extend("\t".join([date, *(_cell(value) for value in group)]) for group in groups)
    return "\n".join(lines) + "\n"


def write_daily_tsv(path: Path, date: str, groups: Sequence[Tuple[Any, ...]]) -> bool:
    """Replace `date`'s rows in the long-format TSV at `path`; returns False when nothing changed."""
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    content = render_daily_tsv(existing, date, groups)
    if content == existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return True


def format_card_speed(speed: Optional[CardSpeed]) -> str:
    """`112 tok/s · output 10,324 ÷ 生成 92.1 s · 14 請求`; no figure → 「—」."""
    if speed is None:
        return f"{DASH} tok/s"
    rate = speed.rate
    if rate is None:
        if 0 < speed.requests < MIN_SAMPLE_ROWS:
            return f"{DASH} tok/s · 樣本不足 {speed.requests} 請求"
        return f"{DASH} tok/s"
    text = "{:,.0f} tok/s · output {:,} ÷ 生成 {:.1f} s · {} 請求".format(
        rate, speed.output_tokens, speed.duration_ms / 1000.0, speed.requests,
    )
    if speed.decode_rate is not None:
        text += " · 解碼 {:,.0f} tok/s".format(speed.decode_rate)
    return text


class TokSpeedService:
    """Reads magpie usage off the UI thread, at most once per cache interval."""

    def __init__(
        self,
        interval: float = USAGE_CACHE_SECONDS,
        tsv_path: Optional[Path] = None,
        reader: Optional[Callable[[], Tuple[str, str]]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.interval = max(MIN_CACHE_SECONDS, float(interval))
        self.tsv_path = Path(tsv_path) if tsv_path is not None else None
        self.reader = reader or run_magpie_usage
        self.clock = clock
        self.error = ""
        self.version = 0
        self.last_fetch = float("-inf")
        self._results: Dict[str, CardSpeed] = {}
        self._fetching = False
        self._lock = threading.Lock()
        self._session_cache: Dict[str, Tuple[int, Tuple[str, ...]]] = {}

    def result(self, key: str) -> Optional[CardSpeed]:
        with self._lock:
            return self._results.get(key)

    def refresh_in_background(self, anchors: Sequence[CardAnchor], force: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._fetching or (not force and now - self.last_fetch < self.interval):
                return False
            self.last_fetch = now
            self._fetching = True
        worker = threading.Thread(
            target=self._refresh_worker, args=(list(anchors),), daemon=True, name="agent-watch-toks",
        )
        worker.start()
        return True

    def _refresh_worker(self, anchors: List[CardAnchor]) -> None:
        try:
            self.refresh_now(anchors)
        finally:
            with self._lock:
                self._fetching = False

    def refresh_now(self, anchors: Sequence[CardAnchor]) -> None:
        """One read and recompute. Any failure clears the figures to 「—」 and never raises."""
        results: Dict[str, CardSpeed] = {}
        try:
            text, error = self.reader()
            if not error:
                rows, error = parse_usage_rows(text)
                if not error:
                    now = self.clock()
                    resolved = [self._with_sessions(anchor) for anchor in anchors]
                    results = compute_all(resolved, rows, now)
        except Exception as exc:
            results, error = {}, f"tok/s 計算失敗：{exc}"
        if not error and self.tsv_path is not None:
            try:
                date = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
                groups = summarize_groups(results.values())
                if groups:
                    write_daily_tsv(self.tsv_path, date, groups)
            except Exception as exc:
                error = f"tok/s 每日 TSV 寫入失敗：{exc}"
        with self._lock:
            # A failed read must not keep old figures on screen.
            self._results = results
            self.error = error
            self.version += 1

    def _with_sessions(self, anchor: CardAnchor) -> CardAnchor:
        if anchor.sessions or not anchor.log_path:
            return anchor
        sessions = self.session_ids(anchor.log_path, anchor.agent)
        if not sessions:
            return anchor
        return CardAnchor(
            key=anchor.key, agent=anchor.agent, alias=anchor.alias,
            start=anchor.start, end=anchor.end, log_path=anchor.log_path, sessions=sessions,
        )

    def session_ids(self, log_path: str, agent: str = "") -> Tuple[str, ...]:
        """Scan a run.log's head once; rescan only while no id was found and the file changed."""
        try:
            size = os.stat(log_path).st_size
        except OSError:
            return ()
        cached = self._session_cache.get(log_path)
        if cached is not None:
            cached_size, ids = cached
            if size == cached_size or (ids and size > cached_size):
                return ids
        try:
            with open(log_path, "rb") as stream:
                head = stream.read(SESSION_SCAN_BYTES)
        except OSError:
            return ()
        ids = extract_session_ids(head.decode("utf-8", errors="replace"), agent)
        self._session_cache[log_path] = (size, ids)
        return ids
