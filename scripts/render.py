#!/usr/bin/env python3
"""Render one code-performance dashboard page sized to the real terminal window.

Usage: render.py <facts.json> [--page overview|proposals|show|hotpaths|specialists|rejected|leads]
                 [--n 2] [--filter TEXT] [--rows R --cols C]
Reads notes*.json (judgement), scout.json + verified/*.json (deep mode) and applied/*.json.
`--page show --filter io1` shows one proposal in full.
Prints a fenced ```text block that is exactly as tall as the window allows.
"""
import argparse
import json
import os
import subprocess
import unicodedata
from pathlib import Path

# Lines Claude Code itself uses around a reply. Override with CODE_PERF_RESERVE.
RESERVE = int(os.environ.get("CODE_PERF_RESERVE", "10"))
VERDICTS = ("confirmed", "unsure", "rejected")
RUNS = {"hot": "very often", "warm": "often", "cold": "rarely", "unknown": "?"}
WORK = {"S": "small", "M": "medium", "L": "large"}
MAX_WIDTH = 130  # wider pages stretch the columns apart
GAIN = {"high": 3, "med": 2, "low": 1}
HOT = {"hot": 3, "warm": 2, "unknown": 1.5, "cold": 1}
EFFORT = {"S": 1, "M": 2, "L": 3}
PAGES = ["overview", "proposals", "show", "hotpaths", "specialists", "rejected", "leads"]
LEAD_WORD = {"proposed": "route", "dismissed": "harmless", "open": "not checked"}


