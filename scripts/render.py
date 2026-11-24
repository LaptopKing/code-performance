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
VERDICT_ICON = {"confirmed": "✅", "unsure": "❔", "rejected": "❌"}
GAIN = {"high": 3, "med": 2, "low": 1}
HOT = {"hot": 3, "warm": 2, "unknown": 1.5, "cold": 1}
EFFORT = {"S": 1, "M": 2, "L": 3}
PAGES = ["overview", "proposals", "show", "hotpaths", "specialists", "rejected", "leads"]
LEAD_ICON = {"proposed": "✅", "dismissed": "✖", "open": "·"}


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
        self.w = max(50, cols - 4)
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
    mode = f"target: {facts['target']}" if facts.get("target") else "overall"
    p.add(f"{title} · {facts['scope']} · {inv.get('files', '?')} files · {mode} · {facts['scanned_at'][:16]}")
    p.rule()


def tag(x):
    """Status icon: applied result wins over the verifier's verdict."""
    a = x.get("applied")
    if a:
        return "🚀" if a.get("status") == "kept" else "↩️"
    return VERDICT_ICON.get(x.get("verdict"), "•")


def prop_header(p):
    return pad("     st id     hot  E title", p.w - 51) + " " + pad("gain / measured", 22) + " where"


def prop_line(p, x, i=None):
    meas = x.get("measured") or {}
    gain = f"{meas['before']}→{meas['after']}" if meas.get("before") else x.get("gain", "")
    num = f"{i:>2}. " if i is not None else ""
    left = f"{num}{tag(x)} {x.get('id', '?'):<7}{x.get('hot', '?')[:4]:<5}{x.get('effort', '?')} "
    return left + pad(x.get("title", ""), p.w - width(left) - 52) + " " + pad(gain, 22) + " " + lfit(x.get("where", ""), 28)


def live(props):
    return [x for x in props if x.get("verdict") != "rejected"]


def matches(x, flt):
    flt = flt.lower()
    return not flt or any(flt in str(x.get(k, "")).lower() for k in ("id", "specialist", "title", "where", "hot"))


def page_overview(p, facts, scout, specs, props, n, flt):
    head(p, facts, "PERFORMANCE")
    if scout["summary"]:
        p.add(f"WHERE TIME GOES  {scout['summary']}")
    c = {k: sum(1 for x in props if x.get("verdict") == k) for k in VERDICT_ICON}
    applied = sum(1 for x in props if x.get("applied"))
    measured = sum(1 for x in props if x.get("measured"))
    p.add(f"PROPOSALS        ✅ {c['confirmed']} confirmed · ❔ {c['unsure']} unsure · ❌ {c['rejected']} rejected · "
          f"{measured} measured · {applied} applied")
    st = {k: sum(1 for g in facts["leads"] if g["status"] == k) for k in LEAD_ICON}
    p.add(f"LEADS            {len(facts['leads'])} from ast-grep: {st['proposed']} proposed · "
          f"{st['dismissed']} dismissed · {st['open']} not judged"
          + (f" · {facts['lead_note']}" if facts.get("lead_note") else ""))
    if not props and not st["dismissed"]:
        p.add("                 not judged yet: SKILL.md step 3 writes notes.json")
    p.rule()
    p.add("TOP FASTER ROUTES")
    p.add(prop_header(p))
    p.listing(live(props), lambda i, x: prop_line(p, x, i), n, footer_lines=2, page_cmd="overview")
    p.rule()
    p.add("next: show <id> · proposals · hotpaths · specialists · rejected · leads · apply <id>")


def page_proposals(p, facts, scout, specs, props, n, flt):
    head(p, facts, "PROPOSALS")
    rows = [x for x in live(props) if matches(x, flt)]
    p.add(f"{len(rows)} live proposals{f' matching {flt!r}' if flt else ''} · ranked by verdict, gain × hot ÷ effort")
    p.add(prop_header(p))
    p.listing(rows, lambda i, x: prop_line(p, x, i), n, footer_lines=2, page_cmd=f"proposals {flt}".strip())
    p.rule()
    p.add("next: show <id> · apply <id> · proposals <specialist|file> · overview")


def page_show(p, facts, scout, specs, props, n, flt):
    x = next((x for x in props if x.get("id", "").lower() == flt.lower()), None)
    head(p, facts, f"PROPOSAL {flt}")
    if not x:
        p.add(f"no proposal with id {flt!r}; ids: {', '.join(y.get('id', '?') for y in props[:20])}")
        return
    p.add(f"{tag(x)} {x.get('title', '')}")
    p.add(f"where     {x.get('where', '')}   ({x['specialist']} · hot: {x.get('hot')} · effort {x.get('effort')})")
    p.rule()
    for label, key in (("now", "now"), ("faster", "change"), ("same b/c", "why_same"), ("gain", "gain"),
                       ("risk", "risk")):
        p.add(f"{label:<10}{x.get(key, '')}")
    p.add(f"{'note':<10}{x.get('verify_note') or x.get('note', '')}")
    m = x.get("measured")
    p.add(f"{'measured':<10}" + (f"{m.get('before')} → {m.get('after')}  ({m.get('how', '')})" if m else "not measured"))
    a = x.get("applied")
    if a:
        p.rule()
        p.add(f"applied   {a.get('status')} · tests {a.get('tests_before')} → {a.get('tests_after')}")
        p.add(f"bench     {a.get('bench_before')} → {a.get('bench_after')}")
        p.add(f"note      {a.get('note', '')}")
    if x.get("smells"):
        p.add(f"{'covers':<10}{', '.join(x['smells'])}")
    p.rule()
    p.add(f"next: apply {x.get('id')} · proposals · overview")


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
        c = {k: sum(1 for x in mine if x.get("verdict") == k) for k in VERDICT_ICON}
        opened = sum(1 for g in leads if g["status"] == "open")
        p.add(f"{key:<8}{pad(name, 34)} {len(leads):>5} {opened:>5}  ✅ {c['confirmed']} ❔ {c['unsure']} ❌ {c['rejected']}")
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
    p.listing(rows, lambda i, x: f"{i:>2}. {x.get('id', '?'):<7}{pad(x.get('title', ''), 40)} "
                                 f"{x.get('verify_note', '')}", n, footer_lines=2, page_cmd="rejected")
    p.rule()
    p.add("next: show <id> · proposals · overview")


def page_leads(p, facts, scout, specs, props, n, flt):
    head(p, facts, "LEADS (ast-grep)")
    f = flt.lower()
    rows = [g for g in facts["leads"]
            if not f or f in " ".join([g["rule"], g["specialist"], g["file"], g["status"], g["heat"]]).lower()]
    p.add(f"{len(rows)} leads{f' matching {flt!r}' if flt else ''} · ✅ proposed · ✖ dismissed · · not judged")
    def fmt(i, g):
        where = lfit(f"{g['file']}:{g['lines'][0]}", 30)
        left = f"{LEAD_ICON[g['status']]} {g['id']:<5}{pad(g['rule'], 22)} {g['heat']:<5}{where:<30} "
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
    a = ap.parse_args()
    rows, cols = term_size()
    p = Page(a.rows or rows, a.cols or cols)
    facts, scout, specs, props, bad = load(a.facts)
    globals()[f"page_{a.page}"](p, facts, scout, specs, props, a.n, a.filter)
    if bad:
        p.lines.insert(2, f"⚠ unreadable: {', '.join(bad)} (rerun that verifier)")
    print(p.render())


if __name__ == "__main__":
    main()
