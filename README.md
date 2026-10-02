# claude-curator

A Claude Code plugin that tells you which of your skills and memories you have stopped using, and moves the ones you choose out of the way. It is modelled on the curator in the Hermes agent. Nothing is deleted outright: archived items can be put back, and permanent deletion is a separate step you have to ask for.

## Install

```
/plugin marketplace add AlucarDWeb/claude-curator
/plugin install curator@claude-curator
```

Start a new session afterwards. You need `python3` 3.9 or newer (the one macOS ships is enough) on macOS or Linux.

## Use

Run `/curator:curator` in a session, or ask Claude to clean up your skills or memories.

- `/curator:curator` lists what looks unused and asks which numbers to archive.
- `/curator:curator deep` also has Claude read your memories and flag the ones that describe finished work, were replaced by a newer note, or repeat something written elsewhere. It runs subagents, so it uses more tokens.
- `/curator:curator restore <id>`, `archived`, `pin <id>` and `purge` do what their names say.

The script also works from a terminal. Run it with `--help` from the plugin folder: `python3 skills/curator/scripts/curator.py --help`.

## What it looks at

- Your personal skills in `~/.claude/skills`, and skills in a project's `.claude/skills` that are not committed to git. Skills committed to a repo belong to the team and are left alone. Plugin skills and skills synced from claude.ai are left alone too.
- Auto-memory files in `~/.claude/projects/*/memory`.

## How it knows what you use

Claude Code deletes session transcripts after about 30 days, so the plugin keeps its own count in `~/.claude/curator/usage.json`. When a session starts, a background hook reads the new lines of your transcripts (which skills ran, which memory files were read or written) and of `~/.claude/history.jsonl` (the slash commands you typed and the projects you worked in).

On the first run it only sees the last 30 days of skills Claude invoked on its own, plus the slash commands you typed for as far back as your history file goes. The report prints both dates. A skill marked "no recorded use" may have been used before them.

## How it decides

- A skill unused for 30 days is listed for review. After 60 days it is suggested for archive, unless another skill, workflow or your `CLAUDE.md` refers to it.
- A memory about ongoing work (type `project`) follows the same clock. Memories that hold your preferences and rules (types `feedback`, `user` and `reference`) load in every session of their project, so idle time says nothing about them. Only the deep pass judges those.
- A memory folder whose project no longer exists on disk is suggested for archive, and so is a file in a memory folder that is not a memory.
- `--stale-days` and `--archive-days` change the two thresholds.

## Archive, restore, purge

Archiving moves the item to `~/.claude/curator/archive/`. For a memory it also removes the line from that project's `MEMORY.md`. Restoring moves the item back and appends the line to the end of `MEMORY.md`, so you may want to move it under the right heading.

Purge deletes archived items for good. Claude shows the list first and waits for your yes. The report reminds you when archived items are more than 30 days old.

## Privacy

Everything runs on your machine with the Python standard library. Nothing is sent anywhere.

## Uninstall

```
/plugin uninstall curator@claude-curator
```

Your ledger and archive stay in `~/.claude/curator/`. Delete that folder if you want them gone too.

## License

MIT. See [LICENSE](LICENSE).
