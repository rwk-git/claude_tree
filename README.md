# session_tree.py

Build a directory tree of your Claude Code sessions. Each session is stored as
`~/.claude/projects/*/*.jsonl`; this tool reconstructs the **real** working-directory
tree (from the `cwd` inside each file) and hangs every session off it as a leaf,
labeled exactly as `/resume` shows it.

Leaf markers: `★` custom title · `·` AI title · `»` first prompt · `?` untitled.

Pass `-s`/`--summary` to also show each session's idle recap (the `away_summary`
Claude Code generates when you step away) as a dimmed `↳` line under the title.

## Usage

```sh
python3 session_tree.py                  # whole tree (recent-first), CLI view
python3 session_tree.py --root ~/lmcache # only sessions under a path
python3 session_tree.py --summary        # add the idle recap under each title
python3 session_tree.py --sort msgs      # order by message count (or: recent, title, size)
python3 session_tree.py --html tree.html # write an interactive HTML page, then open it
python3 session_tree.py --json           # machine-readable dump
python3 session_tree.py --delete <uuid>  # delete a session (full or short uuid); -y to skip prompt
```

`--delete` removes the session's `.jsonl` file (prompting first, unless `-y`/`--yes`)
and `rmdir`s its project directory if that was the last session in it.

CLI leaves show `title [date · N msgs · uuid8]`, colored when writing to a terminal.
The HTML view is a single self-contained file (offline, light/dark): collapsible
dirs, live search, sort, expand/collapse-all, and click-a-uuid to copy
`claude --resume <id>`.

No dependencies beyond Python 3. Respects `$CLAUDE_CONFIG_DIR` (default `~/.claude`).
Run `python3 session_tree.py --help` for all options.
