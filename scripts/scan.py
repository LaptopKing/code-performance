#!/usr/bin/env python3
"""Deterministic facts for the code-performance skill.

Does the token-free half of a performance review so the agent only has to judge:
- leads: candidate slow patterns found with ast-grep (await in a loop, a lookup inside a loop,
  a subprocess or query per item, a deep clone, ...), grouped per function, ranked by how hot
  the path looks, each with a short code excerpt, the faster route and the condition that must
  hold for the result to stay the same. Written to facts.json and, ranked, to leads.txt.
- inventory: code files, lines and complexity hotspots (scc when installed).
- commands: how to run this project's tests and benchmarks, and which tools exist.
A lead is not a finding: the agent confirms or dismisses it.

Usage: scan.py <scope> [--target TEXT --files F ...]
  --files limits the scan to those files (a targeted run on one flow); --target labels it.
Prints the path of the written facts.json.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

CACHE = Path(os.environ.get("CODE_PERF_CACHE", Path.home() / ".cache" / "code-performance"))
SKIP_GLOBS = ["!**/node_modules/**", "!**/dist/**", "!**/build/**", "!**/vendor/**", "!**/.venv/**",
              "!**/venv/**", "!**/coverage/**", "!**/*.min.js",
              # tests are not a performance target
              "!**/tests/**", "!**/test/**", "!**/__tests__/**", "!**/*.test.*", "!**/*.spec.*",
              "!**/test_*.py", "!**/*_test.py", "!**/*_test.go", "!**/*Test.java", "!**/*Tests.java",
              "!**/*_spec.rb", "!**/spec/**", "!**/*Test.php", "!**/target/**", "!**/benches/**"]
MAX_LEADS = 400
EXCERPTS = 60          # leads that get a code excerpt (the top of the ranking)
EXCERPT_AROUND = 3     # lines above and below the first hit

SPECIALISTS = {
    "algo": "algorithms & data structures",
    "io": "file I/O, async & concurrency",
    "data": "database & network calls",
    "proc": "subprocesses & media pipelines",
    "mem": "memory, copies & caching",
    "startup": "startup, imports, bundle & build",
    "ui": "frontend rendering & reactivity",
}

# Path parts that say how often code runs. A hint for ranking; the agent corrects it.
HOT_PARTS = {"routes", "route", "api", "handlers", "handler", "controllers", "controller", "services",
             "service", "repository", "repositories", "models", "workers", "jobs", "queue",
             "queues", "components", "pages", "views", "stores", "hooks", "dsp", "audio", "video", "render",
             "pipeline", "pipelines", "server", "middleware", "core"}
COLD_PARTS = {"scripts", "bin", "tools", "migrations", "seeds", "seed", "fixtures", "examples", "docs",
              "config", "setup", "install", "deploy"}

JS_LANGS = ["javascript", "typescript", "tsx"]


def body_of(kind, field="body"):
    """`X is the <field> of a <kind> node`: only the loop BODY counts, so work in the loop
    header (`for x of arr.filter(..)`, `for m in sorted(xs)`) runs once and is not a lead."""
    return {"inside": {"kind": kind, "field": field}}


def callback_of(call_kind, name_field, names, arg_field="arguments"):
    """X is the argument list (or block) of a call like `xs.map(...)` / `xs.each do ... end`."""
    return {"inside": {"kind": call_kind, "field": arg_field,
                       "has": {"field": name_field, "regex": f"^(.+\\.)?({names})$"}}}


ITER = "forEach|map|flatMap|reduce|filter|some|every"
LOOPS = {
    "javascript": [body_of("for_statement"), body_of("for_in_statement"), body_of("while_statement"),
                   body_of("do_statement"), callback_of("call_expression", "function", ITER)],
    "python": [body_of("for_statement"), body_of("while_statement"), body_of("list_comprehension"),
               body_of("generator_expression"), body_of("dictionary_comprehension")],
    "go": [body_of("for_statement")],
    "rust": [body_of("for_expression"), body_of("while_expression"), body_of("loop_expression"),
             callback_of("call_expression", "function", "for_each|map|filter|filter_map|flat_map|fold|any|all")],
    "java": [body_of("for_statement"), body_of("enhanced_for_statement"), body_of("while_statement"),
             body_of("do_statement"), callback_of("method_invocation", "name", ITER + "|anyMatch|allMatch")],
    "ruby": [body_of("for"), body_of("while"), body_of("until"),
             callback_of("call", "method", "each|each_with_index|each_with_object|map|flat_map|select|"
                                           "reject|filter_map|times|upto|inject|reduce|find_each", "block")],
    "php": [body_of("foreach_statement"), body_of("for_statement"), body_of("while_statement"),
            body_of("do_statement")],
    "bash": [body_of("for_statement"), body_of("c_style_for_statement"), body_of("while_statement")],
}
for _ts in ("typescript", "tsx"):
    LOOPS[_ts] = LOOPS["javascript"]


def call(regex, kind="call_expression", field="function"):
    """A call whose callee text (`fetch`, `exec.Command`, `v.sort`) matches regex."""
    return {"kind": kind, "has": {"field": field, "regex": f"^({regex})$"}}


def js_method(names):
    """A JS call `x.<name>(...)`."""
    return {"kind": "call_expression", "has": {"field": "function", "kind": "member_expression",
                                               "has": {"field": "property", "regex": f"^({names})$"}}}


def java_call(names, obj=None):
    r = {"kind": "method_invocation", "has": {"field": "name", "regex": f"^({names})$"}}
    if obj:
        r = {"kind": "method_invocation", "all": [{"has": r["has"]}, {"has": {"field": "object", "regex": obj}}]}
    return r


def ruby_call(names, receiver=None):
    r = {"kind": "call", "has": {"field": "method", "regex": f"^({names})$"}}
    if receiver:
        r = {"kind": "call", "all": [{"has": r["has"]}, {"has": {"field": "receiver", "regex": receiver}}]}
    return r


def php_fn(names):
    return call(names, kind="function_call_expression")


def php_method(names):
    return {"any": [{"kind": "member_call_expression", "has": {"field": "name", "regex": f"^({names})$"}},
                    {"kind": "scoped_call_expression", "has": {"field": "name", "regex": f"^({names})$"}}]}


def py(regex):
    return call(regex, kind="call")


def anyof(*rules):
    return {"any": list(rules)}


def js(body):
    return {lang: body for lang in JS_LANGS}


def rule(spec, weight, cost, route, check, bodies, loop=True):
    """weight 3/2/1 = how much this pattern usually costs when the path is hot.
    bodies = {ast-grep language: rule body}; one rule id covers every language."""
    return {"specialist": spec, "weight": weight, "cost": cost, "route": route, "check": check,
            "bodies": bodies, "loop": loop}


# External tools a shell loop starts once per item (builtins like echo/printf/test are free).
SHELL_TOOLS = ("ffmpeg|ffprobe|sox|magick|convert|identify|exiftool|mediainfo|grep|sed|awk|cut|tr|jq|yq|wc|"
               "sort|uniq|head|tail|cat|basename|dirname|date|expr|find|stat|bc|python3?|node|git")

RULES = {
    "await-in-loop": rule(
        "io", 3, "one await per item: items run one after another",
        "run the items concurrently (Promise.all / asyncio.gather / join_all), bounded",
        "items independent; result order kept; first failure still fails; no rate limit hit",
        {**js({"kind": "await_expression"}), "python": {"kind": "await"}, "rust": {"kind": "await_expression"}}),
    "lookup-in-loop": rule(
        "algo", 3, "linear search per item: O(n·m)",
        "build a hash set/map once before the loop, then look up by key",
        "the list is not changed inside the loop; equality matches the set's (===, equals/hashCode, eql?)",
        {**js(js_method("includes|indexOf|lastIndexOf|find|findIndex|some|filter")),
         "python": py(r"\w+\.index"), "go": call(r"slices\.(Contains|Index|IndexFunc|ContainsFunc)"),
         "rust": call(r".+\.(contains|position)"), "java": java_call("contains|indexOf|lastIndexOf"),
         "ruby": ruby_call(r"include\?|index|find_index|detect"), "php": php_fn("in_array|array_search")}),
    "sort-in-loop": rule(
        "algo", 2, "sort per item: O(n·m log m)",
        "sort once outside the loop, or a single pass / heap for min, max, top-k",
        "the sorted data does not change between iterations; same stability",
        {**js(js_method("sort|toSorted")), "python": py(r"sorted|\w+\.sort"),
         "go": call(r"sort\.(Slice|SliceStable|Strings|Ints|Float64s|Sort|Stable)|"
                    r"slices\.(Sort|SortFunc|SortStableFunc)"),
         "rust": call(r".+\.(sort|sort_by|sort_by_key|sort_unstable|sort_unstable_by)"), "java": java_call("sort"),
         "ruby": ruby_call("sort|sort_by|sort!|sort_by!"),
         "php": php_fn("sort|rsort|usort|uasort|uksort|ksort|krsort|asort|arsort")}),
    "regex-in-loop": rule(
        "algo", 1, "regex compiled per item",
        "compile the regex once outside the loop (module level / static final)",
        "the pattern does not depend on the item; no shared match state (JS /g lastIndex)",
        {**js({"pattern": "new RegExp($$$)"}), "python": py(r"re\.compile"),
         "go": call(r"regexp\.(MustCompile|Compile)"), "rust": call(r"(regex::)?Regex::new"),
         "java": anyof(java_call("compile", obj="^Pattern$"), java_call("matches|replaceAll|replaceFirst")),
         "ruby": ruby_call("new", receiver="^Regexp$")}),
    "copy-in-loop": rule(
        "mem", 2, "copy of the whole array/object per item: O(n²)",
        "append into one accumulator (push / $a[] = / extend)",
        "the intermediate copies are not kept or shared anywhere",
        {**js({"kind": "spread_element"}), "php": php_fn("array_merge")}),
    "deep-copy": rule(
        "mem", 2, "deep copy of a whole structure (JSON round-trip, deepcopy, Marshal)",
        "a shallow copy, or no copy",
        "nothing mutates the nested parts of the copy",
        {**js({"pattern": "JSON.parse(JSON.stringify($X))"}), "python": py(r"(copy\.)?deepcopy"),
         "ruby": {"pattern": "Marshal.load(Marshal.dump($X))"}}, loop=False),
    "clone-in-loop": rule(
        "mem", 1, "allocates a copy per item",
        "borrow (&T / &str) or move instead of cloning",
        "the loop never mutates or keeps the copy beyond the source's lifetime",
        {"rust": call(r".+\.(clone|to_vec|to_owned|to_string)")}),
    "defer-in-loop": rule(
        "mem", 2, "defers pile up until the function returns (files/locks held)",
        "move the loop body into its own function so each defer runs per item",
        "the deferred call only has to run before the next item, not at function end",
        {"go": {"kind": "defer_statement"}}),
    "sync-io": rule(
        "io", 2, "blocks the event loop while it waits",
        "the fs/promises or async child_process version",
        "not startup-only code; callers can await; ordering of side effects kept",
        js(call(r"(\w+\.)?(readFileSync|writeFileSync|appendFileSync|existsSync|statSync|readdirSync|"
                r"copyFileSync|execSync|execFileSync|spawnSync)")), loop=False),
    "spawn-in-loop": rule(
        "proc", 3, "one process start per item",
        "one process for all items (batch args, one ffprobe/ffmpeg pass, xargs/awk), or a bounded pool",
        "the tool accepts many inputs and its output is identical; errors still reported per item",
        {**js(call(r"(\w+\.)?(spawn|exec|execFile|execa|execaCommand|fork|spawnSync|execSync)")),
         "python": py(r"subprocess\.\w+|os\.system|os\.popen"), "go": call(r"exec\.(Command|CommandContext)"),
         "rust": call(r"(std::process::)?Command::new"),
         "java": anyof({"kind": "object_creation_expression", "has": {"field": "type", "regex": "^ProcessBuilder$"}},
                       java_call("exec", obj="Runtime")),
         "ruby": anyof(ruby_call("system|spawn|exec|capture2|capture2e|capture3|popen|popen3"), {"kind": "subshell"}),
         "php": php_fn("exec|shell_exec|system|passthru|proc_open|popen"),
         "bash": {"kind": "command", "has": {"field": "name", "regex": f"^({SHELL_TOOLS})$"}}}),
    "http-in-loop": rule(
        "data", 3, "one network round trip per item",
        "one batch request, or bounded parallel with one shared keep-alive client",
        "a batch endpoint exists or requests are independent; order and error handling kept",
        {**js(call(r"fetch|axios(\.\w+)?|got(\.\w+)?|ky(\.\w+)?|\$fetch")),
         "python": py(r"(requests|httpx|session|client)\.(get|post|put|request)|urlopen"),
         "go": call(r"http\.(Get|Post|Head|PostForm)|\w*[Cc]lient\.(Do|Get|Post|Head)"),
         "rust": call(r"reqwest::(blocking::)?get|.*[Cc]lient\.(get|post|put|execute)"),
         "java": java_call("send|sendAsync|openConnection|getForObject|getForEntity|postForObject|"
                           "postForEntity|exchange"),
         "ruby": ruby_call("get|post|put|request|get_response",
                           receiver=r"Net::HTTP|HTTParty|Faraday|RestClient|client"),
         "php": anyof(php_fn("curl_exec"), php_method("request|sendRequest")),
         "bash": {"kind": "command", "has": {"field": "name", "regex": "^(curl|wget|http)$"}}}),
    "query-in-loop": rule(
        "data", 3, "one DB query per item (N+1)",
        "one IN (...) query, a join or eager loading (includes / with), then map back by key",
        "same rows and order after mapping back; no per-row transaction semantics",
        {**js(js_method("query|execute|findOne|findUnique|findFirst|findById|findMany|findAll|aggregate|"
                        "countDocuments")),
         "python": py(r"\w+\.(execute|executemany|fetchone)"),
         "go": call(r".+\.(Query|QueryRow|Exec|QueryContext|QueryRowContext|ExecContext)"),
         "rust": call(r".+\.(execute|fetch_one|fetch_all|fetch_optional|query_row)"),
         "java": java_call("executeQuery|executeUpdate|createQuery|findById|getSingleResult|getResultList"),
         "ruby": ruby_call(r"find_by\w*|where|pluck|exists\?|find_or_create_by|find_each"),
         "php": anyof(php_fn("mysqli_query|pg_query|pg_execute"),
                      php_method("query|execute|prepare|findBy|findOneBy|find|findOrFail|where|first"))}),
    "deep-watch": rule(
        "ui", 1, "deep watcher walks the whole object on every change",
        "watch the one field that matters (getter), or shallowRef",
        "the handler only cares about that field",
        js({"kind": "pair", "all": [{"has": {"field": "key", "regex": "^deep$"}},
                                     {"has": {"field": "value", "regex": "^true$"}}]}), loop=False),
}


def rule_yaml():
    """One ast-grep document per (rule, language). JSON is valid YAML, so no indentation games."""
    docs = []
    for rid, r in RULES.items():
        for lang, body in r["bodies"].items():
            body = dict(body)
            if r["loop"]:
                body["inside"] = {"stopBy": "end", "any": LOOPS[lang]}
            docs.append(json.dumps({"id": rid, "language": lang, "rule": body}))
    return "\n---\n".join(docs)


def run_smells(root, paths):
    """Run every rule in one ast-grep pass over paths. Returns (hits, note)."""
    if not shutil.which("sg"):
        return [], "ast-grep (sg) missing: no leads, search by hand (brew install ast-grep)"
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "sgconfig.yml"
        cfg.write_text('ruleDirs: []\nlanguageGlobs:\n  html: ["*.vue"]\n')  # parse <script> in .vue files
        cmd = ["sg", "scan", "-c", str(cfg), "--inline-rules", rule_yaml(), "--json=stream"]
        for g in SKIP_GLOBS:
            cmd += ["--globs", g]
        proc = subprocess.run(cmd + [str(p) for p in paths], capture_output=True, text=True, cwd=root)
    if proc.returncode not in (0, 1) and not proc.stdout:
        return [], f"ast-grep failed: {proc.stderr.strip()[:120]}"
    hits, seen = [], set()
    for line in proc.stdout.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (m["ruleId"], m["file"], m["range"]["start"]["line"])
        if key in seen:  # a nested loop matches once per enclosing loop
            continue
        seen.add(key)
        hits.append({"rule": m["ruleId"], "file": os.path.relpath(Path(root) / m["file"], root),
                     "line": m["range"]["start"]["line"] + 1, "code": m["lines"].strip()[:160]})
    return hits, ""


CONTROL = r"(?!(?:if|for|while|switch|catch|return|new|else|elif|unless|until)\b)"
FN_PATTERNS = [
    re.compile(r"^\s*(?:async\s+)?def\s+(?:self\.)?(\w+[?!]?)"),                       # Python, Ruby
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"),                                      # Go
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)"),  # Rust
    re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+&?(\w+)"),  # PHP
    re.compile(r"^\s*(?:function\s+)?([\w-]+)\s*\(\)\s*\{?\s*$"),                          # shell
    re.compile(r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|override)\s+)+"
               r"[\w<>\[\],.? ]+?\s+" + CONTROL + r"(\w+)\s*\("),                               # Java

    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function|\(|\w+\s*=>)"),
    re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*(?!if\b|for\b|while\b|switch\b|"
               r"catch\b|return\b)(\w+)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{\s*$"),
]


def enclosing_function(lines, line_no):
    """Name of the nearest function header above line_no (1-based). A heuristic, not a parser."""
    for i in range(line_no - 1, -1, -1):
        for pat in FN_PATTERNS:
            m = pat.match(lines[i])
            if m:
                return m.group(1)
    return "(top level)"


def heat(path):
    parts = {p.lower() for p in Path(path).parts[:-1]}
    if parts & COLD_PARTS:
        return "cold"
    if parts & HOT_PARTS:
        return "hot"
    return "warm"


def build_leads(hits, root):
    """Group hits per (rule, file, function), rank them, number them L1..Ln, excerpt the top ones."""
    texts = {}

    def lines_of(f):
        if f not in texts:
            try:
                texts[f] = (Path(root) / f).read_text(errors="replace").splitlines()
            except OSError:
                texts[f] = []
        return texts[f]

    groups = {}
    for h in hits:
        fn = enclosing_function(lines_of(h["file"]), h["line"])
        g = groups.setdefault((h["rule"], h["file"], fn), {"rule": h["rule"], "file": h["file"], "fn": fn,
                                                          "lines": [], "code": h["code"]})
        g["lines"].append(h["line"])
    heat_score = {"hot": 3, "warm": 2, "cold": 1}
    leads = []
    for g in groups.values():
        r = RULES[g["rule"]]
        g["lines"].sort()
        g["heat"] = heat(g["file"])
        g["specialist"] = r["specialist"]
        g["route"], g["check"] = r["route"], r["check"]
        g["score"] = heat_score[g["heat"]] * r["weight"] + min(len(g["lines"]), 5) * 0.2
        leads.append(g)
    leads.sort(key=lambda g: (-g["score"], g["file"], g["lines"][0]))
    leads = leads[:MAX_LEADS]
    for i, g in enumerate(leads, 1):
        g["id"] = f"L{i}"
        if i <= EXCERPTS:
            src = lines_of(g["file"])
            first = g["lines"][0]
            lo, hi = max(1, first - EXCERPT_AROUND), min(len(src), first + EXCERPT_AROUND)
            g["excerpt"] = [f"{n:>5}| {src[n - 1][:110]}" for n in range(lo, hi + 1)]
    return leads


def leads_text(leads, note):
    """The agent's reading list: ranked leads with excerpt, route and check. Read it with head."""
    out = [note] if note else []
    for g in leads:
        r = RULES[g["rule"]]
        where = f"{g['file']}:{','.join(map(str, g['lines'][:6]))}"
        out.append(f"{g['id']} {g['rule']} [{g['specialist']}, {g['heat']}] {where} in {g['fn']}()"
                   + (f" ×{len(g['lines'])}" if len(g["lines"]) > 1 else ""))
        out.append(f"   cost: {r['cost']}")
        out.append(f"   route: {r['route']}")
        out.append(f"   check: {r['check']}")
        out.extend("   " + ln for ln in g.get("excerpt", []))
    return "\n".join(out) + "\n"


