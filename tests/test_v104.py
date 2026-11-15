"""Regression tests for the 1.0.4 changes (one or more per fix)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import ledger as lg

LINE = "%d ledgers found. Cross-window summary and mirror gap check are in the Pro report."


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.ledger = self.data / "ledger.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def make_ledger(self, name, messages=2):
        """A real ledger made by ingest from its own inbox."""
        inbox = self.root / ("inbox-" + name)
        inbox.mkdir(exist_ok=True)
        for i in range(messages):
            text = json.dumps({"channel": name, "author": "a", "message_id": "m%d" % i, "body": "hello %d" % i})
            (inbox / ("m%d.json" % i)).write_text(text, encoding="utf-8")
        path = self.data / (name + ".jsonl")
        lg.ingest(inbox, path)
        return inbox, path

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(HERE / "ledger.py")] + [str(a) for a in args],
                              capture_output=True, env=env)


class LockRetry(Base):
    """Fix: a run waiting for the lock must wait, not fail, when Windows refuses to create the
    lock file for a moment (the previous holder's file is still being deleted)."""

    def test_permission_error_while_creating_the_lock_is_retried(self):
        real_open, calls = os.open, []

        def flaky_open(path, flags, *rest):
            if str(path).endswith(".lock") and len(calls) < 3:
                calls.append(path)
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *rest)

        inbox, _ = self.make_ledger("other")
        with mock.patch.object(lg.os, "open", side_effect=flaky_open):
            summary = lg.ingest(inbox, self.ledger, lock_wait=5.0)
        self.assertEqual(len(calls), 3)                                    # refused three times, then got it
        self.assertEqual(summary["received"], 2)
        self.assertFalse(Path(str(self.ledger) + ".lock").exists())

    def test_permission_error_that_never_ends_is_a_clean_timeout(self):
        inbox, _ = self.make_ledger("other")
        real_open = os.open

        def refuse(path, flags, *rest):
            if str(path).endswith(".lock"):
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *rest)

        with mock.patch.object(lg.os, "open", side_effect=refuse):
            with self.assertRaises(lg.LedgerError) as caught:
                lg.ingest(inbox, self.ledger, lock_wait=0.3)
        self.assertIn("gave up", str(caught.exception))
        self.assertFalse(self.ledger.exists())                             # nothing was written

    def test_lock_file_is_removed_even_when_the_first_tries_fail(self):
        inbox, _ = self.make_ledger("other")
        real_unlink, calls = Path.unlink, []

        def flaky_unlink(self_path, *args, **kwargs):
            if self_path.name.endswith(".lock") and len(calls) < 2:
                calls.append(self_path)
                raise PermissionError(13, "Permission denied")
            return real_unlink(self_path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=flaky_unlink):
            lg.ingest(inbox, self.ledger)
        self.assertEqual(len(calls), 2)
        self.assertFalse(Path(str(self.ledger) + ".lock").exists())        # was: left behind, every later run timed out
        self.assertEqual(lg.ingest(inbox, self.ledger, lock_wait=1.0)["unchanged"], 2)

    def test_twelve_concurrent_processes_no_duplicates_no_loss(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        for i in range(40):
            text = json.dumps({"channel": "c", "author": "a", "message_id": "m%02d" % i, "body": "b%d" % i})
            (inbox / ("m%02d.json" % i)).write_text(text, encoding="utf-8")
        cmd = [sys.executable, str(HERE / "ledger.py"), "ingest", str(inbox), str(self.ledger)]
        procs = [subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(12)]
        for p in procs:
            self.assertEqual(p.wait(timeout=180), 0, p.stderr.read())
            p.stderr.close()
        check = lg.verify(self.ledger)
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["records"], 40)
        self.assertFalse(Path(str(self.ledger) + ".lock").exists())


