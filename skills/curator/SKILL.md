---
name: curator
description: Report skills and auto-memory files that have not been used for a long time, then archive the ones the user picks (recoverable), Hermes-agent style. Use when the user runs /curator or asks to clean up, prune, audit or tidy their skills or memories, find unused or stale skills, or restore or purge something archived earlier.
argument-hint: [deep | restore <id> | archived | purge | pin <id>]
---

The script does all the bookkeeping. Run it, never edit `~/.claude/curator/` by hand. Below, `curator` stands for the full command:

```
python3 "${CLAUDE_SKILL_DIR}/scripts/curator.py" <subcommand>
```

## How usage is known

A SessionStart hook runs `curator.py ingest` in the background. It reads new lines from the session transcripts (Skill tool calls, Read/Write/Edit on memory files) and from `~/.claude/history.jsonl` (slash commands, project activity) into `~/.claude/curator/usage.json`. Transcripts are deleted after about 30 days; the ledger keeps the counts after that. `report` also ingests first, so it is always current.

The report header prints how far back each source goes. A skill with "no recorded use" whose creation date is older than the Skill-tool coverage date may have been used before the ledger existed. Say so when you present it.

## States

- `candidate`: suggested for archive. Skills idle 60d or more and not referenced by another skill, workflow or CLAUDE.md. Memory files of type `project` untouched for 60d or more. Memory folders whose project path no longer exists. Stray non-memory files in a memory folder.
- `review`: worth a look, not suggested. Skills idle 30d or more, or idle 60d but referenced elsewhere. `project` memories idle 30d or more. Memories with no type that have been idle 60d or more. Memories missing from MEMORY.md. Projects with no session in 60d.
- `ok` or `pinned`: hidden unless `--all`.

Memories of type `feedback`, `user` and `reference` are never flagged for idle time, because MEMORY.md loads them into every session in that project. Only the `deep` pass judges them. Git-tracked project skills belong to the team repo and are skipped. Skills under `~/.claude/skills/synced` are managed remotely and skipped. Thresholds change with `--stale-days N --archive-days N` before the subcommand.

## Default flow (`/curator`)

1. Run `curator report`. If the user asked about a narrower scope (only skills, only one project), filter what you show.
2. Show the user the `candidate` rows first, then `review`, as one numbered list. One line per item: id, idle days, last use, reason. If the list goes past 15 items, show candidates in full and summarize review rows as a count with an offer to expand.
3. Ask which numbers to archive. Accept "all candidates", ranges, or ids. Never archive anything the user did not pick.
4. Run `curator archive <id> <id> ...` with the picked ids. Report what moved and give the restore command for each.
5. If the report showed `dangling-link` rows, offer `curator fix-index <memdir id>` as a separate question.
6. If the report ends with an `ARCHIVE` line that counts items old enough to purge (30 days by default, `--purge-after-days N` to change), say so in one line and offer `/curator purge`. Do not purge in this flow.

## Deep pass (`/curator deep`)

The default report is deterministic. The deep pass is the opt-in judgment step, like Hermes's LLM consolidation:

1. Run `curator report --json --all` and collect the memory file paths per project.
2. Spawn one general-purpose subagent (model sonnet) per project with five or more memory files, and a single subagent for the remaining small projects. Give each the file paths, the project's MEMORY.md, `~/.claude/CLAUDE.md`, and the project CLAUDE.md if one exists. Ask it to return a JSON list `{id, verdict, why}` where verdict is one of `obsolete` (describes finished work or a state that no longer holds), `superseded` (a later memory or CLAUDE.md reverses or replaces it), `duplicate` (same rule as another memory or CLAUDE.md, name the other), `keep`. It must quote the line that justifies every verdict other than `keep`, and must not edit anything.
3. Merge the verdicts into the numbered list from the default flow as extra candidates, with the quoted reason, then continue from step 3 of the default flow.

## Other commands

- `restore <id>`: `curator restore <id>`. Memory restores append the index line at the end of MEMORY.md, so tell the user it may need to move back under its section.
- `archived`: `curator archived`.
- `purge`: deletes archived items for good. Run `curator purge --older-than N` or `curator purge <id>` without `--yes` first, show the list, get an explicit yes for those items, then rerun with `--yes`. Never purge as part of the default flow.
- `pin <id>` and `unpin <id>`: pinned items are never flagged and cannot be archived. `skill:curator` is always pinned.

## Rules

- Archive is the only removal the default flow performs. Purge needs its own confirmation every time.
- Do not archive the curator skill, its ledger, or anything outside the ids the script prints.
- If `archive` prints "not found", the item changed since the report. Rerun the report instead of guessing a path.
