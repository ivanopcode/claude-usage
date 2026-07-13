import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector import parse_transcript, scan


def assistant(message_id, timestamp, *, input_tokens=0, output_tokens=0, cache_read=0):
    return {
        "type": "assistant",
        "sessionId": "session-1",
        "timestamp": timestamp,
        "message": {
            "id": message_id,
            "model": "claude-opus-4-6",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.db = self.root / "usage.db"

    def tearDown(self):
        self.temporary.cleanup()

    def rows(self):
        connection = sqlite3.connect(str(self.db))
        try:
            return connection.execute(
                "SELECT turn_key, input_tokens, output_tokens, cache_read_tokens "
                "FROM turns ORDER BY turn_key"
            ).fetchall()
        finally:
            connection.close()

    def test_last_streaming_record_for_message_wins(self):
        transcript = self.projects / "session.jsonl"
        write_jsonl(
            transcript,
            [
                assistant("msg-1", "2026-01-01T10:00:00Z", input_tokens=5),
                assistant("msg-1", "2026-01-01T10:00:01Z", input_tokens=11),
            ],
        )

        rows, lines = parse_transcript(transcript)

        self.assertEqual(lines, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][5], 11)

    def test_scan_is_incremental_and_replaces_changed_file(self):
        transcript = self.projects / "session.jsonl"
        write_jsonl(
            transcript,
            [assistant("msg-1", "2026-01-01T10:00:00Z", input_tokens=10)],
        )

        first = scan(projects_dir=self.projects, db_path=self.db, verbose=False)
        second = scan(projects_dir=self.projects, db_path=self.db, verbose=False)
        self.assertEqual(first["changed"], 1)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(self.rows()[0][1], 10)

        write_jsonl(
            transcript,
            [assistant("msg-1", "2026-01-01T10:00:00Z", input_tokens=1234)],
        )
        updated = scan(projects_dir=self.projects, db_path=self.db, verbose=False)
        self.assertEqual(updated["changed"], 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][1], 1234)

    def test_index_retains_turns_when_source_disappears(self):
        transcript = self.projects / "session.jsonl"
        write_jsonl(
            transcript,
            [assistant("msg-1", "2026-01-01T10:00:00Z", output_tokens=7)],
        )
        scan(projects_dir=self.projects, db_path=self.db, verbose=False)
        transcript.unlink()

        result = scan(projects_dir=self.projects, db_path=self.db, verbose=False)

        self.assertEqual(result["discovered"], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_zero_usage_and_invalid_json_are_ignored(self):
        transcript = self.projects / "session.jsonl"
        transcript.write_text(
            "not json\n"
            + json.dumps(assistant("empty", "2026-01-01T10:00:00Z"))
            + "\n",
            encoding="utf-8",
        )

        scan(projects_dir=self.projects, db_path=self.db, verbose=False)

        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
