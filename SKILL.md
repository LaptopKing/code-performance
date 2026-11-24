---
name: code-performance
description: Find ways to make existing code faster or lighter WITHOUT changing what it does, and propose the faster route for each spot. Command-first like code-audit - scan.py finds and ranks leads with ast-grep in JS/TS/Vue, Python, Go, Rust, Java, Ruby, PHP and shell (await/lookup/sort/regex in a loop, subprocess/HTTP/query per item, deep copies, clones and defers in loops, sync I/O, deep watchers) with code excerpts, the faster route and the same-behaviour check; Claude judges them in one cheap pass, benchmarks the best few when that is safe, and shows a one-window terminal dashboard. Overall mode covers a repo or folder; targeted mode digs into one function, route or flow ("why is the export slow"). `apply <id>` implements one proposal with tests and hyperfine before/after. `deep` runs an opt-in multi-agent workflow. Use whenever the user asks to optimize, speed up, profile, find bottlenecks, reduce memory/startup/bundle time, asks "why is X slow" or "how can X be faster", or says /code-optimization. Not for bugs (use code-review) or code health (use code-audit).
---

# Code performance review

The user wants the same results by a faster route, and does not want one run to eat their usage.
So the scripts do the work and you only judge:
- **Leads** come from `scripts/scan.py`: ast-grep hits grouped per function, ranked by how hot the
  path looks, each with a 7-line excerpt, the faster route and the check that must hold. Written to
  `leads.txt` (for you) and `facts.json` (for the renderer).
- **Judgement** is yours: confirm or dismiss leads, write `notes.json`.
- **Ranking and display** come from `scripts/render.py`.

A change that alters output, order, errors, side effects or public API is not a proposal.

Languages (the same set as `/code-audit`): JS, TS, JSX/TSX, Vue `<script>`, Python, Go, Rust, Java,
Ruby, PHP, shell. One rule id covers every language it applies to, so dismissing `sort-in-loop`
dismisses it everywhere. Rules by language: lookup/sort/regex/spawn/http/query in a loop for all
(regex not PHP: preg caches; shell: spawn and http only); await in a loop for JS/Python/Rust; copy
per item for JS (spread) and PHP (array_merge); deep copy for JS/Python/Ruby; clone per item for
Rust; defer per item for Go; sync I/O and deep watchers for JS/Vue. Test files are skipped in every
language. Test commands are detected for npm/bun, pytest, go, cargo, maven/gradle, rspec/rake,
phpunit/composer.

## Token budget (keep to it)

| Run | What you do | Rough cost (estimate) |
|---|---|---|
| default | scan, read top ≤30 leads from `leads.txt`, judge inline, ≤3 safe benchmarks | ~20–50k tokens |
| `all` on a big scope | default + up to 3 subagents in one message for the rest of the leads | +~30–50k per subagent |
| `deep` | the Workflow in `workflow/perf-review.js`: up to 13 agents | several 100k; ask first |

- Judge from the excerpt in `leads.txt`. Open a file only when the excerpt does not show what you need
  (loop size, who calls it), read ≤40 lines around the lead, and at most ~10 such reads per run.
- Never `cat` facts.json; use `jq` on one key.
- No subagents in the default run.

## 1. Pick scope and mode (~1 min)

- Scope = the path the user named; none → repo root.
- **Overall** (default): `scan.py <scope>`.
- **Targeted** ("why is X slow", a function/route/flow named): find X with CodeMapper `search`, follow
  its callees with `expand`/`path` to the files that do its work (≤10 files), then
  `scan.py <scope> --target "<X>" --files <those files>`. A targeted run has its own cache folder, so
  it never wipes the overall review.
- Area named ("only the DB calls", "ffmpeg") → judge only leads of that specialist:
  `algo io data proc mem startup ui`.

## 2. Scan (~1 s)

```bash
python3 ~/.claude/skills/code-performance/scripts/scan.py <scope> [--target "<X>" --files <f> ...]
```

Prints the path of `facts.json`; `leads.txt` sits next to it. A new scan clears the old judgement.
Then `jq '.inventory | {files, code_lines}, .rules, .lead_note' <facts.json>` (small).
ast-grep missing → the scan says so; tell the user `brew install ast-grep` and look at the scc
hotspots (`jq '.inventory.hot_files'`) by hand instead.

## 3. Judge → notes.json

Read `head -n 360 <dir>/leads.txt` (≈ the top 30 leads). For each lead: is the path really hot, and
does its `check` hold here? Then either propose it or dismiss it. Dismiss a whole rule with one entry
when every hit is harmless for the same reason. While you are in a hot file anyway, you may add up
to 3 proposals that no rule found (ids `X1..X3`): e.g. the same file probed twice, work repeated
across calls, a cache missing on a hot path.

Write `<dir>/notes.json`:

```json
{
  "summary": "≤120 chars: where this code spends its time",
  "hot_paths": [{"name": "mix export", "entry": "routes/mix.js:41", "why_hot": "per request", "steps": []}],
  "dismiss": {"L7": "≤50 chars why", "py-sort-in-loop": "lists of ≤10 items"},
  "proposals": [
    {"id": "L3", "title": "≤60 chars: what gets faster", "why_same": "≤90 chars: the check, confirmed",
     "gain": "≤40 chars, e.g. O(n·m) → O(n+m)", "gain_class": "high|med|low", "effort": "S|M|L",
     "risk": "≤60 chars", "verdict": "confirmed|unsure", "note": "≤90 chars: what you read",
     "measured": {"before": "41 ms", "after": "9 ms", "how": "hyperfine, n=10k, 10 runs"}}
  ]
}
```

