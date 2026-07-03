#!/usr/bin/env python3
"""
session_tree.py — build a directory tree of your Claude Code sessions.

Claude Code stores every session as a .jsonl file under
    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

This tool reconstructs the *real* working-directory tree (using the `cwd`
recorded inside each session, which is unambiguous — the encoded folder name
is not) and hangs each session off the directory it ran in, as a leaf whose
label is the name you'd see in `/resume`.

Two views:
    * CLI      : a `tree`-style ASCII/Unicode diagram (good for headless/remote)
    * --html F : a self-contained, navigable HTML page (collapse/expand, search,
                 click-to-copy the resume command)

Examples:
    ./session_tree.py                      # print the whole tree
    ./session_tree.py --root ~/lmcache     # only sessions under a path
    ./session_tree.py --sort msgs          # sort sessions by message count
    ./session_tree.py --html tree.html     # write an interactive page
    ./session_tree.py --json               # machine-readable dump
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _first_user_prompt(text_parts):
    """Pick a sensible label from the first real user prompt."""
    for txt in text_parts:
        if not isinstance(txt, str):
            continue
        s = txt.strip()
        if not s:
            continue
        # skip harness/meta noise
        low = s.lower()
        if s.startswith("<") or low.startswith("caveat:") or "system-reminder" in low:
            continue
        # collapse whitespace
        return " ".join(s.split())
    return None


def parse_session(path: Path) -> dict | None:
    """Extract metadata from one session .jsonl file. Returns None if unreadable."""
    cwd = None
    ai_title = None
    custom_title = None
    first_prompt = None
    away_summary = None
    msg_count = 0
    got_first = False

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = o.get("type")
                if cwd is None and o.get("cwd"):
                    cwd = o["cwd"]

                if t == "custom-title":
                    custom_title = o.get("customTitle")
                elif t == "ai-title":
                    ai_title = o.get("aiTitle")
                elif t == "system" and o.get("subtype") == "away_summary":
                    # keep the most recent recap
                    away_summary = o.get("content") or away_summary
                elif t in ("user", "assistant"):
                    if t == "user":
                        msg_count += 1
                    if not got_first and t == "user":
                        content = o.get("message", {}).get("content")
                        parts = []
                        if isinstance(content, str):
                            parts = [content]
                        elif isinstance(content, list):
                            for p in content:
                                if isinstance(p, dict) and p.get("type") == "text":
                                    parts.append(p.get("text", ""))
                        fp = _first_user_prompt(parts)
                        if fp:
                            first_prompt = fp
                            got_first = True
    except OSError:
        return None

    if away_summary:
        # drop the trailing "(disable recaps in /config)" hint Claude Code appends
        away_summary = re.sub(r"\s*\(disable recaps in /config\)\s*$", "", away_summary).strip()

    uuid = path.stem
    title = custom_title or ai_title or first_prompt or f"(untitled) {uuid[:8]}"
    label_source = (
        "custom" if custom_title else
        "ai" if ai_title else
        "prompt" if first_prompt else
        "none"
    )

    try:
        mtime = path.stat().st_mtime
        size = path.stat().st_size
    except OSError:
        mtime = 0.0
        size = 0

    return {
        "uuid": uuid,
        "title": title,
        "title_source": label_source,
        "summary": away_summary,
        "cwd": cwd or "(unknown)",
        "mtime": mtime,
        "size": size,
        "msgs": msg_count,
        "file": str(path),
    }


def collect_sessions(projects_dir: Path) -> list[dict]:
    sessions = []
    if not projects_dir.is_dir():
        return sessions
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            s = parse_session(f)
            if s:
                sessions.append(s)
    return sessions


# --------------------------------------------------------------------------- #
# Tree building (keyed on real cwd path components)
# --------------------------------------------------------------------------- #
def build_tree(sessions: list[dict]) -> dict:
    """
    Node = {"name", "path", "children": {name: node}, "sessions": [session,...]}.
    Returns a single root node (common ancestor collapsed for readability).
    """
    root = {"name": "", "path": "/", "children": {}, "sessions": []}

    for s in sessions:
        cwd = s["cwd"]
        if cwd == "(unknown)":
            node = root["children"].setdefault(
                "(unknown)",
                {"name": "(unknown)", "path": "(unknown)", "children": {}, "sessions": []},
            )
            node["sessions"].append(s)
            continue
        parts = [p for p in cwd.split(os.sep) if p]
        node = root
        acc = ""
        for part in parts:
            acc = acc + os.sep + part
            node = node["children"].setdefault(
                part, {"name": part, "path": acc, "children": {}, "sessions": []}
            )
        node["sessions"].append(s)
    return root


def collapse_single_child(node: dict) -> dict:
    """Collapse chains of directories that have exactly one child and no sessions
    (e.g. /home/rick -> shown as one segment) for a tighter tree."""
    # recurse first
    for k, child in node["children"].items():
        node["children"][k] = collapse_single_child(child)

    while (
        len(node["children"]) == 1
        and not node["sessions"]
        and node["path"] != "/"
    ):
        (only,) = node["children"].values()
        merged_name = (node["name"] + os.sep + only["name"]).lstrip(os.sep)
        only["name"] = merged_name
        return only
    return node


def delete_session(sessions: list[dict], uuid: str, assume_yes: bool) -> int:
    """Delete the session whose uuid matches `uuid` (full or unique prefix).
    Removes the .jsonl file and rmdirs its project directory if left empty."""
    matches = [s for s in sessions if s["uuid"] == uuid]
    if not matches:  # fall back to prefix match
        matches = [s for s in sessions if s["uuid"].startswith(uuid)]

    if not matches:
        print(f"No session matching '{uuid}'.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"'{uuid}' is ambiguous — matches {len(matches)} sessions:", file=sys.stderr)
        for s in matches:
            print(f"  {s['uuid']}  {truncate(s['title'], 60)}", file=sys.stderr)
        return 1

    s = matches[0]
    path = Path(s["file"])
    print(f"About to delete:\n  {path}\n  title: {truncate(s['title'], 70)}"
          f"\n  {fmt_date(s['mtime'])} · {s['msgs']} msgs")

    if not assume_yes:
        if not sys.stdin.isatty():
            print("Refusing to delete without confirmation; pass --yes for non-interactive use.",
                  file=sys.stderr)
            return 1
        try:
            ans = input("Delete this session? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    try:
        path.unlink()
    except OSError as e:
        print(f"Failed to delete {path}: {e}", file=sys.stderr)
        return 1
    print(f"Deleted {path.name}")

    proj = path.parent
    try:
        if not any(proj.iterdir()):
            proj.rmdir()
            print(f"Removed empty project directory {proj}")
        elif not any(proj.glob("*.jsonl")):
            print(f"Note: {proj} has no sessions left but still contains other files; "
                  f"left in place.")
    except OSError as e:
        print(f"Could not remove directory {proj}: {e}", file=sys.stderr)
    return 0


def sort_sessions(sessions: list[dict], key: str) -> list[dict]:
    if key == "msgs":
        return sorted(sessions, key=lambda s: -s["msgs"])
    if key == "title":
        return sorted(sessions, key=lambda s: s["title"].lower())
    if key == "size":
        return sorted(sessions, key=lambda s: -s["size"])
    # default: recent first
    return sorted(sessions, key=lambda s: -s["mtime"])


# --------------------------------------------------------------------------- #
# CLI rendering
# --------------------------------------------------------------------------- #
class Style:
    def __init__(self, color: bool):
        self.color = color

    def c(self, code, s):
        if not self.color:
            return s
        return f"\033[{code}m{s}\033[0m"

    dir = lambda self, s: self.c("1;34", s)      # bold blue
    title = lambda self, s: self.c("0", s)
    meta = lambda self, s: self.c("2;37", s)     # dim
    bullet = lambda self, s: self.c("33", s)     # yellow


def fmt_date(mtime: float) -> str:
    if not mtime:
        return "????-??-??"
    return datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_cli(node, st: Style, sort_key: str, max_title: int, show_summary=False,
               summary_width=80, prefix="", is_root=True, out=None):
    out = out if out is not None else []

    if is_root:
        label = node["name"] or node["path"]
        out.append(st.dir(label if label else "/"))

    children = list(node["children"].values())
    children.sort(key=lambda n: n["name"].lower())
    sessions = sort_sessions(node["sessions"], sort_key)

    entries = [("dir", c) for c in children] + [("sess", s) for s in sessions]

    for i, (kind, item) in enumerate(entries):
        last = i == len(entries) - 1
        branch = "└── " if last else "├── "
        child_prefix = prefix + ("    " if last else "│   ")

        if kind == "dir":
            out.append(prefix + branch + st.dir(item["name"] + "/"))
            render_cli(item, st, sort_key, max_title, show_summary, summary_width,
                       child_prefix, is_root=False, out=out)
        else:
            s = item
            src = {"custom": "★", "ai": "·", "prompt": "»", "none": "?"}[s["title_source"]]
            meta = st.meta(f"  [{fmt_date(s['mtime'])} · {s['msgs']} msgs · {s['uuid'][:8]}]")
            out.append(
                prefix + branch + st.bullet(src + " ")
                + st.title(truncate(s["title"], max_title)) + meta
            )
            if show_summary and s.get("summary"):
                wrapped = textwrap.wrap(s["summary"], width=summary_width) or [""]
                for j, wl in enumerate(wrapped):
                    out.append(child_prefix + st.meta(("↳ " if j == 0 else "  ") + wl))
    return out


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
def node_to_jsonable(node, sort_key):
    children = sorted(node["children"].values(), key=lambda n: n["name"].lower())
    sessions = sort_sessions(node["sessions"], sort_key)
    return {
        "name": node["name"] or "/",
        "path": node["path"],
        "children": [node_to_jsonable(c, sort_key) for c in children],
        "sessions": [
            {
                "title": s["title"],
                "source": s["title_source"],
                "uuid": s["uuid"],
                "date": fmt_date(s["mtime"]),
                "msgs": s["msgs"],
                "cwd": s["cwd"],
                "summary": s.get("summary") or "",
            }
            for s in sessions
        ],
    }


HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code Session Tree</title>
<style>
:root{
  --bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--muted:#8b949e;
  --dir:#58a6ff;--accent:#d29922;--custom:#a371f7;--hit:#f0883e;
}
@media (prefers-color-scheme: light){
  :root{--bg:#ffffff;--panel:#f6f8fa;--line:#d0d7de;--fg:#1f2328;--muted:#656d76;
        --dir:#0969da;--accent:#9a6700;--custom:#8250df;--hit:#bc4c00;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
header{position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--line);
  padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:5}
header h1{font-size:15px;margin:0;font-family:system-ui,sans-serif}
.stats{color:var(--muted);font-size:12px}
input[type=search]{flex:1;min-width:180px;background:var(--bg);border:1px solid var(--line);
  color:var(--fg);padding:6px 10px;border-radius:6px;font:inherit}
button{background:var(--bg);border:1px solid var(--line);color:var(--fg);
  padding:6px 10px;border-radius:6px;cursor:pointer;font:inherit}
button:hover{border-color:var(--dir)}
select{background:var(--bg);border:1px solid var(--line);color:var(--fg);
  padding:6px 8px;border-radius:6px;font:inherit}
#tree{padding:12px 16px 60px}
ul{list-style:none;margin:0;padding-left:20px;border-left:1px dotted var(--line)}
li{margin:1px 0}
.row{display:flex;align-items:baseline;gap:6px;padding:1px 4px;border-radius:5px}
.row:hover{background:var(--panel)}
.tog{cursor:pointer;user-select:none;color:var(--muted);width:1em;display:inline-block}
.dir>.row .name{color:var(--dir);font-weight:600;cursor:pointer}
.sess .name{color:var(--fg)}
.badge{font-size:10px;padding:0 5px;border-radius:8px;border:1px solid var(--line);color:var(--muted)}
.src-custom{color:var(--custom)}
.src-ai{color:var(--accent)}
.src-prompt{color:var(--muted)}
.meta{color:var(--muted);font-size:11px;white-space:nowrap}
.recap{color:var(--muted);font-size:12px;font-style:italic;padding:0 4px 2px 26px;
  max-width:70ch;opacity:.85}
body.no-recaps .recap{display:none}
.uuid{cursor:pointer;text-decoration:underline dotted}
.hidden{display:none}
mark{background:var(--hit);color:#000;border-radius:2px}
.collapsed>ul{display:none}
.count{color:var(--muted);font-size:11px}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);
  background:var(--custom);color:#fff;padding:8px 16px;border-radius:8px;
  opacity:0;transition:opacity .2s;pointer-events:none;font-family:system-ui,sans-serif}
#toast.show{opacity:1}
</style></head><body>
<header>
  <h1>🌳 Claude Code Sessions</h1>
  <span class="stats" id="stats"></span>
  <input type="search" id="q" placeholder="filter sessions… (title / path / uuid)">
  <select id="sort" title="sort sessions">
    <option value="recent">recent</option>
    <option value="msgs">most msgs</option>
    <option value="title">title A→Z</option>
  </select>
  <button id="recaps">hide recaps</button>
  <button id="expand">expand all</button>
  <button id="collapse">collapse all</button>
</header>
<div id="tree"></div>
<div id="toast">copied!</div>
<script>
const DATA = __DATA__;
const SRC_LABEL = {custom:"★ custom",ai:"· ai",prompt:"» prompt",none:"? untitled"};
let sortKey = "recent";

function sortSessions(arr){
  const a=[...arr];
  if(sortKey==="msgs") a.sort((x,y)=>y.msgs-x.msgs);
  else if(sortKey==="title") a.sort((x,y)=>x.title.toLowerCase().localeCompare(y.title.toLowerCase()));
  // "recent" = server order (already recent-first)
  return a;
}

function el(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e;}

function renderNode(node){
  const li=el("li","dir");
  const row=el("div","row");
  const tog=el("span","tog","▾");
  const name=el("span","name",node.name.replace(/\\/$/,"")+"/");
  const total=countSessions(node);
  const cnt=el("span","count"," ("+total+")");
  row.append(tog,name,cnt);
  li.append(row);
  const ul=el("ul");
  node.children.forEach(c=>ul.append(renderNode(c)));
  sortSessions(node.sessions).forEach(s=>ul.append(renderSession(s,node.path)));
  li.append(ul);
  const toggle=()=>li.classList.toggle("collapsed");
  tog.onclick=toggle; name.onclick=toggle;
  return li;
}

function countSessions(node){
  let n=node.sessions.length;
  node.children.forEach(c=>n+=countSessions(c));
  return n;
}

function renderSession(s,path){
  const li=el("li","sess");
  li.dataset.search=(s.title+" "+s.uuid+" "+s.cwd+" "+(s.summary||"")).toLowerCase();
  const row=el("div","row");
  const src=el("span","src-"+s.source,({custom:"★",ai:"·",prompt:"»",none:"?"})[s.source]);
  const name=el("span","name",s.title);
  const meta=el("span","meta");
  meta.append(" "+s.date+" · "+s.msgs+" msgs · ");
  const uuid=el("span","uuid",s.uuid.slice(0,8));
  uuid.title="click to copy: claude --resume "+s.uuid;
  uuid.onclick=()=>copy("claude --resume "+s.uuid);
  meta.append(uuid);
  row.append(src,name,meta);
  li.append(row);
  if(s.summary){
    const rec=el("div","recap","↳ "+s.summary);
    li.append(rec);
  }
  return li;
}

function copy(t){
  navigator.clipboard.writeText(t).then(()=>{
    const toast=document.getElementById("toast");
    toast.textContent="copied: "+t;
    toast.classList.add("show");
    setTimeout(()=>toast.classList.remove("show"),1400);
  });
}

function build(){
  const tree=document.getElementById("tree");
  tree.innerHTML="";
  const rootUl=el("ul");
  (DATA.children.length?DATA.children:[DATA]).forEach(c=>rootUl.append(renderNode(c)));
  DATA.sessions && sortSessions(DATA.sessions).forEach(s=>rootUl.append(renderSession(s,DATA.path)));
  tree.append(rootUl);
  let total=countSessions(DATA);
  document.getElementById("stats").textContent=total+" sessions";
}

function filter(){
  const q=document.getElementById("q").value.trim().toLowerCase();
  document.querySelectorAll("#tree li.sess").forEach(li=>{
    const hit=!q||li.dataset.search.includes(q);
    li.classList.toggle("hidden",!hit);
    const nameEl=li.querySelector(".name");
    const t=nameEl.textContent;
    if(q&&hit){
      const i=t.toLowerCase().indexOf(q);
      if(i>=0){nameEl.innerHTML="";nameEl.append(t.slice(0,i));
        const m=el("mark",null,t.slice(i,i+q.length));nameEl.append(m);nameEl.append(t.slice(i+q.length));}
    } else { nameEl.textContent=t; }
  });
  // hide empty dirs when filtering
  document.querySelectorAll("#tree li.dir").forEach(li=>{
    const visible=li.querySelector("li.sess:not(.hidden)");
    li.classList.toggle("hidden",!!q&&!visible);
    if(q&&visible) li.classList.remove("collapsed");
  });
}

document.getElementById("q").addEventListener("input",filter);
document.getElementById("sort").addEventListener("change",e=>{sortKey=e.target.value;build();filter();});
document.getElementById("recaps").onclick=e=>{
  const off=document.body.classList.toggle("no-recaps");
  e.target.textContent=off?"show recaps":"hide recaps";
};
document.getElementById("expand").onclick=()=>document.querySelectorAll("#tree li.dir").forEach(li=>li.classList.remove("collapsed"));
document.getElementById("collapse").onclick=()=>document.querySelectorAll("#tree li.dir").forEach(li=>li.classList.add("collapsed"));
build();
</script></body></html>
"""


