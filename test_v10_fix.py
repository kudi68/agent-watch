import tempfile
import unittest
from pathlib import Path

import watch


class TsvNoteTests(unittest.TestCase):
    def load_row(self, fields):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        log = root / "run.log"
        report = root / "REPORT.md"
        log.write_text("work\n", encoding="utf-8")
        report.write_text("DONE\n", encoding="utf-8")
        row = [fields[0], str(log), str(report), *fields[3:]]
        path = root / "runs.tsv"
        path.write_text("\t".join(row) + "\n", encoding="utf-8")
        return watch.load_tsv_agents(
            path, True, watch.LogParser(process_start_reader=None), 1_800_000_000,
        )

    def test_nine_columns_load_note_without_format_error(self):
        fields = ["task-a", "", "", "", "", "", "", "", "ordinary note"]
        agents, errors = self.load_row(fields)
        self.assertEqual(errors, [])
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].note, "ordinary note")

    def test_superseded_note_extracts_replacement_name(self):
        fields = ["task-a", "", "", "", "", "", "", "", "SUPERSEDED(by x; q1=C)"]
        agents, errors = self.load_row(fields)
        self.assertEqual(errors, [])
        self.assertEqual(agents[0].superseded_hint, "x")

    def test_three_and_eight_column_rows_remain_compatible(self):
        for width in (3, 8):
            fields = ["task-a", "", "", "", "", "", "", ""][:width]
            with self.subTest(width=width):
                agents, errors = self.load_row(fields)
                self.assertEqual(errors, [])
                self.assertEqual(len(agents), 1)
                self.assertEqual(agents[0].note, "")
                self.assertEqual(agents[0].superseded_hint, "")

    def test_ten_columns_are_rejected(self):
        fields = ["task-a", "", "", "", "", "", "", "", "note", "extra"]
        agents, errors = self.load_row(fields)
        self.assertEqual(agents, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("需 3 至 9 欄", errors[0])


if __name__ == "__main__":
    unittest.main()