A proposal on a lead (`L3`) takes `where`, `now`, `change`, `hot` and `specialist` from the lead;
set them only to override (e.g. a more exact `change`, `hot` after you checked the caller). An `X`
proposal sets all of them. `hot_paths` is optional, ≤3; `steps` can stay empty.
`unsure` = same behaviour depends on something you could not check; say what in `note`.

**Big scope** (more than ~30 leads judged worth reading, and the user said `all` or agreed when you
offered it): spawn up to 3 subagents in ONE message, split by specialist:
`algo+mem`, `io+data+proc`, `startup+ui`. Give each the path of `leads.txt`, its specialist keys
(`rg -A11 '\[io, ' leads.txt`), the notes schema above, this section's rules, and tell it to write
`<dir>/notes-<group>.json` and return only the counts. The renderer merges every `notes*.json`.

## 4. Benchmark the best few, when it is safe

Measure up to 3 of the strongest proposals (`gain_class` high or med) during the diagnosis, but
only when ALL of these hold:
- it runs in under ~60 s and the harness takes a few minutes to write;
- it works on generated input or a copy of small real input inside `<dir>/bench/<id>/`;
- it touches nothing real: no writes outside `<dir>/bench/`, no network, no database, no queues, no
  tool that writes to a user path (an ffmpeg bench writes its output into the bench folder);
- the repo stays unchanged (`git status` the same before and after).
Copy the slow and the fast version side by side, run them on the same input, and **assert their
outputs are equal** (a cheap same-behaviour check), then `hyperfine --warmup 2 --runs 10` or a timing
loop. Record `measured`; when you skip one, write the reason in `note`. Benchmarks that would need a
live service or real data go to `apply`.

## 5. Show a page

```bash
python3 ~/.claude/skills/code-performance/scripts/render.py <facts.json> --page overview
```

Pages: `overview`, `proposals`, `show --filter <id>`, `leads [--filter <rule|specialist|open|file>]`,
`hotpaths`, `specialists`, `rejected`. `--n 2` = next page of a long list.

Copy the printed block **verbatim** as your whole reply: Bash output is not shown to the user, and a
line above the block pushes its top off screen. At most one short line under it (what you skipped,
e.g. "judged the top 30 of 84 leads"). The block's last line lists what the user can type next.

## 6. Follow-ups

- "proposals io", "show L3", "leads open" → render that page. Don't rescan.
- "more" / "the rest" → judge the next leads in `leads.txt` (`sed -n` the next ~360 lines), add to
  `notes.json`, render.
- "why is X slow" on an existing review → a targeted run (step 1).
- "rescan" or the code changed → steps 2–5 again.
- Lines cut off → re-render with `CODE_PERF_RESERVE=<n>` (default 10) or `--rows/--cols`.

## 7. `apply <id>`: implement one proposal and prove it

One proposal at a time, in this order. Acceptance: tests pass with the same counts before and after,
the benchmark mean improves by more than twice the larger standard deviation, and only the `where`
lines (plus imports) changed.

1. Read the proposal with `jq` from `notes*.json` (or `verified/*.json` after `deep`).
2. `git status --short <files>`: files with uncommitted changes are the user's work → ask before
   editing. Copy each file you will touch to `<dir>/applied/<id>.bak/`.
3. Baseline: run `facts.commands.test` (none → tell the user there is no safety net and ask before
   keeping the change). Benchmark: reuse `<dir>/bench/<id>/` if step 4 built one, else write a harness
   there that calls the real function on realistic input (same safety rules as step 4, except it may
   read real input read-only): `hyperfine --warmup 3 --runs 10 --export-json before.json '<cmd>'`.
4. Make the change: only the `where` lines and the imports they need, in the code's own style.
5. Tests and the same benchmark again (`after.json`).
6. Tests identical and the gain beats the noise → keep (`"status": "kept"`). Otherwise restore the
   backups (`"reverted"`) and say what failed.
7. Write `<dir>/applied/<id>.json`: `{"id", "status", "tests_before", "tests_after", "bench_before",
   "bench_after", "files", "note"}` (bench as mean ± stddev with units), then render `show --filter <id>`.
Never commit or stage.

## 8. `deep`: the multi-agent workflow (opt-in only)

Only when the user says `deep`. First tell them the cost (up to 13 agents, at most 5 at once, several
100k tokens, ~5–15 min) and wait for a yes. Then write the run goal: one paragraph naming what
"faster" means here, the suspected hot paths, what must not change, and what a good run returns. Call:

```
Workflow({ scriptPath: "<absolute $HOME>/.claude/skills/code-performance/workflow/perf-review.js",
           args: { facts, dir, scope: facts.scope, root: facts.root, goal, target?, focus?: ["io"] } })
```

Stages: 1 scout (hot paths, assigns ≤6 specialists) → specialists, each in its own scope → one
skeptic per specialist that tries to refute and benchmarks. Each prompt carries a `DONE WHEN`
acceptance spec. Results land in `scout.json` and `verified/*.json`; render as in step 5.

## What the labels mean (tell the user when asked)

- ✅ confirmed · ❔ unsure (depends on something not checked) · ❌ rejected (deep mode: changes
  behaviour or no gain) · 🚀 applied and kept · ↩️ applied and reverted.
- Leads: ✅ proposed · ✖ dismissed (with the reason) · · not judged yet.
- **hot**: how often the code runs. scan.py guesses from the path (routes/services/workers/components
  = hot, scripts/migrations/tools = cold, else warm); a proposal's `hot` is your checked value.
- **gain / measured**: measured before→after when a benchmark ran, else the estimate.
- **E** = effort S/M/L.
