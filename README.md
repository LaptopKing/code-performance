# code-performance

A Claude Code skill that finds faster routes for existing code without changing what it does.
Sibling of [code-audit](../code-audit): same one-window terminal dashboard, different question.

```
/code-performance                      overall review of the repo
/code-performance src/import          overall review of one folder
/code-performance why is the mix export slow   targeted run on one flow
/code-performance apply L3            implement one proposal, tests + hyperfine before/after
```

## How it works

Command-first, like code-audit: scripts do the counting, Claude only judges.

1. `scripts/scan.py` runs ast-grep rules (JS/TS/Vue, Python, Go, Rust, Java, Ruby, PHP, shell) once, groups hits per function, ranks
   them by how hot the path looks, and writes `leads.txt`: each lead with a 7-line excerpt, the
   faster route and the check that must hold for the result to stay the same.
2. Claude reads the top ~30 leads, confirms or dismisses them, benchmarks up to 3 when it is safe
   (scratch copies, under a minute, outputs asserted equal) and writes `notes.json`.
3. `scripts/render.py` draws the ranked dashboard.

`deep` (opt-in) runs `workflow/perf-review.js`: scout → up to 6 specialists → one skeptic each.

## Design decisions

- **Token budget first.** The default run uses no subagents: the excerpt, route and check in
  `leads.txt` let Claude judge most leads without opening files (~20–50k tokens, estimate).
  Big scopes use at most 3 subagents in one message; the 13-agent workflow is opt-in.
- **Same behaviour is checked, not assumed:** each rule carries its condition, benchmarks assert
  equal outputs, `apply` keeps a change only when tests are unchanged and the gain beats 2× noise.
- **Benchmarks never touch real data:** generated input or copies under the cache folder; anything
  needing a live service waits for `apply`.
- **Deep mode** caps concurrency at 5 and gives every stage a `DONE WHEN` acceptance spec plus a
  per-run goal, so agents stop at done instead of looping.

## Tests

```
cd scripts && python3 -m unittest test_scan
```
