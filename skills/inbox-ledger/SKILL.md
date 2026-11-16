---
name: inbox-ledger
description: Counts every note in an inbox folder exactly once into an append-only ledger and sets broken notes aside. Use when the user asks, for example, "ingest my inbox", "ledger status", "did I already count this note", "인박스 장부 정리해 줘".
---

# inbox-ledger

Counts every note in an inbox folder exactly once into an append-only ledger and sets broken notes aside. A small Python tool shipped in this plugin: standard library only, nothing to install, runs locally.

## Run

Use `python` (Windows: `py -3` also works; macOS/Linux: `python3`). On Windows add `-X utf8` if the console shows broken characters.

    python "${CLAUDE_PLUGIN_ROOT}/ledger.py" ingest <inbox-folder> <ledger.jsonl>
    python "${CLAUDE_PLUGIN_ROOT}/ledger.py" status <ledger.jsonl>
    python "${CLAUDE_PLUGIN_ROOT}/ledger.py" verify <ledger.jsonl>

Run it from the user's project folder and pass the path they mean. If no path is given, ask which folder or file to check.

## Read the result

Report how many notes were added, skipped as already counted, and set aside as broken.

Show the tool's own output (shortened if long) and then a two- or three-line summary. Do not change the user's files unless they ask.
