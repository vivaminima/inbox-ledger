import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import ledger as lg


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.ledger = self.root / "data" / "ledger.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, name="a.json", **fields):
        msg = {"channel": "015", "author": "alice", "message_id": "m1", "body": "hello"}
        msg.update(fields)
        (self.inbox / name).write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")

    def go(self, **kw):
        return lg.ingest(self.inbox, self.ledger, **kw)

    def lines(self):
        return [l for l in self.ledger.read_text(encoding="utf-8").split("\n") if l.strip()]

    def records(self):
        return [json.loads(l) for l in self.lines()]


class Ingest(Base):
    def test_idempotent(self):
        self.put()
        self.assertEqual(self.go()["received"], 1)
        again = self.go()
        self.assertEqual((again["received"], again["unchanged"], len(self.lines())), (0, 1, 1))

    def test_every_change_is_a_new_version_including_a_revert(self):
        self.put(); self.go()
        self.put(body="hello v2"); self.go()
        self.put(body="hello")                               # back to the first text
        s = self.go()
        self.assertEqual((s["received"], s["unchanged"]), (1, 0))
        self.assertEqual([r["version"] for r in self.records()], [1, 2, 3])
        self.assertEqual(self.records()[0]["body_sha256"], self.records()[2]["body_sha256"])
        self.assertEqual(self.go()["unchanged"], 1)          # and still idempotent

    def test_same_body_different_channel_or_author_is_separate(self):
        self.put("a.json"); self.put("b.json", channel="016"); self.put("c.json", author="bob")
        self.assertEqual(self.go()["received"], 3)

    def test_body_is_not_stored_only_its_hash(self):
        self.put(body="confidential text")
        self.go()
        self.assertNotIn("confidential", self.ledger.read_text(encoding="utf-8"))

    def test_bad_inputs_are_quarantined_once_and_do_not_block_others(self):
        self.put("ok.json")
        (self.inbox / "broken.json").write_text("{ not json", encoding="utf-8")
        (self.inbox / "list.json").write_text("[1,2]", encoding="utf-8")
        (self.inbox / "nofield.json").write_text('{"channel":"1","author":"a","message_id":"m"}', encoding="utf-8")
        (self.inbox / "binary.json").write_bytes(b"\xff\xfe\x00bad")
        s = self.go()
        self.assertEqual((s["received"], s["quarantined"]), (1, 4))
        s = self.go()
        self.assertEqual((s["quarantined"], len(self.lines())), (0, 5))

    def test_fixed_file_is_accepted_after_quarantine(self):
        (self.inbox / "a.json").write_text("{ not json", encoding="utf-8")
        self.go()
        self.put("a.json")
        self.assertEqual(self.go()["received"], 1)

    def test_any_author_by_default_and_allow_list_when_given(self):
        self.put("a.json", author="someone-new")
        self.assertEqual(self.go()["received"], 1)
        self.put("b.json", author="mallory", message_id="m2")
        s = self.go(authors=["alice", "someone-new"])
        self.assertEqual((s["received"], s["quarantined"]), (0, 1))

    def test_bom_and_unicode_messages(self):
        (self.inbox / "k.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"channel": "프로젝트-가", "author": "홍길동", "message_id": "m1", "body": "한글 본문"}, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(self.go()["received"], 1)
        self.assertEqual(self.records()[0]["author"], "홍길동")

    def test_separator_in_id_fields_is_rejected(self):
        self.put(channel="a|b")
        self.assertEqual(self.go()["quarantined"], 1)

    def test_missing_inbox(self):
        with self.assertRaises(lg.LedgerError):
            lg.ingest(self.root / "nope", self.ledger)

    def test_inbox_is_never_modified(self):
        self.put(); (self.inbox / "bad.json").write_text("{", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.inbox.iterdir()}
        self.go()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.inbox.iterdir()})