def term_size():
    """The Bash tool has no tty, so find the tty of the parent claude process."""
    if os.environ.get("CODE_PERF_ROWS"):
        return int(os.environ["CODE_PERF_ROWS"]), int(os.environ.get("CODE_PERF_COLS", "100"))
    pid = os.getpid()
    for _ in range(8):
        try:
            out = subprocess.run(["ps", "-o", "tty=,ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
        except OSError:
            break
        if len(out) < 2:
            break
        tty, ppid = out[0], out[1]
        if tty not in ("?", ""):
            size = subprocess.run(["stty", "-F", f"/dev/{tty}", "size"], capture_output=True, text=True).stdout.split()
            if len(size) == 2:
                return int(size[0]), int(size[1])
        pid = int(ppid)
    return 24, 80


def width(s):
    w = 0
    for ch in s:
        if unicodedata.combining(ch) or ch == "️":
            continue
        w += 2 if unicodedata.east_asian_width(ch) in "WF" or ord(ch) >= 0x1F300 or ch in "✅❔❌" else 1
    return w


def fit(s, w):
    """Cut s to display width w, marking the cut with …"""
    if width(s) <= w:
        return s
    out = ""
    for ch in s:
        if width(out + ch) > w - 1:
            break
        out += ch
    return out + "…"


def lfit(path, w):
    """Cut a path from the left so the file name survives."""
    return path if width(path) <= w else "…" + path[-(w - 1):]


def pad(s, w):
    s = fit(s, w)
    return s + " " * (w - width(s))


class Page:
    def __init__(self, rows, cols):
        self.h = max(8, rows - RESERVE)
        self.w = max(50, min(cols - 4, MAX_WIDTH))
        self.lines = []

    def add(self, s=""):
        self.lines.append(fit(s, self.w))

    def rule(self):
        self.add("─" * self.w)

    def left(self, footer_lines=1):
        return self.h - len(self.lines) - footer_lines

    def listing(self, items, fmt, n, footer_lines=1, page_cmd=""):
        """Add as many items as fit; say how many are left and how to see them."""
        room = max(self.left(footer_lines + 1), 1)
        start = (n - 1) * room
        chunk = items[start:start + room]
        for i, it in enumerate(chunk, start + 1):
            self.add(fmt(i, it))
        rest = len(items) - start - len(chunk)
        if rest > 0:
            self.add(f"  +{rest} more → '{page_cmd} {n + 1}'")
        return chunk

    def render(self):
        body = self.lines
        if len(body) > self.h:  # cut the middle, never the last line: it says what to type next
            body = body[:self.h - 1] + body[-1:]
        return "```text\n" + "\n".join(body) + "\n```"


def load(facts_path):
    """facts + the merged review + every proposal (filled from its lead, with applied results)."""
    d = Path(facts_path).parent
    facts = json.loads(Path(facts_path).read_text())
    bad = []

    def read(p):
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            bad.append(p.name)
            return None

    # notes*.json = the judgement pass (main session or up to 3 subagents); scout.json = deep mode.
    review = {"summary": [], "hot_paths": [], "skipped": [], "dismiss": {}}
    specs, props = {}, []
    for p in [d / "scout.json", *sorted(d.glob("notes*.json"))]:
        v = read(p) if p.exists() else None
        if not v:
            continue
        if v.get("summary"):
            review["summary"].append(v["summary"])
        review["hot_paths"] += v.get("hot_paths", [])
        review["skipped"] += v.get("skipped", [])
        review["dismiss"].update(v.get("dismiss", {}))
        props += v.get("proposals", [])
    for p in sorted((d / "verified").glob("*.json")):  # deep mode
        v = read(p)
        if v:
            specs[v.get("specialist", p.stem)] = v
            props += [{**x, "specialist": x.get("specialist", v.get("specialist", p.stem))}
                      for x in v.get("proposals", [])]
    applied = {}
    for p in sorted((d / "applied").glob("*.json")):
        a = read(p)
        if a:
            applied[a.get("id", p.stem)] = a
    leads = {g["id"]: g for g in facts["leads"]}
    seen, merged = set(), []
    for x in props:
        if x.get("id") in seen:
            continue
        seen.add(x.get("id"))
        g = leads.get(x.get("id"))
        if g:  # a proposal on a lead only needs what differs from the lead's template
            x.setdefault("where", f"{g['file']}:{g['lines'][0]}")
            x.setdefault("now", facts["rules"].get(g["rule"], {}).get("cost", g["rule"]))
            x.setdefault("change", g.get("route", ""))
            x.setdefault("specialist", g["specialist"])
            x.setdefault("hot", g["heat"])
        x.setdefault("specialist", "other")
        x["applied"] = applied.get(x.get("id"))
        merged.append(x)
    for g in facts["leads"]:
        note = review["dismiss"].get(g["id"]) or review["dismiss"].get(g["rule"])
        g["status"] = "proposed" if g["id"] in seen else "dismissed" if note else "open"
        g["note"] = note or ""
    merged.sort(key=score, reverse=True)
    review["summary"] = " · ".join(review["summary"])
    return facts, review, specs, merged, bad


def score(x):
    """Rank: verdict first, then gain × how hot ÷ effort."""
    order = {"confirmed": 2, "unsure": 1}.get(x.get("verdict"), 0)
    return (order, GAIN.get(x.get("gain_class"), 1) * HOT.get(x.get("hot"), 1.5) / EFFORT.get(x.get("effort"), 2))


def head(p, facts, title):
    inv = facts["inventory"]
    mode = f"only: {facts['target']}" if facts.get("target") else "whole scope"
    name = Path(facts["root"]).name + ("" if facts["scope"] == "." else f"/{facts['scope']}")
    p.add(f"{title} · {name} · {inv.get('files', '?')} files · {mode} · {facts['scanned_at'][:16].replace('T', ' ')}")
    p.rule()


def tag(x):
    """Words only for what needs attention; a fully checked route gets no mark."""
    a = x.get("applied")
    if a:
        return "applied" if a.get("status") == "kept" else "undone"
    return {"unsure": "unsure", "rejected": "ruled out"}.get(x.get("verdict"), "")


def gain_text(x):
    """The best number we have: timed on the real code after apply, else the safe-copy timing, else the guess."""
    a = x.get("applied") or {}
    if a.get("status") == "kept" and a.get("bench_before"):
        return f"{a['bench_before'].split(' ±')[0]}→{a['bench_after'].split(' ±')[0]} timed"
    m = x.get("measured") or {}
    return f"{m['before']}→{m['after']} timed" if m.get("before") else x.get("gain", "")


# Column widths: runs, work, gain, where. The title takes what is left.
COLS = (10, 6, 23, 26)


def ranks(props):
    """Proposal id -> the number the user sees and types (1..n, best first). Ids stay internal."""
    return {x.get("id"): i for i, x in enumerate(live(props), 1)}


def find(props, key):
    """A number from the dashboard (or an internal id) -> (number, proposal)."""
    rows = live(props)
    if key.isdigit() and 1 <= int(key) <= len(rows):
        return int(key), rows[int(key) - 1]
    for i, x in enumerate(rows, 1):
        if x.get("id", "").lower() == key.lower():
            return i, x
    return None, None


def prop_header(p):
    title_w = p.w - 5 - sum(COLS) - len(COLS)
    return ("     " + pad("what gets faster", title_w) + " " + pad("runs", COLS[0]) + " "
            + pad("work", COLS[1]) + " " + pad("gain", COLS[2]) + " where")


def prop_line(p, x, i):
    title = x.get("title", "") + (f" ({tag(x)})" if tag(x) else "")
    title_w = p.w - 5 - sum(COLS) - len(COLS)
    return (f"{i:>3}. " + pad(title, title_w) + " " + pad(RUNS.get(x.get("hot"), "?"), COLS[0])
            + " " + pad(WORK.get(x.get("effort"), "?"), COLS[1]) + " " + pad(gain_text(x), COLS[2]) + " "
            + lfit(x.get("where", ""), COLS[3]))


def live(props):
    return [x for x in props if x.get("verdict") != "rejected"]


def matches(x, flt):
    flt = flt.lower()
    return not flt or any(flt in str(x.get(k, "")).lower() for k in ("id", "specialist", "title", "where", "hot"))


def page_overview(p, facts, scout, specs, props, n, flt):
    head(p, facts, "PERFORMANCE")
    if scout["summary"]:
        p.add(f"WHERE TIME GOES  {scout['summary']}")
    c = {k: sum(1 for x in props if x.get("verdict") == k) for k in VERDICTS}
    applied = sum(1 for x in props if x.get("applied"))
    measured = sum(1 for x in props if x.get("measured"))
    extra = [f"{c['unsure']} unsure" if c["unsure"] else "", f"{c['rejected']} ruled out" if c["rejected"] else "",
             f"{measured} timed" if measured else "", f"{applied} applied" if applied else ""]
    p.add(f"FASTER ROUTES    {len(live(props))} found, same results" + "".join(f" · {e}" for e in extra if e))
    st = {k: sum(1 for g in facts["leads"] if g["status"] == k) for k in LEAD_WORD}
    p.add(f"SPOTS CHECKED    {len(facts['leads'])} suspicious patterns: {st['proposed']} became {'a route' if st['proposed'] == 1 else 'routes'} · "
          f"{st['dismissed']} harmless" + (f" · {st['open']} not checked yet" if st["open"] else "")
          + (f" · {facts['lead_note']}" if facts.get("lead_note") else ""))
    if not props and not st["dismissed"]:
        p.add("                 not judged yet: SKILL.md step 3 writes notes.json")
    p.rule()
    p.add(prop_header(p))
    rows = live(props)
    p.listing(rows, lambda i, x: prop_line(p, x, i), n, footer_lines=3, page_cmd="overview")
    p.rule()
    todo = [i for i, x in enumerate(rows, 1) if not x.get("applied")]
    p.add("timed = measured, others are estimates · unsure = one thing not verified yet")
    if todo:
        p.add(f"next: show {todo[0]} (details) · apply {todo[0]} (make the change and test it) · "
              "leads (all spots) · hotpaths")
    else:
        p.add("next: show 1 (details) · leads (all spots) · hotpaths" if rows else "next: leads (all spots)")


def page_proposals(p, facts, scout, specs, props, n, flt):
    head(p, facts, "PROPOSALS")
    rows = [x for x in live(props) if matches(x, flt)]
    p.add(f"{len(rows)} live proposals{f' matching {flt!r}' if flt else ''} · ranked by verdict, gain × hot ÷ effort")
    p.add(prop_header(p))
    num = ranks(props)
    p.listing(rows, lambda i, x: prop_line(p, x, num[x.get("id")]), n, footer_lines=2,
              page_cmd=f"proposals {flt}".strip())
    p.rule()
    p.add("next: show <n> · apply <n> · proposals <specialist|file> · overview")


def page_show(p, facts, scout, specs, props, n, flt):
    num, x = find(props, flt)
    head(p, facts, f"PROPOSAL {num or flt}")
    if not x:
        p.add(f"no proposal number {flt!r}; the overview numbers them 1–{len(live(props))}")
        return
    p.add(x.get("title", "") + (f"  ({tag(x)})" if tag(x) else ""))
    p.add(f"where     {x.get('where', '')}   (runs {RUNS.get(x.get('hot'), '?')} · "
          f"{WORK.get(x.get('effort'), '?')} change)")
    p.rule()
    for label, key in (("now", "now"), ("faster", "change"), ("same as", "why_same"), ("gain", "gain"),
                       ("risk", "risk")):
        p.add(f"{label:<10}{x.get(key, '')}")
    p.add(f"{'note':<10}{x.get('verify_note') or x.get('note', '')}")
    m = x.get("measured")
    a = x.get("applied")
    if m or not a:  # after apply, the applied block below holds the timing
        p.add(f"{'measured':<10}" + (f"{m.get('before')} → {m.get('after')}  ({m.get('how', '')})" if m
                                     else "not measured"))
    if a:
        p.rule()
        p.add(f"applied   {a.get('status')} · tests {a.get('tests_before')} → {a.get('tests_after')}")
        p.add(f"bench     {a.get('bench_before')} → {a.get('bench_after')}")
        p.add(f"note      {a.get('note', '')}")
    if x.get("smells"):
        p.add(f"{'covers':<10}{', '.join(x['smells'])}")
    p.rule()
    if a:
        p.add("next: overview · leads (all spots)")
    else:
        p.add(f"next: apply {num} (make the change and test it) · overview")


def page_hotpaths(p, facts, scout, specs, props, n, flt):
    head(p, facts, "HOT PATHS")
    if not scout["hot_paths"]:
        p.add("no hot paths recorded yet (SKILL.md step 3)")
    for h in scout["hot_paths"]:
        if p.left(4) < 3:
            p.add("  … more: 'hotpaths 2' not paged, see scout.json")
            break
        p.add(f"▶ {h['name']} · {h['entry']} · {h['why_hot']}")
        for s in h.get("steps", [])[:max(1, p.left(4) - 1)]:
            p.add(f"    → {s}")
    p.rule()
    skipped = "; ".join(f"{s['specialist']}: {s['reason']}" for s in scout["skipped"])
    p.add(f"skipped: {skipped or 'none'}")
    p.add("next: proposals · specialists · overview")


def page_specialists(p, facts, scout, specs, props, n, flt):
    head(p, facts, "SPECIALISTS")
    p.add(f"{'':<8}{pad('area', 34)} {'leads':>5} {'open':>5}  proposals")
    for key, name in facts["specialists"].items():
        leads = [g for g in facts["leads"] if g["specialist"] == key]
        mine = [x for x in props if x.get("specialist") == key]
        c = {k: sum(1 for x in mine if x.get("verdict") == k) for k in VERDICTS}
        opened = sum(1 for g in leads if g["status"] == "open")
        p.add(f"{key:<8}{pad(name, 34)} {len(leads):>5} {opened:>5}  {c['confirmed'] + c['unsure']} routes"
              + (f", {c['rejected']} ruled out" if c["rejected"] else ""))
    if flt and flt in specs:  # deep mode keeps what each specialist checked
        p.rule()
        p.add(f"{flt} checked:")
        p.listing(specs[flt].get("searched", []), lambda i, s: f"  {i}. {s}", n, footer_lines=2,
                  page_cmd=f"specialists {flt}")
    p.rule()
    p.add("next: proposals <key> · leads <key> · overview")


def page_rejected(p, facts, scout, specs, props, n, flt):
    head(p, facts, "REJECTED")
    rows = [x for x in props if x.get("verdict") == "rejected" and matches(x, flt)]
    p.add(f"{len(rows)} proposals the verifier refuted (not the same behaviour, or no real gain)")
    p.listing(rows, lambda i, x: f"{i:>2}. {pad(x.get('title', ''), 44)} "
                                 f"{x.get('verify_note', '')}", n, footer_lines=2, page_cmd="rejected")
    p.rule()
    p.add("next: proposals · overview")


def page_leads(p, facts, scout, specs, props, n, flt):
    head(p, facts, "SUSPICIOUS SPOTS (ast-grep)")
    f = flt.lower()
    rows = [g for g in facts["leads"]
            if not f or f in " ".join([g["rule"], g["specialist"], g["file"], g["status"], g["heat"]]).lower()]
    p.add(f"{len(rows)} suspicious spots{f' matching {flt!r}' if flt else ''} · each became a route, is harmless "
          "(reason shown), or is not checked yet")
    num = ranks(props)

    def fmt(i, g):
        where = lfit(f"{g['file']}:{g['lines'][0]}", 30)
        word = f"route {num[g['id']]}" if g["id"] in num else LEAD_WORD[g["status"]]
        left = f"{pad(word, 11)} {pad(g['rule'].replace('-', ' '), 16)} {where:<30} "
        return left + fit(g["note"] or f"{g['fn']}()" + (f" ×{len(g['lines'])}" if len(g["lines"]) > 1 else ""),
                          max(10, p.w - width(left)))
    p.listing(rows, fmt, n, footer_lines=2, page_cmd=f"leads {flt}".strip())
    p.rule()
    p.add("next: leads <rule|specialist|open|file> · proposals · overview")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("facts")
    ap.add_argument("--page", default="overview", choices=PAGES)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--filter", default="")
    ap.add_argument("--rows", type=int)
    ap.add_argument("--cols", type=int)
    ap.add_argument("--resolve", metavar="N", help="print the internal id of proposal number N (for apply)")
    a = ap.parse_args()
    if a.resolve:
        num, x = find(load(a.facts)[3], a.resolve)
        print(x["id"] if x else f"no proposal number {a.resolve}")
        return
    rows, cols = term_size()
    p = Page(a.rows or rows, a.cols or cols)
    facts, scout, specs, props, bad = load(a.facts)
    globals()[f"page_{a.page}"](p, facts, scout, specs, props, a.n, a.filter)
    if bad:
        p.lines.insert(2, f"⚠ unreadable: {', '.join(bad)} (rerun that verifier)")
    print(p.render())


if __name__ == "__main__":
    main()
