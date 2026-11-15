# Inbox Ledger

*Part of the [Vivaminima tools](https://github.com/vivaminima) for Claude Code users.*

Turn a folder of JSON messages into an append-only log you can trust: nothing counted twice,
nothing silently lost, and any later edit to the log is detectable.

## New to this? Start here

**What problem it solves, in one sentence:** when you run more than one Claude window (or Claude plus a script), they pass notes to each other as files in a folder — and you can't tell whether a note really arrived, arrived twice, or was changed later. Inbox Ledger is a **receipt book** for that folder.

**Who it's for:** people who already split work across two or more AI windows — for example one Claude that plans and one that builds — and hand results over through a shared folder. If you only ever use one chat window, you don't need this yet.

**What a "message" is here:** a small JSON file with four fields — channel, author, message_id, body. Claude can write these for you.

**You don't need to know Python or the terminal.** Claude runs it for you.

1. Open the **Claude desktop app → Code tab** (Claude Code) and pick the folder your windows use as their shared inbox.
2. Download this repository (green **Code** button → **Download ZIP**) and unzip it anywhere.
3. Type the sentence below to Claude, with the path to the unzipped folder. Claude runs the script and explains the result in plain words.
4. If Claude says Python is missing, ask: *"Install Python 3 for me, step by step."*

> *"Use ledger.py from the inbox-ledger folder: ingest my inbox folder into ledger.jsonl, then run status and tell me what arrived, what changed and what was rejected."*

What you get back: how many notes arrived, which ones changed (version 2, 3…), which broken files were set aside, and whether anyone edited the receipt book afterwards.

Built for hand-offs between agents, scripts and people — "did that message actually arrive, and
which version?" One Python file. No dependencies. Works offline.

```
python ledger.py ingest inbox/ data/ledger.jsonl
python ledger.py verify data/ledger.jsonl
python ledger.py status data/ledger.jsonl
python ledger.py recover data/ledger.jsonl --out data/ledger.clean.jsonl
```

A real session (two good messages and one broken file in the inbox):

```
$ python ledger.py ingest inbox ledger.jsonl
{
  "received": 2,
  "new_versions": 0,
  "quarantined": 1,
  "unchanged": 0,
}

$ python ledger.py ingest inbox ledger.jsonl        # run it again: nothing new
{
  "received": 0,
  "new_versions": 0,
  "quarantined": 0,
  "unchanged": 3,
  "events": []
}

$ python ledger.py verify ledger.jsonl              # exit code 0
{
  "ok": true,
  "records": 3,
  "corrupt_lines": [],
  "chain_breaks_at_lines": [],
  "torn_last_line": false,
  "head": "fefc55922981e6af86f466a9d9d85287aa6863910a404945c6621baa8f778ff7"
}
```

## Message format

Each file in the inbox is one JSON object with four string fields:

```json
{"channel": "release-42", "author": "alice", "message_id": "m-001", "body": "Ship it."}
```

A message is identified by `channel | author | message_id`. Other fields are allowed and ignored.
The body itself is **not** copied into the ledger — only its SHA-256 and length — so the ledger can be
shared without sharing the content.

## Guarantees

| Situation | What happens |
|---|---|
| Same inbox ingested twice | Nothing is added the second time |
| A message body is edited | Recorded as version 2, 3, ... of the same message |
| A body is changed back to earlier text | Also a new version - the ledger shows what happened, in order |
| A message file is UTF-16 (PowerShell 5.1 `>` writes it), with or without a BOM | Read normally |
| A file is not valid JSON, or a field is missing | Recorded once as `QUARANTINED` with the reason; other files continue |
| The quarantined file is fixed | Accepted on the next run |
| The ledger has a damaged line in the middle, or a broken link | `ingest` adds nothing, exits 1 and tells you to run `recover` |
| Only the last line is half-written (a run was stopped mid-write) | `ingest` repairs it by itself: the cut-off bytes go to `ledger.jsonl.torn`, a `TAIL_REPAIRED` record is added, and new messages go into the same file. `verify` and `status` only report it; they never change the file |
| The last record is complete but lacks its final newline | Not damage: the next `ingest` adds the newline and continues |
| Someone edits or deletes a record | `verify` and `status` report it: each record carries its own hash and the previous record's hash |
| Two runs start at once | A lock file makes them take turns. A run that cannot get the lock waits and tries again (up to `--lock-wait` seconds); it does not fail on the first refusal. Tested with 8 and with 12 processes at once |
| You run `recover` | A clean, re-chained copy is written; the original is untouched; messages whose records were lost are received again on the next ingest |

Why ingest stops on damage: the unreadable line may be the record of a message that is still in the inbox.
Appending anyway would record that message a second time. What that looks like:

```
$ python ledger.py ingest inbox ledger.jsonl        # exit code 1
DAMAGED: ledger is damaged (1 unreadable line(s), 0 broken link(s)); nothing was added. Run:  ledger.py recover "ledger.jsonl" --out NEW_FILE   then ingest into NEW_FILE.

$ python ledger.py recover ledger.jsonl --out ledger.clean.jsonl
{
  "kept_records": 3,
  "dropped_lines": [
    4
  ],
  "out": "ledger.clean.jsonl"
}
```

The inbox is never modified.

## Options

```
ingest --authors alice,bob     only accept these authors (default: anyone)
ingest --lock-wait SECONDS     how long to wait for another run (default 30)
```

Exit codes: `0` success, `1` the ledger is damaged (`verify`, `status` and `ingest` all report it),
`2` bad path, lock timeout or other error.

When the folder of your ledger holds two or more ledgers, `ingest` and `status` end with one line that says how
many, for example `2 ledgers found. Cross-window summary and mirror gap check are in the Pro report.` It is
printed after the JSON, on stderr, so stdout stays plain JSON. A `recover` copy or a plain file copy of a ledger is
not counted as another ledger. With one ledger nothing is printed.

`status` always starts with an `integrity` line. It reports damaged lines, broken links between records
and a half-written last line, so a summary can never look healthy while `verify` would fail.

If a run is killed, its lock file (`ledger.jsonl.lock`) may remain. After 10 minutes the tool reports
it as stale and asks you to delete it — it will not guess.

## What it is not

- Not a database and not a message queue. It is a receipt book.
- Tamper-evident, not tamper-proof: someone who can rewrite the whole file can rebuild the chain.
  Keep a copy of the `head` hash from `verify` somewhere else if that matters to you.
- The lock file protects runs on one machine. It is not designed for network drives.

## Windows

Use UTF-8 mode so non-English text prints correctly in the console:

```
python -X utf8 ledger.py status data/ledger.jsonl
```

## Requirements and what was tested

Python 3 standard library only - nothing to install.

- Tested: Windows 11, Python 3.10.11 (this release, 49 tests, including a cp949 console run).
- Known issue: on one Windows machine the 8-process test of 1.0.3 failed once in 30 runs. 1.0.4 makes a waiting
  run retry instead of failing; after the change the same test passed 30 of 30 runs on Windows 11. If you still see
  it fail, raise `--lock-wait`; no record is lost either way.
- Version 1.0.2 was tested on Linux, Python 3.13; 1.0.0 was also run on Windows with Python 3.10 by an independent reviewer.
- Other versions and platforms have not been tested.

Run the tests yourself with `python -m unittest discover -s tests`.

MIT licence. See CHANGELOG.md for what changed.

## More from Vivaminima

- **Inbox Ledger Pro ($19)**, the paid edition of this tool, one-time payment on Gumroad: https://vivastory4.gumroad.com/l/inbox-ledger-pro

  <a href="https://vivastory4.gumroad.com/l/inbox-ledger-pro"><img src="https://raw.githubusercontent.com/vivaminima/vivaminima/main/img/inbox-ledger.png" width="480" alt="Inbox Ledger Pro"></a>

- **Token Audit Kit ($19)** — see how many tokens your CLAUDE.md and its @imports cost before your first prompt, and a step-by-step guide to split it. Free script: https://github.com/vivaminima/context-file-audit · Paid kit (store): https://vivastory4.gumroad.com
- Free tools: [skill-audit-kit](https://github.com/vivaminima/skill-audit-kit) · [secret-map](https://github.com/vivaminima/secret-map) · [inbox-ledger](https://github.com/vivaminima/inbox-ledger) · [release-gate](https://github.com/vivaminima/release-gate) · [skill-install-gate](https://github.com/vivaminima/skill-install-gate) · [console-safe](https://github.com/vivaminima/console-safe) · [checklist-stop](https://github.com/vivaminima/checklist-stop) · [handoff-check](https://github.com/vivaminima/handoff-check) · [llm-budget-router-lite](https://github.com/vivaminima/llm-budget-router-lite) · [go-no-go-verdict-skill](https://github.com/vivaminima/go-no-go-verdict-skill) · [ai-company-starter-kit](https://github.com/vivaminima/ai-company-starter-kit) · [beginner-readme-section](https://github.com/vivaminima/beginner-readme-section)

Vivaminima is not affiliated with, endorsed by or sponsored by Anthropic.