class LedgersLine(Base):
    """New: one closing line with the real number, only when two or more ledgers are there."""

    def test_no_line_for_zero_or_one_ledger(self):
        self.assertEqual(lg.count_ledgers(self.ledger), 0)
        self.assertIsNone(lg.ledgers_line(self.ledger))
        _, one = self.make_ledger("one")
        self.assertEqual(lg.count_ledgers(one), 1)
        self.assertIsNone(lg.ledgers_line(one))

    def test_line_carries_the_real_number(self):
        _, one = self.make_ledger("one")
        self.make_ledger("two")
        self.assertEqual(lg.ledgers_line(one), LINE % 2)
        self.make_ledger("three")
        self.assertEqual(lg.ledgers_line(one), LINE % 3)

    def test_only_real_ledgers_are_counted(self):
        inbox, one = self.make_ledger("one")
        (self.data / "notes.jsonl").write_text('{"hello": "world"}\n', encoding="utf-8")       # JSON lines, not a ledger
        (self.data / "empty.jsonl").write_bytes(b"")
        (self.data / "binary.jsonl").write_bytes(b"\xff\xfe\x00\x01")
        (self.data / "one.jsonl.torn").write_text("{}\n", encoding="utf-8")
        (self.data / "one.jsonl.lock").write_text("1\n", encoding="utf-8")
        forged = json.loads(one.read_text(encoding="utf-8").split("\n")[0])
        forged["seq"] = 99                                                                     # hash no longer fits
        (self.data / "forged.jsonl").write_text(json.dumps(forged) + "\n", encoding="utf-8")
        (self.data / "sub").mkdir()
        shutil.copy(str(one), str(self.data / "sub" / "elsewhere.jsonl"))                      # another folder
        self.assertEqual(lg.count_ledgers(one), 1)

    def test_recovered_copy_and_plain_copy_are_the_same_ledger(self):
        _, one = self.make_ledger("one")
        shutil.copy(str(one), str(self.data / "one.backup.jsonl"))
        lg.recover(one, self.data / "one.clean.jsonl")
        lg.recover(self.data / "one.clean.jsonl", self.data / "one.clean2.jsonl")
        self.assertEqual(lg.count_ledgers(one), 1)
        self.assertIsNone(lg.ledgers_line(self.data / "one.clean.jsonl"))
        one.unlink()
        (self.data / "one.backup.jsonl").unlink()
        self.assertEqual(lg.count_ledgers(self.data / "one.clean.jsonl"), 1)   # the copy alone is still one

    def test_cli_prints_the_line_after_the_json_and_keeps_stdout_json(self):
        inbox, one = self.make_ledger("one")
        self.make_ledger("two")
        for command in (("status", one), ("ingest", inbox, one)):
            r = self.cli(*command)
            self.assertEqual(r.returncode, 0, r.stderr)
            json.loads(r.stdout.decode("utf-8"))                             # stdout is still plain JSON
            self.assertEqual(r.stderr.decode("utf-8").strip(), LINE % 2)
        r = self.cli("verify", one)
        self.assertEqual((r.returncode, r.stderr), (0, b""))                # verify stays silent

    def test_cli_says_nothing_with_one_ledger(self):
        inbox, one = self.make_ledger("one")
        for command in (("status", one), ("ingest", inbox, one)):
            r = self.cli(*command)
            self.assertEqual((r.returncode, r.stderr), (0, b""))

    def test_counting_never_changes_a_file(self):
        _, one = self.make_ledger("one")
        _, two = self.make_ledger("two")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.iterdir()}
        lg.ledgers_line(one)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.iterdir()})

    def test_cp949_and_ascii_consoles_exit_cleanly_with_the_line(self):
        _, one = self.make_ledger("하나")
        self.make_ledger("둘")
        for enc in ("cp949", "ascii"):
            env = dict(os.environ, PYTHONIOENCODING=enc, PYTHONUTF8="0")
            r = self.cli("status", one, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(b"2 ledgers found.", r.stderr)


if __name__ == "__main__":
    unittest.main()
