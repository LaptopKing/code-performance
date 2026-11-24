# code-performance

A [Claude Code](https://claude.com/claude-code) skill that finds ways to make existing code **faster or
lighter without changing what it does**, and shows them as a one-screen terminal dashboard. Sibling of
[code-audit](https://github.com/LaptopKing/code-audit): same dashboard style, different question.

Supports the same languages as code-audit: JavaScript, TypeScript, JSX/TSX, Vue `<script>`, Python,
Go, Rust, Java, Ruby, PHP and shell.

## Install

Needs `git`, Python 3.8+ and [ast-grep](https://ast-grep.github.io) (`sg`). Cloning into the skills
folder is the whole install (restart the agent session). Linux, macOS and Windows.

**One command**

```bash
git clone https://github.com/LaptopKing/code-performance ~/.claude/skills/code-performance
```

Windows (PowerShell): `git clone https://github.com/LaptopKing/code-performance $HOME\.claude\skills\code-performance`

**One prompt** (paste into Claude Code or any other agent)

```text
Install the code-performance skill: clone https://github.com/LaptopKing/code-performance into my
agent's skills folder (~/.claude/skills/code-performance for Claude Code) and check that `sg` is installed.
```

**Other agents**

| Agent | Clone into |
|-------|------------|
| Claude Code | `~/.claude/skills/code-performance` |
| Codex, Gemini CLI, OpenCode | `~/.agents/skills/code-performance` |

Update: `git -C ~/.claude/skills/code-performance pull`.
Needed: `brew install ast-grep` (or `npm i -g @ast-grep/cli`, `cargo install ast-grep`).
Optional: `brew install scc hyperfine`. scc adds size and complexity hotspots; hyperfine times the
before and after of `apply`.

## Usage

```text
/code-performance                         review the whole repo
/code-performance src/import              review one folder
why is the mix export slow?               a targeted review of one flow
```

Then type a command to switch pages, no rescan:

| Type | Shows / does |
|------|--------------|
| `show 2` | proposal 2 in full: now, faster way, why the result stays the same, risk, timing |
| `apply 2` | makes change 2, runs the tests and times it before and after; keeps it only if tests pass and it is faster |
| `leads` · `leads harmless` | every suspicious spot the pattern scan found, and what became of it |
| `hotpaths` | where the time goes: the code that runs most |
| `check more spots` | judges the next batch of suspicious spots on a big repo |
| `deep` | opt-in multi-agent review (up to 13 agents, asks first: costs several 100k tokens) |
| `rescan` | run the scan again after code changes |

## Example

code-audit reviewed by this skill (`/code-performance ~/projects/code-audit`):

```text
PERFORMANCE · code-audit · 10 files · whole scope · 2026-11-24 10:32
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
WHERE TIME GOES  the 3 security scanners (minutes per audit); everything else takes seconds
FASTER ROUTES    2 found, same results · 1 timed
SPOTS CHECKED    4 suspicious patterns: 1 became a route · 3 harmless
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
     what gets faster                                         runs       work   gain                    where
  1. Run the 3 security scanners at the same time             often      small  sum → slowest scanner   scripts/scan.py:500-505
  2. Find the terminal size with 1 process instead of up to 8 often      small  16.1 ms→12.0 ms timed   scripts/render.py:30
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
timed = measured on a safe copy, others are estimates · unsure = one thing not verified yet
next: show 1 (details) · apply 1 (make the change and test it) · leads (all spots) · hotpaths
```

Suspicious spots come from `scripts/scan.py`: ast-grep rules for patterns that are often slow, such as
a database call, a process start or a list search inside a loop. Each one comes with a code excerpt,
the faster route, and the condition that must hold for the result to stay the same. Claude checks
each spot, then times the best few when that is safe: on copies, under a minute, outputs asserted
equal, repo untouched. A default run uses no subagents, so it stays cheap in tokens. Full agent
instructions: [`SKILL.md`](SKILL.md).

## Tests

```bash
cd scripts && python3 -m unittest test_scan
```