class Damage(Base):
    def seed(self, n=3):
        for i in range(n):
            self.put("m%d.json" % i, message_id="m%d" % i)
        self.go()

    def assert_refuses(self):
        before = self.ledger.read_bytes()
        with self.assertRaises(lg.LedgerDamaged) as ctx:
            self.go()
        self.assertIn("recover", str(ctx.exception))
        self.assertEqual(self.ledger.read_bytes(), before)       # nothing appended, nothing rewritten

    def test_half_written_last_line_is_set_aside_and_ingest_continues(self):
        self.seed()
        with self.ledger.open("ab") as f:
            f.write(b'{"event":"RECEIVED","sourc')            # power cut mid-write
        self.put("new.json", message_id="new")
        s = self.go()
        self.assertEqual((s["received"], s["tail_repaired"]["torn_line"]), (1, 4))
        self.assertTrue(lg.verify(self.ledger)["ok"])

    def test_damaged_middle_line_never_causes_a_duplicate(self):
        # independent review: with a damaged line, b.json was recorded twice
        self.seed()
        rows = self.lines()
        rows[1] = rows[1][:20]
        self.ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.put("new.json", message_id="new")
        self.assert_refuses()
        v = lg.verify(self.ledger)
        self.assertEqual([c["line"] for c in v["corrupt_lines"]], [2])
        self.assertEqual(v["chain_breaks_at_lines"], [3])
        clean = self.root / "clean.jsonl"
        lg.recover(self.ledger, clean)
        s = lg.ingest(self.inbox, clean)
        self.assertEqual(s["received"], 2)                     # the lost message and the new one
        received = [r["source_id"] for r in lg.read_ledger(clean)["records"] if r.get("event") == "RECEIVED"]
        self.assertEqual(len(received), len(set(received)))    # no source recorded twice
        self.assertTrue(lg.verify(clean)["ok"])

    def test_broken_link_stops_ingest(self):
        self.seed()
        rows = self.records()
        rows[1]["prev"] = "f" * 64
        rows[1]["hash"] = lg.sha256_text(lg.canonical(rows[1]))
        self.ledger.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")
        self.assert_refuses()

    def test_edited_record_is_detected(self):
        self.seed()
        rows = self.records()
        rows[0]["author"] = "mallory"
        self.ledger.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")
        v = lg.verify(self.ledger)
        self.assertFalse(v["ok"])
        self.assertIn("altered", v["corrupt_lines"][0]["reason"])

    def test_deleted_record_is_detected(self):
        self.seed()
        rows = self.lines()
        self.ledger.write_text("\n".join([rows[0], rows[2]]) + "\n", encoding="utf-8")
        self.assertEqual(lg.verify(self.ledger)["chain_breaks_at_lines"], [2])

    def test_clean_ledger_verifies(self):
        self.seed()
        v = lg.verify(self.ledger)
        self.assertTrue(v["ok"])
        self.assertEqual(v["records"], 3)

    def test_recover_writes_clean_copy_and_leaves_original_alone(self):
        self.seed()
        rows = self.lines()
        rows[1] = "garbage"
        self.ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
        original = self.ledger.read_bytes()
        out = self.root / "clean.jsonl"
        r = lg.recover(self.ledger, out)
        self.assertEqual((r["kept_records"], r["dropped_lines"]), (2, [2]))
        self.assertEqual(self.ledger.read_bytes(), original)
        self.assertTrue(lg.verify(out)["ok"])
        with self.assertRaises(lg.LedgerError):
            lg.recover(self.ledger, out)                       # never overwrites
        s = lg.ingest(self.inbox, out)                         # carry on from the clean copy
        self.assertEqual(s["received"], 1)                     # only the dropped message comes back
        self.assertTrue(lg.verify(out)["ok"])

    def test_status_reports_every_kind_of_damage(self):
        # valid record hash but wrong link to the previous record (found in independent review)
        self.seed()
        rows = self.records()
        rows[1]["prev"] = "f" * 64
        rows[1]["hash"] = lg.sha256_text(lg.canonical(rows[1]))
        self.ledger.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n", encoding="utf-8")
        s = lg.status(self.ledger)
        self.assertEqual((s["damaged_lines"], s["chain_breaks"] > 0), (0, True))
        self.assertNotEqual(s["integrity"], "OK")
        self.assertEqual(lg.main(["status", str(self.ledger)]), 1)
        self.assertFalse(lg.verify(self.ledger)["ok"])

    def test_status_flags_corrupt_and_torn_lines(self):
        self.seed()
        with self.ledger.open("ab") as f:
            f.write(b"garbage\n")
        self.assertNotEqual(lg.status(self.ledger)["integrity"], "OK")
        with self.ledger.open("ab") as f:
            f.write(b'{"half')
        s = lg.status(self.ledger)
        self.assertTrue(s["torn_last_line"])
        self.assertNotEqual(s["integrity"], "OK")

    def test_status(self):
        self.seed()
        self.put("m0.json", message_id="m0", body="edited"); self.go()
        s = lg.status(self.ledger)
        self.assertEqual((s["messages"], s["revised_messages"], s["channels"]), (3, 1, {"015": 3}))
        self.assertEqual((s["integrity"], s["chain_breaks"], s["damaged_lines"]), ("OK", 0, 0))


