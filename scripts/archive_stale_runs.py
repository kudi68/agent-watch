#!/usr/bin/env python3
"""Move stale rows out of agent-watch's done TSV into a side archive file.

The dashboard never needs years of history in one TSV; this one-shot tool trims
rows whose card finished (or whose result file last changed) more than N days
ago. It is deliberately conservative:

* default is ``--dry-run``; ``--apply`` is required to write anything;
* the TSV is backed up to ``<name>.bak-awclean-<TS>`` before the rewrite;
* a row with no readable timestamp is never archived (unknown stays);
* only the TSV, its backup and the archive file are touched — no card folder is
  deleted, moved, or renamed.

Usage::

    python3 scripts/archive_stale_runs.py --done /path/agent-runs-done.tsv
    python3 scripts/archive_stale_runs.py --done ... --apply

Tests must point ``--done`` at a copy; the live TSV is read-only for this card.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

DEFAULT_DAYS = 7
PATH_COLUMNS = (2, 1)  # last/REPORT path first (它記錄完成), then run.log
DATE_IN_PATH_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")
TS_FORMAT = "%Y%m%d-%H%M%S"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive done-TSV rows older than N days (dry-run by default).",
    )
    parser.add_argument("--done", required=True, type=Path,
                        help="agent-runs-done.tsv to trim (use a copy when testing)")
    parser.add_argument("--days", type=float, default=DEFAULT_DAYS,
                        help=f"age threshold in days (default {DEFAULT_DAYS})")
    parser.add_argument("--apply", action="store_true",
                        help="write the trimmed TSV and archive (default: dry-run)")
    parser.add_argument("--archive", type=Path, default=None,
                        help="side archive TSV (default <done>.archive.tsv)")
    parser.add_argument("--backup-dir", type=Path, default=None,
                        help="where to keep the .bak-awclean-<TS> backup (default beside --done)")
    parser.add_argument("--now", type=float, default=None,
                        help="override the clock (epoch seconds) for tests")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    return parser.parse_args(argv)


def default_archive_path(done: Path) -> Path:
    return done.with_name(done.name + ".archive.tsv")


def backup_path(done: Path, backup_dir: Optional[Path], stamp: str) -> Path:
    name = f"{done.name}.bak-awclean-{stamp}"
    return (backup_dir / name) if backup_dir is not None else done.with_name(name)


def row_paths(parts: Iterable[str]) -> List[Path]:
    paths: List[Path] = []
    for index in PATH_COLUMNS:
        if index < len(parts) and parts[index].strip():
            paths.append(Path(parts[index].strip()).expanduser())
    return paths


def path_timestamp(path: Path) -> Optional[float]:
    """mtime of the row's result/log file when it exists."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime if info.st_mtime > 0 else None


def date_timestamp(text: str) -> Optional[float]:
    """Fallback: the first YYYYMMDD run folder in a path, at local midnight."""
    match = DATE_IN_PATH_RE.search(text or "")
    if match is None:
        return None
    try:
        moment = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return time.mktime(moment.timetuple())


def row_timestamp(parts: Sequence[str]) -> Optional[float]:
    """Completion evidence for a row: newest readable mtime, else a dated path."""
    stamps = [stamp for stamp in (path_timestamp(path) for path in row_paths(parts))
              if stamp is not None]
    if stamps:
        return max(stamps)
    for index in PATH_COLUMNS:
        if index < len(parts):
            stamp = date_timestamp(parts[index])
            if stamp is not None:
                return stamp
    return None


def split_rows(lines: Sequence[str], cutoff: float) -> Tuple[List[str], List[str], int]:
    """Return (kept, stale, unknown) — comments and blank lines are always kept."""
    kept: List[str] = []
    stale: List[str] = []
    unknown = 0
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            kept.append(line)
            continue
        parts = line.split("\t")
        stamp = row_timestamp(parts)
        if stamp is None:
            unknown += 1
            kept.append(line)
        elif stamp < cutoff:
            stale.append(line)
        else:
            kept.append(line)
    return kept, stale, unknown


def describe(line: str, now: float) -> str:
    parts = line.split("\t")
    stamp = row_timestamp(parts)
    age = "unknown" if stamp is None else f"{(now - stamp) / 86400:.1f}d"
    name = parts[0] if parts else line
    return f"  {age:>9}  {name}"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp",
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def append_archive(path: Path, rows: Sequence[str], stamp: str) -> None:
    if not rows:
        return
    body = "".join(line.rstrip("\n") + "\n" for line in rows)
    if path.exists():
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"# archived {stamp}\n{body}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# agent-watch done rows archived by archive_stale_runs.py\n"
            "# Same columns as agent-runs-done.tsv; grouped by archive timestamp.\n"
        )
        path.write_text(f"{header}# archived {stamp}\n{body}", encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    done = args.done.expanduser()
    if not done.is_file():
        print(f"error: --done is not a file: {done}", file=sys.stderr)
        return 2
    if args.days <= 0:
        print("error: --days must be positive", file=sys.stderr)
        return 2
    now = time.time() if args.now is None else args.now
    cutoff = now - args.days * 86400
    archive = args.archive.expanduser() if args.archive else default_archive_path(done)
    if archive.resolve() == done.resolve():
        print("error: --archive must differ from --done", file=sys.stderr)
        return 2

    lines = done.read_text(encoding="utf-8", errors="replace").splitlines()
    kept, stale, unknown = split_rows(lines, cutoff)
    stamp = datetime.fromtimestamp(now).strftime(TS_FORMAT)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {done}")
    print(f"  rows={len(lines)} keep={len(kept)} stale={len(stale)} unknown={unknown}")
    print(f"  cutoff={datetime.fromtimestamp(cutoff).isoformat(timespec='seconds')}"
          f"  days={args.days:g}")
    if not args.quiet:
        for line in stale:
            print(describe(line, now))
    if not args.apply:
        print("  dry-run only; pass --apply to move these rows")
        return 0
    if not stale:
        print("  nothing stale; no file written")
        return 0

    backup = backup_path(done, args.backup_dir.expanduser() if args.backup_dir else None,
                         stamp)
    if backup.exists():
        print(f"error: backup already exists: {backup}", file=sys.stderr)
        return 2
    done.parent.mkdir(parents=True, exist_ok=True)
    original = done.read_text(encoding="utf-8", errors="replace")
    backup.write_text(original, encoding="utf-8")
    print(f"  backup: {backup}")

    atomic_write(done, "".join(line.rstrip("\n") + "\n" for line in kept))
    append_archive(archive, stale, stamp)
    print(f"  archive: {archive}")
    print(f"  moved {len(stale)} row(s); card folders untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
