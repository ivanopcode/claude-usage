"""Incrementally index Claude Code token records for the terminal UI.

Claude Code writes one JSON object per line below ``~/.claude/projects``.
This module keeps only the fields required by the charts and deliberately
avoids session analytics, pricing, browser dashboards, and project metadata.
"""

from __future__ import print_function

import json
import os
import sqlite3
from pathlib import Path


VERSION = "0.1.0"

DEFAULT_DB_PATH = Path(
    os.environ.get("CLAUDE_USAGE_TUI_DB", Path.home() / ".claude" / "usage-tui.db")
)
DEFAULT_PROJECT_DIRS = (
    Path.home() / ".claude" / "projects",
    Path.home()
    / "Library"
    / "Developer"
    / "Xcode"
    / "CodingAssistant"
    / "ClaudeAgentConfig"
    / "projects",
)


def connect(db_path=DEFAULT_DB_PATH):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def initialize(connection):
    """Create the intentionally small, standalone cache schema."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS turns (
            turn_key              TEXT PRIMARY KEY,
            source_path           TEXT NOT NULL,
            session_id            TEXT,
            timestamp             TEXT,
            model                 TEXT,
            input_tokens          INTEGER NOT NULL DEFAULT 0,
            output_tokens         INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path       TEXT PRIMARY KEY,
            mtime_ns   INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            line_count INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_turns_timestamp
            ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_turns_session_timestamp
            ON turns(session_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_turns_source
            ON turns(source_path);
        """
    )
    connection.commit()


def _integer(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_transcript(path):
    """Return final token records from one JSONL transcript.

    Claude Code can emit several snapshots for one assistant response. The
    final occurrence of a message id wins. Records without ids use a stable
    source-path/line key so an unchanged file is reproducible.
    """
    source = str(Path(path).expanduser().resolve())
    by_key = {}
    line_count = 0

    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line_count, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if record.get("type") != "assistant":
                continue

            message = record.get("message") or {}
            usage = message.get("usage") or {}
            input_tokens = _integer(usage.get("input_tokens"))
            output_tokens = _integer(usage.get("output_tokens"))
            cache_read = _integer(usage.get("cache_read_input_tokens"))
            cache_creation = _integer(usage.get("cache_creation_input_tokens"))
            if input_tokens + output_tokens + cache_read + cache_creation <= 0:
                continue

            message_id = str(message.get("id") or "")
            if message_id:
                turn_key = "message:" + message_id
            else:
                turn_key = "line:{}:{}".format(source, line_count)
            by_key[turn_key] = (
                turn_key,
                source,
                str(record.get("sessionId") or ""),
                str(record.get("timestamp") or ""),
                str(message.get("model") or "unknown"),
                input_tokens,
                output_tokens,
                cache_read,
                cache_creation,
            )
    return list(by_key.values()), line_count


def _transcript_paths(project_dirs):
    paths = set()
    for directory in project_dirs:
        root = Path(directory).expanduser()
        if root.exists():
            paths.update(path.resolve() for path in root.rglob("*.jsonl") if path.is_file())
    return sorted(paths, key=lambda path: str(path))


def _unchanged(connection, path, stat):
    row = connection.execute(
        "SELECT mtime_ns, size_bytes FROM processed_files WHERE path = ?",
        (str(path),),
    ).fetchone()
    return bool(
        row
        and int(row["mtime_ns"]) == int(stat.st_mtime_ns)
        and int(row["size_bytes"]) == int(stat.st_size)
    )


def scan(projects_dir=None, project_dirs=None, db_path=DEFAULT_DB_PATH, verbose=True):
    """Refresh changed transcripts and retain rows for files later removed.

    Retaining indexed rows is intentional: Claude Code can clean old JSONL
    files, while historical token activity remains useful to the owner.
    """
    if project_dirs is not None:
        roots = tuple(Path(path) for path in project_dirs)
    elif projects_dir is not None:
        roots = (Path(projects_dir),)
    else:
        roots = DEFAULT_PROJECT_DIRS

    connection = connect(db_path)
    initialize(connection)
    discovered = _transcript_paths(roots)
    changed = 0
    skipped = 0
    rows_written = 0

    try:
        for path in discovered:
            try:
                stat = path.stat()
            except OSError:
                continue
            if _unchanged(connection, path, stat):
                skipped += 1
                continue
            try:
                rows, line_count = parse_transcript(path)
            except OSError as error:
                if verbose:
                    print("warning: could not read {}: {}".format(path, error))
                continue

            source = str(path)
            with connection:
                connection.execute("DELETE FROM turns WHERE source_path = ?", (source,))
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO turns (
                        turn_key, source_path, session_id, timestamp, model,
                        input_tokens, output_tokens, cache_read_tokens,
                        cache_creation_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO processed_files
                        (path, mtime_ns, size_bytes, line_count)
                    VALUES (?, ?, ?, ?)
                    """,
                    (source, int(stat.st_mtime_ns), int(stat.st_size), int(line_count)),
                )
            changed += 1
            rows_written += len(rows)
    finally:
        connection.close()

    result = {
        "discovered": len(discovered),
        "changed": changed,
        "skipped": skipped,
        "turns_written": rows_written,
        "db_path": str(Path(db_path).expanduser()),
    }
    if verbose:
        print(
            "Indexed {changed} changed transcript(s), kept {skipped} unchanged; "
            "{turns_written} turn(s) written to {db_path}".format(**result)
        )
    return result


if __name__ == "__main__":
    scan()