class Locking(Base):
    def test_stale_lock_is_reported_not_ignored(self):
        self.put()
        self.ledger.parent.mkdir(parents=True)
        lock = Path(str(self.ledger) + ".lock")
        lock.write_text("x")
        import os, time
        old = time.time() - 3600
        os.utime(lock, (old, old))
        with self.assertRaises(lg.LedgerError):
            self.go()

    def test_busy_lock_times_out(self):
        self.put()
        self.ledger.parent.mkdir(parents=True)
        Path(str(self.ledger) + ".lock").write_text("x")
        with self.assertRaises(lg.LedgerError):
            self.go(lock_wait=0.2)

    def test_lock_is_released(self):
        self.put(); self.go()
        self.assertFalse(Path(str(self.ledger) + ".lock").exists())

    def test_eight_concurrent_processes_no_duplicates_no_loss(self):
        for i in range(60):
            self.put("m%02d.json" % i, message_id="m%02d" % i)
        cmd = [sys.executable, str(HERE / "ledger.py"), "ingest", str(self.inbox), str(self.ledger)]
        procs = [subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(8)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0, p.stderr.read())
            p.stderr.close()
        v = lg.verify(self.ledger)
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["records"], 60)
        self.assertEqual(sorted(r["seq"] for r in self.records()), list(range(1, 61)))


class Cli(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "ledger.py")] + [str(a) for a in args],
                              capture_output=True, text=True, encoding="utf-8")

    def test_exit_codes(self):
        self.put()
        self.assertEqual(self.run_cli("ingest", self.inbox, self.ledger).returncode, 0)
        self.assertEqual(self.run_cli("verify", self.ledger).returncode, 0)
        self.assertEqual(self.run_cli("status", self.ledger).returncode, 0)
        with self.ledger.open("ab") as f:
            f.write(b"garbage\n")
        self.assertEqual(self.run_cli("verify", self.ledger).returncode, 1)
        self.assertEqual(self.run_cli("status", self.ledger).returncode, 1)
        r = self.run_cli("ingest", self.inbox, self.ledger)
        self.assertEqual(r.returncode, 1)
        self.assertIn("recover", r.stderr)
        self.assertEqual(self.run_cli("verify", self.root / "none.jsonl").returncode, 2)
        self.assertEqual(self.run_cli("ingest", self.root / "none", self.ledger).returncode, 2)
        r = self.run_cli("recover", self.ledger, "--out", self.root / "c.jsonl")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.run_cli("verify", self.root / "c.jsonl").returncode, 0)


if __name__ == "__main__":
    unittest.main()
