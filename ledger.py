#!/usr/bin/env python3
"""Inbox Ledger - turn a folder of JSON messages into a tamper-evident, append-only log.

  ingest   read *.json messages from an inbox folder and append new ones to the ledger
  verify   check every record's hash and the chain between records
  status   summarise what the ledger holds
  recover  write a clean copy of a damaged ledger (the original is never modified)

Properties: running ingest twice adds nothing; every change to a message body becomes a
new version (including a change back to earlier text); a broken input file is quarantined
instead of stopping the run; a last line that was cut off mid-write is set aside in
<ledger>.torn and ingest carries on in the same file; any other damage is never appended to -
ingest stops and points at recover; concurrent runs are serialised with a lock file.

Standard library only. No network. Works on Windows, macOS and Linux.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

VERSION = "1.0.3"
REQUIRED_FIELDS = ("channel", "author", "message_id", "body")
MAX_MESSAGE_BYTES = 5 * 1024 * 1024
GENESIS = "0" * 64


class LedgerError(Exception):
    pass


class LedgerDamaged(LedgerError):
    """The ledger has damaged lines or a broken chain; nothing was appended."""


# --------------------------------------------------------------------------- primitives

def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(record):
    """Stable JSON used for hashing: sorted keys, no spaces, the 'hash' field excluded."""
    return json.dumps({k: v for k, v in record.items() if k != "hash"},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def seal(record, prev_hash):
    record["prev"] = prev_hash
    record["hash"] = sha256_text(canonical(record))
    return record


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class Lock:
    """Cross-platform lock file. Created exclusively; removed on exit."""

    def __init__(self, ledger_path, wait=30.0, stale_after=600.0):
        self.path = Path(str(ledger_path) + ".lock")
        self.wait, self.stale_after = wait, stale_after
        self.held = False

    def __enter__(self):
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, ("%d %s\n" % (os.getpid(), now_utc())).encode("ascii"))
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age > self.stale_after:
                    raise LedgerError("stale lock file %s (%.0f s old); if no other run is active, delete it"
                                      % (self.path, age))
                if time.monotonic() > deadline:
                    raise LedgerError("another run holds %s; gave up after %.0f s" % (self.path, self.wait))
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


# --------------------------------------------------------------------------- reading

def read_ledger(path):
    """Read a ledger tolerantly.

    Returns a dict: records (valid, in order), corrupt (list of {line, reason}),
    chain_breaks (list of line numbers), torn_tail (bool), torn_line, torn_bytes,
    missing_newline (bool), last_hash.

    A half-written last line (no newline at the end and not a complete record - a run
    stopped while writing) is reported as torn_tail, separately from damage in the
    middle of the file. A complete, valid last record that only lacks its newline is not
    damage: the next append writes the newline first. Nothing here raises or writes.
    """
    state = {"records": [], "corrupt": [], "chain_breaks": [], "torn_tail": False, "torn_line": None,
             "torn_bytes": 0, "missing_newline": False, "last_hash": GENESIS, "lines": 0}
    path = Path(path)
    if not path.exists():
        return state
    raw = path.read_bytes()
    chunks = raw.split(b"\n")
    unterminated = bool(raw) and not raw.endswith(b"\n")
    prev = GENESIS
    for number, chunk in enumerate(chunks, 1):
        if not chunk.strip():
            continue
        state["lines"] = number
        is_tail = unterminated and number == len(chunks)
        try:
            record = json.loads(chunk.decode("utf-8"))
            if not isinstance(record, dict) or "hash" not in record or "prev" not in record:
                raise ValueError("not a ledger record")
            if record["hash"] != sha256_text(canonical(record)):
                raise KeyError("hash")
        except (ValueError, UnicodeDecodeError, KeyError, TypeError) as exc:
            if is_tail:
                state.update(torn_tail=True, torn_line=number, torn_bytes=len(chunk))
            elif isinstance(exc, KeyError):
                state["corrupt"].append({"line": number, "reason": "hash mismatch (record was altered)"})
            else:
                state["corrupt"].append({"line": number, "reason": type(exc).__name__})
            continue
        if is_tail:
            state["missing_newline"] = True
        if record["prev"] != prev:
            state["chain_breaks"].append(number)
        prev = record["hash"]
        record["_line"] = number
        state["records"].append(record)
    state["last_hash"] = prev
    return state


def append_records(path, records, missing_newline):
    """Append sealed records durably. A complete last record without its newline gets one first."""
    payload = b"".join((json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8") for r in records)
    if not payload:
        return
    with open(path, "ab") as handle:
        if missing_newline:
            handle.write(b"\n")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------- ingest

_TEXT_WS = "\t\n\r\f\v"


def sniff_utf16(raw):
    """Return 'utf-16-le' or 'utf-16-be' when BOM-less bytes are UTF-16 text, else None.

    Counting zero bytes alone misses text that is mostly Korean (or Chinese, Japanese):
    those characters have no zero byte. So the side that holds more zero bytes only picks
    the byte order; the decision is made by decoding and checking the result - almost no
    control or unassigned characters, and at least a little plain ASCII.
    """
    if len(raw) < 2 or b"\x00" not in raw:
        return None
    sample = raw[:65536]
    if len(sample) % 2:
        sample = sample[:-1]
    even, odd = sample[0::2].count(0), sample[1::2].count(0)
    if even == odd:
        return None
    codec = "utf-16-le" if odd > even else "utf-16-be"
    text = None
    for cut in (sample, sample[:-2]):
        try:
            text = cut.decode(codec)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return None
    bad = plain = 0
    for ch in text:
        code = ord(ch)
        if code < 0x80:
            if ch in _TEXT_WS or 0x20 <= code < 0x7F:
                plain += 1
            else:
                bad += 1
        elif unicodedata.category(ch) in ("Cc", "Cn", "Co", "Cs"):
            bad += 1
    if bad > len(text) * 0.01 or plain < len(text) * 0.02:
        return None
    return codec


def decode_text(raw):
    """Return (text, encoding): UTF-8 with or without BOM, UTF-16 with a BOM or detected without one."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    elif raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        codec = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-16-be"
        try:
            return raw[2:].decode(codec), codec
        except UnicodeDecodeError:
            return None, None
    codec = sniff_utf16(raw)
    if codec:
        try:
            return raw.decode(codec), codec
        except UnicodeDecodeError:
            return None, None
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return None, None


