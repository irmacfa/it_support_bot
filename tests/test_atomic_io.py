from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.atomic_io import atomic_exchange_paths, atomic_write_json


class AtomicWriteJsonTests(unittest.TestCase):
    def test_replaces_existing_json_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"old": true}', encoding="utf-8")

            atomic_write_json(path, {"new": "value"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": "value"})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["state.json"])

    def test_directory_exchange_is_atomic_when_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "active"
            incoming = root / "incoming"
            active.mkdir()
            incoming.mkdir()
            (active / "value.txt").write_text("old", encoding="utf-8")
            (incoming / "value.txt").write_text("new", encoding="utf-8")

            exchanged = atomic_exchange_paths(incoming, active)

            if exchanged:
                self.assertEqual((active / "value.txt").read_text(encoding="utf-8"), "new")
                self.assertEqual((incoming / "value.txt").read_text(encoding="utf-8"), "old")
            else:
                self.assertEqual((active / "value.txt").read_text(encoding="utf-8"), "old")
                self.assertEqual((incoming / "value.txt").read_text(encoding="utf-8"), "new")

    def test_serialization_error_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"old": true}', encoding="utf-8")

            with self.assertRaises(TypeError):
                atomic_write_json(path, {"not_json": object()})

            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual([p.name for p in path.parent.iterdir()], ["state.json"])

    def test_replace_error_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"old": true}', encoding="utf-8")

            with patch("app.atomic_io.os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"new": "value"})

            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual([p.name for p in path.parent.iterdir()], ["state.json"])


if __name__ == "__main__":
    unittest.main()