def render_html(root_jsonable, show_summary=True) -> str:
    html_out = HTML_TEMPLATE.replace("__DATA__", json.dumps(root_jsonable))
    if not show_summary:
        html_out = html_out.replace("<body>", '<body class="no-recaps">')
        html_out = html_out.replace('id="recaps">hide recaps', 'id="recaps">show recaps')
    return html_out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Tree of Claude Code sessions.")
    ap.add_argument("--projects-dir", type=Path, default=PROJECTS_DIR,
                    help=f"Claude projects dir (default: {PROJECTS_DIR})")
    ap.add_argument("--root", type=str, default=None,
                    help="Only include sessions whose cwd is under this path.")
    ap.add_argument("--sort", choices=["recent", "msgs", "title", "size"], default="recent",
                    help="Order sessions within each directory (default: recent).")
    ap.add_argument("--html", metavar="FILE", type=Path, default=None,
                    help="Write an interactive HTML page instead of CLI output.")
    ap.add_argument("--json", action="store_true", help="Dump raw session metadata as JSON.")
    ap.add_argument("--delete", metavar="UUID", default=None,
                    help="Delete the session with this uuid (full or unique prefix), "
                         "then rmdir its project dir if empty. Prompts unless --yes.")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Skip the confirmation prompt for --delete.")
    ap.add_argument("--no-collapse", action="store_true",
                    help="Do not collapse single-child directory chains.")
    ap.add_argument("-s", "--summary", action="store_true",
                    help="Show each session's idle recap (away_summary) under its title.")
    ap.add_argument("--max-title", type=int, default=70, help="Truncate titles (CLI) to N chars.")
    ap.add_argument("--no-color", action="store_true", help="Disable ANSI colors in CLI output.")
    args = ap.parse_args(argv)

    sessions = collect_sessions(args.projects_dir)

    if args.delete:
        return delete_session(sessions, args.delete, args.yes)

    if args.root:
        root_abs = os.path.abspath(os.path.expanduser(args.root))
        sessions = [s for s in sessions if os.path.abspath(s["cwd"]).startswith(root_abs)]

    if not sessions:
        print(f"No sessions found under {args.projects_dir}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(sessions, sys.stdout, indent=2)
        print()
        return 0

    tree = build_tree(sessions)
    if not args.no_collapse:
        tree = collapse_single_child(tree)

    if args.html:
        payload = node_to_jsonable(tree, args.sort)
        args.html.write_text(render_html(payload, show_summary=args.summary), encoding="utf-8")
        print(f"Wrote {args.html}  ({len(sessions)} sessions). Open it in a browser.")
        return 0

    color = sys.stdout.isatty() and not args.no_color
    st = Style(color)
    lines = render_cli(tree, st, args.sort, args.max_title, show_summary=args.summary)
    print("\n".join(lines))
    print()
    print(st.meta(f"{len(sessions)} sessions · ★ custom title  · ai-title  » first prompt  ? untitled"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