def load_message(path):
    """Return (message_dict, error_string). UTF-16 files (PowerShell 5.1 `>` writes them) are accepted."""
    try:
        if path.stat().st_size > MAX_MESSAGE_BYTES:
            return None, "file larger than %d bytes" % MAX_MESSAGE_BYTES
        text, _encoding = decode_text(path.read_bytes())
        if text is None:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "not text")
        data = json.loads(text)
    except UnicodeDecodeError:
        return None, "not valid UTF-8 or UTF-16"
    except ValueError as exc:
        return None, "invalid JSON: %s" % str(exc)[:80]
    except OSError as exc:
        return None, "unreadable: %s" % type(exc).__name__
    if not isinstance(data, dict):
        return None, "top level must be a JSON object"
    for field in REQUIRED_FIELDS:
        if not isinstance(data.get(field), str) or not data[field].strip():
            return None, "missing or empty string field %r" % field
    for field in REQUIRED_FIELDS[:3]:
        if "|" in data[field] or "\n" in data[field]:
            return None, "field %r must not contain '|' or a newline" % field
    return data, None


def repair_torn_tail(ledger, state):
    """Set aside a half-written last line. Call only under the lock, when it is the only damage.

    1. the torn bytes are appended to <ledger>.torn (one JSON line per repair: base64 + text);
    2. the ledger is cut back to its last complete line;
    3. a TAIL_REPAIRED record is chained onto the ledger.
    The bytes are saved before anything is cut, so nothing is lost if the run stops halfway.
    """
    ledger = Path(ledger)
    raw = ledger.read_bytes()
    cut = raw.rfind(b"\n") + 1
    torn = raw[cut:]
    torn_path = Path(str(ledger) + ".torn")
    entry = {"at": now_utc(), "ledger_line": state["torn_line"], "bytes": len(torn),
             "sha256": hashlib.sha256(torn).hexdigest(), "data_base64": base64.b64encode(torn).decode("ascii"),
             "text": torn.decode("utf-8", "replace")}
    with open(torn_path, "ab") as handle:
        handle.write((json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    with open(ledger, "r+b") as handle:
        handle.truncate(cut)
        handle.flush()
        os.fsync(handle.fileno())
    seq = max([r.get("seq", 0) for r in state["records"]] or [0]) + 1
    record = seal({"event": "TAIL_REPAIRED", "seq": seq, "at": now_utc(), "torn_line": state["torn_line"],
                   "torn_bytes": len(torn), "torn_sha256": entry["sha256"], "kept_in": torn_path.name},
                  state["last_hash"])
    append_records(ledger, [record], False)
    return {"torn_line": state["torn_line"], "torn_bytes": len(torn), "kept_in": str(torn_path)}


def ingest(inbox, ledger, authors=None, lock_wait=30.0):
    """Append new messages from inbox/*.json to the ledger. Returns a summary dict."""
    inbox, ledger = Path(inbox), Path(ledger)
    if not inbox.is_dir():
        raise LedgerError("inbox folder does not exist: %s" % inbox)
    allowed = {a.strip() for a in authors if a.strip()} if authors else None
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with Lock(ledger, wait=lock_wait):
        state = read_ledger(ledger)
        repaired = None
        if state["torn_tail"] and not state["corrupt"] and not state["chain_breaks"]:
            # The safe case: only the last line was cut off mid-write. Set it aside and carry on.
            repaired = repair_torn_tail(ledger, state)
            state = read_ledger(ledger)
        if state["corrupt"] or state["chain_breaks"] or state["torn_tail"]:
            # Appending now could record a message twice (its earlier record may be the
            # damaged one), so stop and let the user rebuild a clean ledger first.
            raise LedgerDamaged(
                "ledger is damaged (%d unreadable line(s), %d broken link(s)%s); nothing was added. "
                "Run:  ledger.py recover \"%s\" --out NEW_FILE   then ingest into NEW_FILE."
                % (len(state["corrupt"]), len(state["chain_breaks"]),
                   ", half-written last line" if state["torn_tail"] else "", ledger))
        latest, versions, quarantined = {}, {}, set()
        for r in state["records"]:
            if r.get("event") == "RECEIVED":
                if r["version"] >= versions.get(r["source_id"], 0):
                    latest[r["source_id"]] = r["body_sha256"]
                versions[r["source_id"]] = max(versions.get(r["source_id"], 0), r["version"])
            elif r.get("event") == "QUARANTINED":
                quarantined.add((r["file"], r["file_sha256"]))
        seq = max([r.get("seq", 0) for r in state["records"]] or [0])
        prev = state["last_hash"]
        new, summary = [], {"received": 0, "new_versions": 0, "quarantined": 0, "unchanged": 0,
                            "events": [], "tail_repaired": repaired}

        def add(record):
            nonlocal seq, prev
            seq += 1
            record.update({"seq": seq, "at": now_utc()})
            seal(record, prev)
            prev = record["hash"]
            new.append(record)
            summary["events"].append({k: record[k] for k in ("event", "file", "source_id", "version", "reason") if k in record})

        for path in sorted(inbox.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            message, error = load_message(path)
            if message and allowed is not None and message["author"] not in allowed:
                error = "author %r is not in the allowed list" % message["author"]
            if error:
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    digest = "unreadable"
                if (path.name, digest) in quarantined:
                    summary["unchanged"] += 1
                    continue
                quarantined.add((path.name, digest))
                add({"event": "QUARANTINED", "file": path.name, "file_sha256": digest, "reason": error})
                summary["quarantined"] += 1
                continue
            source = "|".join(message[f] for f in REQUIRED_FIELDS[:3])
            body_hash = sha256_text(message["body"])
            if latest.get(source) == body_hash:
                summary["unchanged"] += 1
                continue
            version = versions.get(source, 0) + 1
            latest[source] = body_hash
            versions[source] = version
            add({"event": "RECEIVED", "source_id": source, "channel": message["channel"], "author": message["author"],
                 "message_id": message["message_id"], "version": version, "body_sha256": body_hash,
                 "body_chars": len(message["body"]), "file": path.name})
            summary["received"] += 1
            summary["new_versions"] += 1 if version > 1 else 0
        append_records(ledger, new, state["missing_newline"])
    return summary


# --------------------------------------------------------------------------- verify / status / recover

def verify(ledger):
    state = read_ledger(ledger)
    ok = not state["corrupt"] and not state["chain_breaks"] and not state["torn_tail"]
    return {"ok": ok, "records": len(state["records"]), "corrupt_lines": state["corrupt"],
            "chain_breaks_at_lines": state["chain_breaks"], "torn_last_line": state["torn_tail"],
            "torn_line": state["torn_line"],
            "head": state["last_hash"] if state["records"] else None}


def status(ledger):
    state = read_ledger(ledger)
    latest, counts = {}, {}
    for r in state["records"]:
        counts[r.get("event", "?")] = counts.get(r.get("event", "?"), 0) + 1
        if r.get("event") == "RECEIVED":
            latest[r["source_id"]] = {"version": r["version"], "at": r["at"], "file": r["file"]}
    channels = {}
    for source in latest:
        channel = source.split("|", 1)[0]
        channels[channel] = channels.get(channel, 0) + 1
    intact = not state["corrupt"] and not state["chain_breaks"] and not state["torn_tail"]
    return {"integrity": "OK" if intact else "DAMAGED - run verify for details",
            "records": len(state["records"]), "events": counts, "messages": len(latest), "channels": channels,
            "revised_messages": sum(1 for v in latest.values() if v["version"] > 1),
            "damaged_lines": len(state["corrupt"]), "chain_breaks": len(state["chain_breaks"]),
            "torn_last_line": state["torn_tail"], "latest": latest}


def recover(ledger, out):
    """Write the valid records of a damaged ledger to a new file with a fresh chain."""
    ledger, out = Path(ledger), Path(out)
    if not ledger.is_file():
        raise LedgerError("ledger does not exist: %s" % ledger)
    if out.exists():
        raise LedgerError("refusing to overwrite existing file: %s" % out)
    state = read_ledger(ledger)
    prev, rebuilt = GENESIS, []
    for r in state["records"]:
        clean = {k: v for k, v in r.items() if k not in ("_line", "hash", "prev")}
        clean["recovered_from_hash"] = r["hash"]
        seal(clean, prev)
        prev = clean["hash"]
        rebuilt.append(clean)
    marker = {"event": "RECOVERED", "seq": max([r.get("seq", 0) for r in rebuilt] or [0]) + 1, "at": now_utc(),
              "original_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
              "dropped_lines": sorted([c["line"] for c in state["corrupt"]] +
                                      ([state["torn_line"]] if state["torn_tail"] else [])),
              "kept_records": len(rebuilt)}
    rebuilt.append(seal(marker, prev))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "xb") as handle:
        for r in rebuilt:
            handle.write((json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    return {"kept_records": len(rebuilt) - 1, "dropped_lines": marker["dropped_lines"], "out": str(out)}


# --------------------------------------------------------------------------- cli

def safe_stdio():
    """Never crash on a console that cannot show a character (cp949, ascii): replace it instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and (stream.encoding or "").lower().replace("-", "").replace("_", "") != "utf8":
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def emit(data):
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))


def main(argv=None):
    safe_stdio()
    parser = argparse.ArgumentParser(prog="ledger", description="Append-only, tamper-evident inbox ledger.")
    parser.add_argument("--version", action="version", version="Inbox Ledger " + VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="append new inbox messages to the ledger")
    p.add_argument("inbox")
    p.add_argument("ledger")
    p.add_argument("--authors", default="", help="comma-separated allowed authors (default: anyone)")
    p.add_argument("--lock-wait", type=float, default=30.0, help="seconds to wait for another run to finish")
    for name, text in (("verify", "check hashes and the chain"), ("status", "summarise the ledger")):
        p = sub.add_parser(name, help=text)
        p.add_argument("ledger")
    p = sub.add_parser("recover", help="write a clean copy of a damaged ledger")
    p.add_argument("ledger")
    p.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            result = ingest(args.inbox, args.ledger, args.authors.split(",") if args.authors else None, args.lock_wait)
            emit(result)
            return 0
        if args.command == "verify":
            if not Path(args.ledger).is_file():
                raise LedgerError("ledger does not exist: %s" % args.ledger)
            result = verify(args.ledger)
            emit(result)
            return 0 if result["ok"] else 1
        if args.command == "status":
            if not Path(args.ledger).is_file():
                raise LedgerError("ledger does not exist: %s" % args.ledger)
            result = status(args.ledger)
            emit(result)
            return 0 if result["integrity"] == "OK" else 1
        emit(recover(args.ledger, args.out))
        return 0
    except LedgerDamaged as exc:
        print("DAMAGED: %s" % exc, file=sys.stderr)
        return 1
    except (LedgerError, OSError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
