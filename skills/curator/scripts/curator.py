#!/usr/bin/env python3
"""Curator for Claude Code skills and auto-memory, modelled on the Hermes agent curator.

Usage lives in a ledger (~/.claude/curator/usage.json) fed from session transcripts and
history.jsonl, so it outlives the transcript cleanup. Archive moves items under
~/.claude/curator/archive and restore puts them back. Only purge deletes anything.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
PROJECTS = CLAUDE / "projects"
SKILLS = CLAUDE / "skills"
HISTORY = CLAUDE / "history.jsonl"
DATA = CLAUDE / "curator"
LEDGER = DATA / "usage.json"
ARCHIVE = DATA / "archive"

SELF_ID = "skill:curator"
EXCLUDED_SKILL_DIRS = {"synced"}
MEMORY_TOOLS = {"Read": "read", "Write": "write", "Edit": "write", "MultiEdit": "write"}
SLASH_RE = re.compile(r"/([A-Za-z0-9][\w:.-]*)")
LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


HOME_ENC = encode(Path.home())


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_epoch(sec):
    return datetime.fromtimestamp(sec, timezone.utc)


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def norm(ts):
    dt = parse(ts)
    return iso(dt) if dt else None


def latest(*values):
    values = [v for v in values if v]
    return max(values) if values else None


def days_since(ts):
    dt = parse(ts)
    return None if dt is None else max(0, (now() - dt).days)


def birth(path):
    st = os.lstat(path)
    return iso(from_epoch(getattr(st, "st_birthtime", st.st_mtime)))


def mtime(path):
    return iso(from_epoch(os.stat(path).st_mtime))


def short_project(enc):
    if enc == HOME_ENC:
        return "~"
    return enc[len(HOME_ENC) + 1:] if enc.startswith(HOME_ENC + "-") else enc.lstrip("-")


def tilde(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path and path.startswith(home) else path


# --- ledger ---------------------------------------------------------------

def empty_ledger():
    return {"version": 1, "created_at": iso(now()), "offsets": {}, "skills": {},
            "memories": {}, "projects": {}, "pinned": [], "coverage": {}}


def load_ledger():
    led = empty_ledger()
    try:
        led.update(json.loads(LEDGER.read_text()))
    except (OSError, ValueError):
        pass
    return led


def save_ledger(led):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=1, sort_keys=True))
    os.replace(tmp, LEDGER)


@contextmanager
def ledger_lock(block=True):
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / ".lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def bump(table, key, ts, count_field, last_field):
    rec = table.setdefault(key, {})
    rec[count_field] = rec.get(count_field, 0) + 1
    rec[last_field] = latest(rec.get(last_field), ts)
    if ts and (not rec.get("first_seen_at") or ts < rec["first_seen_at"]):
        rec["first_seen_at"] = ts


def new_lines(path, offsets):
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    start = offsets.get(key, 0)
    if size < start:
        start = 0
    if size == start:
        return []
    with open(path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return []
    offsets[key] = start + end + 1
    return chunk[:end].decode("utf-8", "replace").splitlines()


def ingest_transcript(path, led):
    lines = new_lines(path, led["offsets"])
    if not lines:
        return
    proj = led["projects"].setdefault(path.relative_to(PROJECTS).parts[0], {})
    memory_prefix = str(PROJECTS) + "/"
    for line in lines:
        if "cwd" not in proj:
            m = re.search(r'"cwd":"([^"]+)"', line)
            if m:
                proj["cwd"] = m.group(1)
        if '"name":"Skill"' not in line and not ("/memory/" in line and '"tool_use"' in line):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        ts = norm(entry.get("timestamp"))
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, args = block.get("name"), block.get("input") or {}
            if name == "Skill" and args.get("skill"):
                bump(led["skills"], args["skill"].lstrip("/"), ts, "use_count", "last_used_at")
            elif name in MEMORY_TOOLS:
                fp = args.get("file_path") or ""
                if fp.startswith(memory_prefix) and "/memory/" in fp:
                    kind = MEMORY_TOOLS[name]
                    bump(led["memories"], fp, ts, kind + "_count", "last_" + kind + "_at")
    proj["last_active_at"] = latest(proj.get("last_active_at"), mtime(path))


def ingest_history(led):
    for line in new_lines(HISTORY, led["offsets"]):
        try:
            entry = json.loads(line)
            ts = iso(from_epoch(entry["timestamp"] / 1000))
        except (ValueError, KeyError, TypeError):
            continue
        cov = led["coverage"]
        cov["history_since"] = min(cov.get("history_since") or ts, ts)
        if entry.get("project"):
            proj = led["projects"].setdefault(encode(entry["project"]), {})
            proj["cwd"] = entry["project"]
            proj["last_active_at"] = latest(proj.get("last_active_at"), ts)
        m = SLASH_RE.match((entry.get("display") or "").strip())
        if m:
            bump(led["skills"], m.group(1), ts, "use_count", "last_used_at")


def ingest(block=True):
    with ledger_lock(block) as acquired:
        if not acquired:
            return None
        led = load_ledger()
        transcripts = list(PROJECTS.glob("**/*.jsonl")) if PROJECTS.is_dir() else []
        if transcripts and not led["coverage"].get("transcripts_since"):
            led["coverage"]["transcripts_since"] = min(birth(p) for p in transcripts)
        for path in transcripts:
            ingest_transcript(path, led)
        ingest_history(led)
        live = {str(p) for p in transcripts} | {str(HISTORY)}
        led["offsets"] = {k: v for k, v in led["offsets"].items() if k in live}
        led["last_ingest_at"] = iso(now())
        save_ledger(led)
        return led


# --- inventory --------------------------------------------------------------

def frontmatter(path):
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    lines = text[3:end if end > 0 else len(text)].splitlines()
    out = {}
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Za-z_]+):\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value in (">", "|", ">-", "|-"):
            folded = []
            for nxt in lines[i + 1:]:
                if nxt and not nxt[0].isspace():
                    break
                folded.append(nxt.strip())
            value = " ".join(x for x in folded if x)
        if value and key not in out:
            out[key] = value.strip("\"'")
    return out


def resolve_encoded(enc):
    """Find the directory whose encoded path equals enc, or None."""
    def walk(base, rest):
        if not rest:
            return base
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return None
        for name in entries:
            part = "-" + encode(name)
            if rest == part or rest.startswith(part + "-"):
                sub = os.path.join(base, name)
                if os.path.isdir(sub):
                    found = walk(sub, rest[len(part):])
                    if found:
                        return found
        return None
    return walk("/", enc)


def project_info(enc, led):
    info = led["projects"].get(enc, {})
    cwd = info.get("cwd") or resolve_encoded(enc)
    active = info.get("last_active_at")
    for t in (PROJECTS / enc).glob("*.jsonl"):
        active = latest(active, mtime(t))
    return {"cwd": cwd, "exists": bool(cwd and os.path.isdir(cwd)), "last_active_at": active}


def git_tracked_skills(skills_root):
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(skills_root), "ls-files", "."],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {Path(line).parts[0] for line in out.splitlines() if len(Path(line).parts) > 1}


def reference_corpus():
    files = [CLAUDE / "CLAUDE.md"]
    files += list(SKILLS.glob("*/SKILL.md"))
    for sub in ("workflows", "agents"):
        files += [p for p in (CLAUDE / sub).glob("*") if p.is_file()]
    corpus = {}
    for f in files:
        try:
            corpus[f] = f.read_text(errors="replace")
        except OSError:
            pass
    return corpus


def skill_state(idle, uses, referenced, a):
    if uses == 0 and idle is not None and idle < a.stale_days:
        return "ok", "new, not used yet"
    if idle is not None and idle >= a.archive_days and not referenced:
        return "candidate", None
    if idle is not None and idle >= a.stale_days:
        return "review", None
    return "ok", None


def inventory_skills(led, a):
    corpus = reference_corpus()
    roots = [(SKILLS, None)]
    for enc, proj in led["projects"].items():
        cwd = proj.get("cwd")
        if cwd and (Path(cwd) / ".claude" / "skills").is_dir() and Path(cwd) != Path.home():
            roots.append((Path(cwd) / ".claude" / "skills", short_project(enc)))
    seen, rows = set(), []
    for root, scope in roots:
        if not root.is_dir():
            continue
        # Skills committed to a project repo belong to the team, not to this user's library.
        team_owned = git_tracked_skills(root) if scope else set()
        for d in sorted(root.iterdir()):
            if (d.name.startswith(".") or d.name in EXCLUDED_SKILL_DIRS or d.name in team_owned
                    or not (d / "SKILL.md").exists()):
                continue
            real = os.path.realpath(d)
            if real in seen:
                continue
            seen.add(real)
            fm = frontmatter(d / "SKILL.md")
            names = {d.name, fm.get("name") or d.name}
            recs = [led["skills"][n] for n in names if n in led["skills"]]
            uses = sum(r.get("use_count", 0) for r in recs)
            last_used = latest(*(r.get("last_used_at") for r in recs))
            created = birth(d)
            idle = days_since(last_used or created)
            name_re = re.compile(r"(?<![\w-])/?(%s)(?![\w-])" % "|".join(re.escape(n) for n in names))
            refs = sorted(tilde(str(f)) for f, text in corpus.items()
                          if not str(f).startswith(str(d) + "/") and not os.path.realpath(f).startswith(real + "/")
                          and name_re.search(text))
            sid = "skill:" + (d.name if scope is None else scope + "/" + d.name)
            state, note = skill_state(idle, uses, bool(refs), a)
            reasons = [note] if note else []
            if uses == 0 and state != "ok":
                reasons.append("no recorded use")
            if refs and idle is not None and idle >= a.archive_days:
                reasons.append("referenced by " + ", ".join(refs[:2]))
            if os.path.islink(d):
                reasons.append("symlink to " + tilde(os.readlink(d)))
            if sid == SELF_ID or sid in led["pinned"]:
                state, reasons = "pinned", reasons
            rows.append({"id": sid, "kind": "skill", "path": str(d), "scope": scope or "user",
                         "description": fm.get("description", ""), "created_at": created,
                         "last_used_at": last_used, "use_count": uses, "idle_days": idle,
                         "state": state, "reasons": reasons})
    return rows


def index_links(index_path):
    try:
        text = index_path.read_text(errors="replace")
    except OSError:
        return set()
    return {m.split("#")[0] for m in LINK_RE.findall(text) if not re.match(r"[a-z]+:", m)}


def inventory_memory(led, a):
    dirs, files, issues = [], [], []
    if not PROJECTS.is_dir():
        return dirs, files, issues
    for pdir in sorted(PROJECTS.iterdir()):
        mdir = pdir / "memory"
        if not mdir.is_dir():
            continue
        proj = short_project(pdir.name)
        info = project_info(pdir.name, led)
        entries = sorted(p for p in mdir.iterdir() if not p.name.startswith("."))
        index = mdir / "MEMORY.md"
        linked = index_links(index)
        mems = [p for p in entries if p.suffix == ".md" and p.name != "MEMORY.md"]
        if not mems and not [p for p in entries if p.name != "MEMORY.md"]:
            continue
        dormant = days_since(info["last_active_at"])
        did = "memdir:" + proj
        reasons, state = [], "ok"
        if not info["exists"]:
            state = "candidate"
            reasons.append("project path not found on disk (renamed or deleted?)"
                           if info["cwd"] else "project path unknown and not resolvable")
        elif dormant is not None and dormant >= a.archive_days:
            state = "review"
            reasons.append("no session in this project for %dd" % dormant)
        if did in led["pinned"]:
            state = "pinned"
        dirs.append({"id": did, "kind": "memdir", "path": str(mdir), "project": proj,
                     "project_path": info["cwd"], "project_exists": info["exists"],
                     "project_last_active_at": info["last_active_at"], "file_count": len(mems),
                     "state": state, "reasons": reasons})
        for name in sorted(linked):
            if not (mdir / name).exists():
                issues.append({"id": did, "kind": "dangling-link", "file": name,
                               "detail": "MEMORY.md links a file that does not exist"})
        for p in entries:
            if p.is_file() and p.suffix != ".md":
                issues.append({"id": "mem:%s/%s" % (proj, p.name), "kind": "stray-file", "file": p.name,
                               "detail": "not a memory file", "path": str(p)})
        if state == "candidate":
            continue
        for p in mems:
            fm = frontmatter(p)
            rec = led["memories"].get(str(p), {})
            touch = latest(mtime(p), rec.get("last_read_at"), rec.get("last_write_at"))
            idle = days_since(touch)
            mtype = fm.get("type", "?")
            st, why = "ok", []
            if p.name not in linked:
                st = "review"
                why.append("not listed in MEMORY.md, so never loaded")
            # feedback, user and reference memories apply on every session through the index,
            # so time since the last edit says nothing about whether they are still needed.
            if idle is None or mtype in ("feedback", "user", "reference"):
                pass
            elif mtype == "project" and idle >= a.archive_days:
                st = "candidate"
                why.append("project memory untouched for %dd" % idle)
            elif (mtype == "project" and idle >= a.stale_days) or idle >= a.archive_days:
                st = "review"
                why.append("untouched for %dd" % idle + ("" if mtype == "project" else ", no type"))
            mid = "mem:%s/%s" % (proj, p.stem)
            if mid in led["pinned"] or did in led["pinned"]:
                st = "pinned"
            files.append({"id": mid, "kind": "memory", "path": str(p), "project": proj, "type": mtype,
                          "description": fm.get("description", ""), "last_touch_at": touch,
                          "read_count": rec.get("read_count", 0), "idle_days": idle,
                          "state": st, "reasons": why})
    return dirs, files, issues


def build_report(led, a):
    dirs, files, issues = inventory_memory(led, a)
    archived = archived_entries()
    purgeable = [e["id"] for e in archived if days_since(e["archived_at"]) >= a.purge_after_days]
    return {"generated_at": iso(now()),
            "archive": {"count": len(archived), "purge_after_days": a.purge_after_days, "purgeable": purgeable},
            "thresholds": {"stale_days": a.stale_days, "archive_days": a.archive_days},
            "coverage": {"skill_tool_calls_since": led["coverage"].get("transcripts_since"),
                         "slash_commands_since": led["coverage"].get("history_since"),
                         "ledger_since": led.get("created_at")},
            "skills": inventory_skills(led, a), "memory_dirs": dirs, "memories": files,
            "index_issues": issues}


# --- output -------------------------------------------------------------------

def day(ts):
    return ts[:10] if ts else "never"


def print_report(rep, show_all):
    order = {"candidate": 0, "review": 1, "pinned": 2, "ok": 3}
    keep = (lambda r: True) if show_all else (lambda r: r["state"] in ("candidate", "review"))
    cov, th = rep["coverage"], rep["thresholds"]
    print("Curator report %s (review after %dd idle, archive candidate after %dd)"
          % (rep["generated_at"][:10], th["stale_days"], th["archive_days"]))
    print("Usage data: Skill tool calls since %s, slash commands since %s, ledger since %s"
          % (day(cov["skill_tool_calls_since"]), day(cov["slash_commands_since"]), day(cov["ledger_since"])))

    def summary(rows):
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        return ", ".join("%d %s" % (counts[s], s) for s in sorted(counts, key=order.get))

    def emit(title, rows, fmt):
        print("\n%s (%d: %s)" % (title, len(rows), summary(rows) or "none"))
        shown = sorted((r for r in rows if keep(r)), key=lambda r: (order[r["state"]], -(r.get("idle_days") or 0)))
        width = max([len(r["id"]) for r in shown] or [0])
        for r in shown:
            print("  %-9s %s  %s" % (r["state"], r["id"].ljust(width), fmt(r)))
        if not shown:
            print("  nothing to review")

    emit("SKILLS", rep["skills"], lambda r: "idle %4sd  uses %-3d last %-10s  %s" % (
        r["idle_days"], r["use_count"], day(r["last_used_at"]), "; ".join(r["reasons"])))
    emit("MEMORY PROJECTS", rep["memory_dirs"], lambda r: "%2d files  %s  last session %s  %s" % (
        r["file_count"], tilde(r["project_path"]) or "?", day(r["project_last_active_at"]),
        "; ".join(r["reasons"])))
    emit("MEMORY FILES", rep["memories"], lambda r: "%-9s idle %4sd  %s" % (
        r["type"], r["idle_days"], "; ".join(r["reasons"])))
    if rep["index_issues"]:
        print("\nMEMORY HYGIENE (%d)" % len(rep["index_issues"]))
        for i in rep["index_issues"]:
            print("  %-13s %s  %s" % (i["kind"], i["id"], i["detail"] if i["kind"] == "stray-file"
                                       else "%s: %s" % (i["file"], i["detail"])))
    arc = rep["archive"]
    if arc["purgeable"]:
        print("\nARCHIVE (%d items): %d archived %d+ days ago, run /curator purge to delete them for good"
              % (arc["count"], len(arc["purgeable"]), arc["purge_after_days"]))
    elif arc["count"]:
        print("\nARCHIVE (%d items): none older than %d days" % (arc["count"], arc["purge_after_days"]))


# --- actions -----------------------------------------------------------------

def all_items(led, a):
    rep = build_report(led, a)
    items = {r["id"]: r for r in rep["skills"] + rep["memory_dirs"] + rep["memories"]}
    for i in rep["index_issues"]:
        if i["kind"] == "stray-file":
            items[i["id"]] = {"id": i["id"], "kind": "stray", "path": i["path"], "state": "candidate"}
    return items


def drop_index_lines(index, filename):
    if not index.exists():
        return []
    lines = index.read_text().splitlines(keepends=True)
    hit = [l for l in lines if "](%s)" % filename in l or "](%s#" % filename in l]
    if hit:
        index.write_text("".join(l for l in lines if l not in hit))
    return hit


def archive(ids, led, a):
    items = all_items(led, a)
    rc = 0
    for item_id in ids:
        item = items.get(item_id)
        if item is None:
            print("%s: not found" % item_id)
            rc = 1
            continue
        if item["state"] == "pinned":
            print("%s: pinned, unpin it first" % item_id)
            rc = 1
            continue
        src = Path(item["path"])
        dest = ARCHIVE / ("%s-%s" % (now().strftime("%Y%m%d-%H%M%S"), re.sub(r"[^\w.-]", "_", item_id)))
        dest.mkdir(parents=True)
        manifest = {"id": item_id, "kind": item["kind"], "original_path": str(src),
                    "archived_at": iso(now()), "index_lines": []}
        if item["kind"] in ("memory", "stray"):
            manifest["index_lines"] = drop_index_lines(src.parent / "MEMORY.md", src.name)
        shutil.move(str(src), str(dest / src.name))
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
        print("archived %s -> %s" % (item_id, tilde(str(dest))))
    return rc


def archived_entries():
    out = []
    if ARCHIVE.is_dir():
        for d in sorted(ARCHIVE.iterdir()):
            try:
                m = json.loads((d / "manifest.json").read_text())
            except (OSError, ValueError):
                continue
            m["dir"] = d
            out.append(m)
    return out


def restore(ids):
    entries = archived_entries()
    rc = 0
    for item_id in ids:
        matches = [e for e in entries if e["id"] == item_id]
        if not matches:
            print("%s: not in archive" % item_id)
            rc = 1
            continue
        e = matches[-1]
        dst = Path(e["original_path"])
        if os.path.lexists(dst):
            print("%s: %s already exists, not overwriting" % (item_id, tilde(str(dst))))
            rc = 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(e["dir"] / dst.name), str(dst))
        if e.get("index_lines"):
            index = dst.parent / "MEMORY.md"
            text = index.read_text() if index.exists() else "# Memory\n"
            if not text.endswith("\n"):
                text += "\n"
            index.write_text(text + "".join(l if l.endswith("\n") else l + "\n" for l in e["index_lines"]))
        shutil.rmtree(e["dir"])
        print("restored %s -> %s" % (item_id, tilde(str(dst))))
    return rc


def list_archived():
    entries = archived_entries()
    if not entries:
        print("archive is empty")
    for e in entries:
        print("%-52s archived %s  age %dd" % (e["id"], day(e["archived_at"]), days_since(e["archived_at"])))
    return 0


def purge(ids, older_than, yes):
    entries = archived_entries()
    if ids:
        targets = [e for e in entries if e["id"] in ids]
    elif older_than is not None:
        targets = [e for e in entries if days_since(e["archived_at"]) >= older_than]
    else:
        print("purge needs ids or --older-than N")
        return 2
    if not targets:
        print("nothing to purge")
        return 0
    for e in targets:
        print("  %s (archived %s)" % (e["id"], day(e["archived_at"])))
    if not yes:
        print("dry run: pass --yes to delete these %d archived item(s) permanently" % len(targets))
        return 0
    for e in targets:
        shutil.rmtree(e["dir"])
    print("purged %d item(s)" % len(targets))
    return 0


def fix_index(ids, led, a):
    items = all_items(led, a)
    rc = 0
    for item_id in ids:
        item = items.get(item_id)
        index = Path(item["path"]) / "MEMORY.md" if item and item["kind"] == "memdir" else None
        if index is None or not index.exists():
            print("%s: not a memdir id with a MEMORY.md" % item_id)
            rc = 1
            continue
        missing = [n for n in index_links(index) if not (index.parent / n).exists()]
        lines = index.read_text().splitlines(keepends=True)
        dropped = [l for l in lines if any("](%s)" % n in l or "](%s#" % n in l for n in missing)]
        if dropped:
            DATA.mkdir(parents=True, exist_ok=True)
            backup = DATA / ("MEMORY.md.%s.%s.bak" % (re.sub(r"[^\w.-]", "_", item_id), now().strftime("%Y%m%d-%H%M%S")))
            shutil.copy2(index, backup)
            index.write_text("".join(l for l in lines if l not in dropped))
        print("%s: removed %d dangling line(s)%s" % (item_id, len(dropped),
              " (backup %s)" % tilde(str(backup)) if dropped else ""))
    return rc


def set_pin(ids, pinned):
    with ledger_lock():
        led = load_ledger()
        current = set(led["pinned"])
        current = current | set(ids) if pinned else current - set(ids)
        led["pinned"] = sorted(current)
        save_ledger(led)
    print(("pinned " if pinned else "unpinned ") + ", ".join(ids))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="curator", description=__doc__.splitlines()[0])
    p.add_argument("--stale-days", type=int, default=30)
    p.add_argument("--archive-days", type=int, default=60)
    p.add_argument("--purge-after-days", type=int, default=30, help="age at which report suggests a purge")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="scan new transcript and history lines into the ledger")
    r = sub.add_parser("report", help="show candidates (ingests first)")
    r.add_argument("--json", action="store_true")
    r.add_argument("--all", action="store_true", help="include ok and pinned rows")
    for name, help_text in (("archive", "move items to the archive"), ("restore", "move archived items back"),
                            ("pin", "never flag these items"), ("unpin", "remove a pin"),
                            ("fix-index", "drop MEMORY.md lines that link missing files (memdir ids)")):
        sub.add_parser(name, help=help_text).add_argument("ids", nargs="+")
    sub.add_parser("archived", help="list archived items")
    pg = sub.add_parser("purge", help="permanently delete archived items")
    pg.add_argument("ids", nargs="*")
    pg.add_argument("--older-than", type=int)
    pg.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "ingest":
        ingest(block=False)
        return 0
    if a.cmd in ("report", "archive", "fix-index"):
        led = ingest() or load_ledger()
        if a.cmd == "archive":
            return archive(a.ids, led, a)
        if a.cmd == "fix-index":
            return fix_index(a.ids, led, a)
        rep = build_report(led, a)
        if a.json:
            json.dump(rep, sys.stdout, indent=1)
            print()
        else:
            print_report(rep, a.all)
        return 0
    if a.cmd == "restore":
        return restore(a.ids)
    if a.cmd in ("pin", "unpin"):
        return set_pin(a.ids, a.cmd == "pin")
    if a.cmd == "archived":
        return list_archived()
    if a.cmd == "purge":
        return purge(a.ids, a.older_than, a.yes)
    return 2


if __name__ == "__main__":
    sys.exit(main())
