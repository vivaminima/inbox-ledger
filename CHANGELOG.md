# Changelog

## 1.0.4
- Lock: a run that is waiting for another run no longer fails on the first refusal. On Windows the lock file of a
  run that is just finishing cannot be created for a moment ("access denied" instead of "file exists"); 1.0.3
  treated that as an error and exited 2. Now it waits and tries again until `--lock-wait` runs out. Waits are
  uneven, so runs do not retry in step.
- Lock: the lock file is removed with retries. Before, one failed delete left the lock behind and every later run
  waited and gave up.
- No record is lost or written twice in either case: nothing is appended without the lock.
- New closing line, only when the ledger's folder holds two or more ledgers: `N ledgers found. Cross-window
  summary and mirror gap check are in the Pro report.` It goes to stderr after the JSON. Copies made by `recover`
  or by copying the file are not counted. With one ledger nothing is printed.
- Tests: 49 (was 37). Concurrent-run test repeated 30 times on Windows 11, Python 3.10.11: 30 of 30 passed.
## 1.0.3
- A half-written last line (a run stopped mid-write) is now repaired automatically when it is the only damage.
  `ingest` saves the cut-off bytes to `<ledger>.torn` first (one JSON line per repair, the bytes in base64), cuts the
  ledger back to its last complete line, adds a `TAIL_REPAIRED` record, and carries on in the same file - no new
  file name, no manual step. The summary shows it under `tail_repaired`. Before, ingest stopped and asked for
  `recover` into a new file.
- Damage in the middle, or a broken link between records, still stops `ingest` exactly as before. `verify` and
  `status` still only read: they report a torn last line as `torn_line`; `recover` lists it in `dropped_lines`.
- A complete, valid last record that only lacks its final newline is no longer treated as damage.
- Message files in UTF-16 are read: with a BOM, and without one. BOM-less UTF-16 is detected by decoding and checking
  for control characters, so a message that is mostly Korean is recognised too. Before, these files were quarantined.
- Console output never crashes on a console that cannot show a character (for example cp949 or ASCII); such
  characters are replaced.
- Tests: 37 (was 26). Every fix above has its own regression test.

## 1.0.2
- A damaged ledger is no longer appended to. Before, ingestion continued past an unreadable line and could
  record the same message twice. Now `ingest` adds nothing, exits 1 and points to `recover`.
- Changing a message body back to earlier text is recorded as a new version instead of being counted as unchanged.
- Tests use neutral sample names.
- README: sample output is real tool output; Windows UTF-8 note; tested-versions list names only what was run.

## 1.0.1
- `status` reports broken links between records and a half-written last line, starts with an `integrity`
  line and exits 1 when the ledger is damaged. Before, a ledger with a valid record pointing at the wrong
  predecessor showed `damaged_lines: 0`.

## 1.0.0
- First release.
