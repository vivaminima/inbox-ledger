"""Regression tests for the 1.0.3 changes (one or more per fix)."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import ledger as lg


def korean_text(lines=40):
    """Text that is more than 80% Hangul, with a little ASCII."""
    row = "가나다라마바사아자차카타파하대한민국"
    text = "".join("%s %d\n" % (row, i % 10) for i in range(lines))
    hangul = sum(1 for c in text if "가" <= c <= "힣")
    assert hangul / len(text) >= 0.8
    return text


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.ledger = self.root / "ledger.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, name, mid, body="hello", encoding="utf-8"):
        text = json.dumps({"channel": "c", "author": "a", "message_id": mid, "body": body}, ensure_ascii=False)
        (self.inbox / name).write_bytes(text.encode(encoding))

    def seed(self):
        for i in range(3):
            self.put("m%d.json" % i, "m%d" % i)
        lg.ingest(self.inbox, self.ledger)


class TornTail(Base):
    def torn_path(self):
        return Path(str(self.ledger) + ".torn")

    def test_verify_names_the_torn_line_and_changes_nothing(self):
        self.seed()
        with self.ledger.open("ab") as f:
            f.write(b'{"event":"RECEIVED","sourc')
        before = self.ledger.read_bytes()
        v = lg.verify(self.ledger)
        self.assertEqual((v["ok"], v["torn_last_line"], v["torn_line"], v["corrupt_lines"]), (False, True, 4, []))
        self.assertEqual(self.ledger.read_bytes(), before)          # verify and status only read

    def test_torn_tail_is_set_aside_and_ingest_continues_in_the_same_file(self):
        self.seed()
        torn = b'{"event":"RECEIVED","sourc'
        with self.ledger.open("ab") as f:
            f.write(torn)
        self.put("new.json", "new")
        s = lg.ingest(self.inbox, self.ledger)
        self.assertEqual(s["received"], 1)
        self.assertEqual((s["tail_repaired"]["torn_line"], s["tail_repaired"]["torn_bytes"]), (4, len(torn)))
        self.assertTrue(lg.verify(self.ledger)["ok"])                # same file name, chain intact
        events = [r["event"] for r in lg.read_ledger(self.ledger)["records"]]
        self.assertEqual(events, ["RECEIVED"] * 3 + ["TAIL_REPAIRED", "RECEIVED"])
        kept = [json.loads(l) for l in self.torn_path().read_text(encoding="utf-8").splitlines()]
        self.assertEqual(base64.b64decode(kept[0]["data_base64"]), torn)
        self.assertIsNone(lg.ingest(self.inbox, self.ledger)["tail_repaired"])

    def test_second_repair_appends_to_the_same_torn_file(self):
        self.seed()
        for _ in range(2):
            with self.ledger.open("ab") as f:
                f.write(b'{"half')
            lg.ingest(self.inbox, self.ledger)
        self.assertEqual(len(self.torn_path().read_text(encoding="utf-8").splitlines()), 2)
        self.assertTrue(lg.verify(self.ledger)["ok"])

    def test_middle_damage_plus_torn_tail_still_stops_and_changes_nothing(self):
        self.seed()
        rows = self.ledger.read_text(encoding="utf-8").splitlines()
        rows[1] = rows[1][:20]
        self.ledger.write_text("\n".join(rows) + "\n" + '{"half', encoding="utf-8")
        before = self.ledger.read_bytes()
        with self.assertRaises(lg.LedgerDamaged) as ctx:
            lg.ingest(self.inbox, self.ledger)
        self.assertIn("recover", str(ctx.exception))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertFalse(self.torn_path().exists())

    def test_cli_reports_the_repair_and_exits_zero(self):
        self.seed()
        with self.ledger.open("ab") as f:
            f.write(b'{"half')
        p = subprocess.run([sys.executable, "-I", str(HERE / "ledger.py"), "ingest", str(self.inbox), str(self.ledger)],
                           capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout.decode("utf-8"))["tail_repaired"]["torn_line"], 4)

    def test_recover_lists_the_torn_line_as_dropped(self):
        self.seed()
        with self.ledger.open("ab") as f:
            f.write(b'{"half')
        r = lg.recover(self.ledger, self.root / "clean.jsonl")
        self.assertEqual((r["kept_records"], r["dropped_lines"]), (3, [4]))

    def test_complete_last_record_without_newline_is_not_damage(self):
        self.seed()
        self.ledger.write_bytes(self.ledger.read_bytes().rstrip(b"\n"))
        self.assertTrue(lg.verify(self.ledger)["ok"])
        self.put("new.json", "new")
        self.assertEqual(lg.ingest(self.inbox, self.ledger)["received"], 1)
        self.assertTrue(lg.verify(self.ledger)["ok"])
        self.assertEqual(lg.verify(self.ledger)["records"], 4)


class Utf16Messages(Base):
    def test_bomless_utf16_mostly_korean_message_is_received(self):
        body = korean_text()
        for i, codec in enumerate(("utf-16-le", "utf-16-be")):
            self.put("k%d.json" % i, "k%d" % i, body=body, encoding=codec)
        s = lg.ingest(self.inbox, self.ledger)
        self.assertEqual((s["received"], s["quarantined"]), (2, 0))
        rec = lg.read_ledger(self.ledger)["records"][0]
        self.assertEqual(rec["body_sha256"], lg.sha256_text(body))

    def test_utf16_with_bom_is_received(self):
        text = json.dumps({"channel": "c", "author": "a", "message_id": "b", "body": "x"})
        (self.inbox / "b.json").write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
        self.assertEqual(lg.ingest(self.inbox, self.ledger)["received"], 1)

    def test_cp949_message_is_still_quarantined(self):
        self.put("c.json", "c", body=korean_text(2), encoding="cp949")
        s = lg.ingest(self.inbox, self.ledger)
        self.assertEqual(s["quarantined"], 1)
        self.assertIn("UTF", lg.read_ledger(self.ledger)["records"][0]["reason"])


class Console(Base):
    def test_cp949_and_ascii_consoles_exit_cleanly(self):
        self.put("k.json", "k", body="— " + korean_text(2))
        (self.inbox / "bad—.json").write_text("{", encoding="utf-8")
        for enc in ("cp949", "ascii"):
            with self.subTest(enc=enc):
                env = dict(os.environ, PYTHONIOENCODING=enc, PYTHONUTF8="0")
                p = subprocess.run([sys.executable, "-I", str(HERE / "ledger.py"), "ingest", str(self.inbox),
                                    str(self.ledger)], capture_output=True, env=env)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertNotIn(b"Traceback", p.stderr)
                for cmd in ("verify", "status"):
                    p = subprocess.run([sys.executable, "-I", str(HERE / "ledger.py"), cmd, str(self.ledger)],
                                       capture_output=True, env=env)
                    self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