def run_scc(paths):
    if not shutil.which("scc"):
        return None
    out = subprocess.run(["scc", "--by-file", "--format", "json", "--no-cocomo", *map(str, paths)],
                         capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def inventory(paths, root):
    langs = run_scc(paths)
    if langs is None:
        return {"languages": [], "files": 0, "code_lines": 0, "hot_files": [], "note": "scc missing: no inventory"}
    files = []
    for lang in langs:
        for f in lang.get("Files", []):
            if any(part in ("node_modules", "dist", "build", "vendor") for part in Path(f["Location"]).parts):
                continue
            files.append({"file": os.path.relpath(Path(f["Location"]).resolve(), root), "lang": lang["Name"],
                          "lines": f["Lines"], "code": f["Code"], "complexity": f["Complexity"]})
    by_lang = Counter()
    for f in files:
        by_lang[f["lang"]] += f["code"]
    return {
        "languages": [{"lang": k, "code": v} for k, v in by_lang.most_common(8)],
        "files": len(files),
        "code_lines": sum(f["code"] for f in files),
        "hot_files": sorted(files, key=lambda f: -f["complexity"])[:10],
    }


def commands(root):
    """How to run tests and benchmarks here, so `apply` can prove a change."""
    out = {"test": None, "bench": []}
    pkg = Path(root) / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        runner = "bun" if (Path(root) / "bun.lockb").exists() or (Path(root) / "bun.lock").exists() else "npm"
        if "test" in scripts:
            out["test"] = f"{runner} run test"
        out["bench"] += [f"{runner} run {k}" for k in scripts if "bench" in k or "perf" in k]
    if any((Path(root) / f).exists() for f in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")) \
            and not out["test"]:
        out["test"] = "python3 -m pytest -q"
    if (Path(root) / "Cargo.toml").exists():
        out["test"] = out["test"] or "cargo test"
        out["bench"].append("cargo bench")
    if (Path(root) / "pom.xml").exists():
        out["test"] = out["test"] or "mvn -q test"
    if (Path(root) / "build.gradle").exists() or (Path(root) / "build.gradle.kts").exists():
        out["test"] = out["test"] or ("./gradlew test" if (Path(root) / "gradlew").exists() else "gradle test")
    if (Path(root) / "Gemfile").exists():
        out["test"] = out["test"] or ("bundle exec rspec" if (Path(root) / "spec").is_dir() else "bundle exec rake test")
    if (Path(root) / "composer.json").exists():
        phpunit = (Path(root) / "vendor" / "bin" / "phpunit").exists()
        out["test"] = out["test"] or ("vendor/bin/phpunit" if phpunit else "composer test")
    if (Path(root) / "go.mod").exists():
        out["test"] = out["test"] or "go test ./..."
        out["bench"].append("go test -bench=. ./...")
    out["tools"] = {t: bool(shutil.which(t)) for t in ("hyperfine", "sg", "scc", "node", "py-spy", "jq")}
    return out


def repo_root(scope):
    r = subprocess.run(["git", "-C", str(scope), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else Path(scope)


def cache_dir(root, scope, target):
    """One folder per scope (and per target, so a targeted run never wipes the overall review)."""
    key = hashlib.sha1(f"{scope}|{target or ''}".encode()).hexdigest()[:8]
    return CACHE / f"{Path(root).name}-{key}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scope")
    ap.add_argument("--target", help="what the targeted run is about, e.g. 'mix export'")
    ap.add_argument("--files", nargs="+", help="only scan these files (the traced flow of the target)")
    a = ap.parse_args()
    scope = Path(a.scope).resolve()
    root = repo_root(scope)
    paths = [Path(f).resolve() for f in a.files] if a.files else [scope]
    hits, note = run_smells(root, paths)
    leads = build_leads(hits, root)
    rules = Counter(g["rule"] for g in leads)
    facts = {
        "scope": os.path.relpath(scope, root),
        "root": str(root),
        "target": a.target,
        "files": [os.path.relpath(p, root) for p in paths] if a.files else None,
        "scanned_at": dt.datetime.now().isoformat(timespec="seconds"),
        "specialists": SPECIALISTS,
        "inventory": inventory(paths, root),
        "rules": {k: {"leads": v, "specialist": RULES[k]["specialist"], "cost": RULES[k]["cost"]}
                  for k, v in rules.most_common()},
        "leads": leads,
        "lead_note": note,
        "commands": commands(root),
    }
    out = cache_dir(root, scope, a.target)
    out.mkdir(parents=True, exist_ok=True)
    for old in ["scout.json", *[p.name for p in out.glob("notes*.json")]]:  # a new scan starts a new review
        (out / old).unlink(missing_ok=True)
    shutil.rmtree(out / "verified", ignore_errors=True)
    (out / "facts.json").write_text(json.dumps(facts, indent=1))
    (out / "leads.txt").write_text(leads_text(leads, note))
    print(out / "facts.json")


if __name__ == "__main__":
    main()
