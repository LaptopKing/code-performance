"""Tests for scan.py. Run: python3 -m unittest test_scan (from scripts/). Needs ast-grep (sg)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import scan

SLOW_TS = """
async function f(ids: string[], all: string[]) {
  for (const id of ids) {
    const r = await fetch(id);
    if (all.includes(id)) console.log(r);
  }
  ids.forEach(async (x) => { await db.query(x) });
  const c = JSON.parse(JSON.stringify(ids));
}
for (const v of all.filter(Boolean).sort()) { console.log(v) }
export const w = { watch: { items: { handler() {}, deep: true } } };
function k(rows: any[]) { return rows.reduce((acc, r) => [...acc, r], []); }
"""

FAST_TS = """
async function f(ids: string[], all: string[]) {
  const known = new Set(all);
  const rows = await Promise.all(ids.map((id) => fetch(id)));
  return rows.filter((_, i) => known.has(ids[i]));
}
"""

SLOW_PY = """
import subprocess, re, copy
def g(files):
    for f in files:
        subprocess.run(["ffprobe", f])
        pat = re.compile(f)
    for m in sorted(files):
        print(m)
    return copy.deepcopy(files)
"""

SLOW_GO = """package main
func probe(files []string, known []string) {
	for _, f := range files {
		exec.Command("ffprobe", f).Run()
		re := regexp.MustCompile(f)
		_ = slices.Contains(known, f)
		fh, _ := os.Open(f)
		defer fh.Close()
		_ = re
	}
	for _, k := range sortedKeys(known) {
		fmt.Println(k)
	}
}
"""

SLOW_RS = """fn probe(files: Vec<String>, known: Vec<String>) {
    for f in files.iter() {
        std::process::Command::new("ffprobe").arg(f).output();
        let re = Regex::new(f);
        let ok = known.contains(f);
        let copy = known.clone();
    }
    for k in known.clone() {
        println!("{}", k);
    }
}
"""

SLOW_JAVA = """class A {
    void probe(List<String> files, List<String> known) throws Exception {
        for (String f : files) {
            new ProcessBuilder("ffprobe", f).start();
            Pattern p = Pattern.compile(f);
            boolean ok = known.contains(f);
            stmt.executeQuery("select 1");
        }
        files.forEach(f -> Collections.sort(known));
    }
}
"""

SLOW_RB = """def probe(files, known)
  files.each do |f|
    system("ffprobe", f)
    User.find_by(name: f)
    known.include?(f)
  end
  for k in known.sort
    puts k
  end
end
"""

SLOW_PHP = """<?php
function probe($files, $known, $db) {
    $all = [];
    foreach ($files as $f) {
        shell_exec("ffprobe " . $f);
        in_array($f, $known);
        $all = array_merge($all, [$f]);
        $db->query("select 1");
    }
}
"""

SLOW_SH = """probe_all() {
  for f in *.wav; do
    ffprobe "$f"
    curl -s "http://x/$f"
    echo "$f"
  done
}
"""

SLOW_VUE = """<template><div/></template>
<script setup lang="ts">
async function h(xs) {
  for (const x of xs) { await load(x) }
}
</script>
"""


@unittest.skipUnless(shutil.which("sg"), "ast-grep (sg) not installed")
class SmellTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="perf-scan-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        files = {"slow.ts": SLOW_TS, "fast.ts": FAST_TS, "slow.py": SLOW_PY, "c.vue": SLOW_VUE, "slow.go": SLOW_GO,
                 "slow.rs": SLOW_RS, "Slow.java": SLOW_JAVA, "slow.rb": SLOW_RB, "slow.php": SLOW_PHP,
                 "slow.sh": SLOW_SH, "slow_test.go": SLOW_GO}
        for name, body in files.items():
            (self.root / name).write_text(body)
        (self.root / "slow.test.ts").write_text(SLOW_TS)  # tests are not a target
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "dep.js").write_text("for (;;) { await x() }")
        hits, note = scan.run_smells(self.root, [self.root])
        self.assertEqual(note, "")
        self.found = {(h["rule"], h["file"], h["line"]) for h in hits}

    def test_every_planted_smell_is_found(self):
        want = {
            ("await-in-loop", "slow.ts", 4), ("http-in-loop", "slow.ts", 4),
            ("lookup-in-loop", "slow.ts", 5), ("await-in-loop", "slow.ts", 7),
            ("query-in-loop", "slow.ts", 7), ("deep-copy", "slow.ts", 8),
            ("deep-watch", "slow.ts", 11), ("copy-in-loop", "slow.ts", 12),
            ("spawn-in-loop", "slow.py", 5), ("regex-in-loop", "slow.py", 6),
            ("deep-copy", "slow.py", 9), ("await-in-loop", "c.vue", 4),
        }
        self.assertEqual(want - self.found, set())

    def test_every_language_code_audit_covers_has_leads(self):
        want = {
            ("spawn-in-loop", "slow.go", 4), ("regex-in-loop", "slow.go", 5), ("lookup-in-loop", "slow.go", 6),
            ("defer-in-loop", "slow.go", 8),
            ("spawn-in-loop", "slow.rs", 3), ("regex-in-loop", "slow.rs", 4), ("lookup-in-loop", "slow.rs", 5),
            ("clone-in-loop", "slow.rs", 6),
            ("spawn-in-loop", "Slow.java", 4), ("regex-in-loop", "Slow.java", 5),
            ("lookup-in-loop", "Slow.java", 6), ("query-in-loop", "Slow.java", 7), ("sort-in-loop", "Slow.java", 9),
            ("spawn-in-loop", "slow.rb", 3), ("query-in-loop", "slow.rb", 4), ("lookup-in-loop", "slow.rb", 5),
            ("spawn-in-loop", "slow.php", 5), ("lookup-in-loop", "slow.php", 6), ("copy-in-loop", "slow.php", 7),
            ("query-in-loop", "slow.php", 8),
            ("spawn-in-loop", "slow.sh", 3), ("http-in-loop", "slow.sh", 4),
        }
        self.assertEqual(want - self.found, set())
        others = {f for f in self.found if f[1].split(".")[-1] in ("go", "rs", "java", "rb", "php", "sh")}
        self.assertEqual(others - want, set(), "loop headers, builtins like echo, and _test.go stay quiet")

    def test_fast_code_and_node_modules_stay_quiet(self):
        noisy = {f for f in self.found if f[1] == "fast.ts"}
        # Promise.all(ids.map(fetch)) is a fetch inside a map callback: the one accepted lead in fast.ts
        self.assertEqual({f[0] for f in noisy}, {"http-in-loop"})
        self.assertFalse(any("node_modules" in f[1] for f in self.found))
        self.assertFalse(any(f[1] == "slow.test.ts" for f in self.found))

    def test_loop_header_work_runs_once_and_is_not_a_lead(self):
        self.assertNotIn(("lookup-in-loop", "slow.ts", 10), self.found)
        self.assertNotIn(("sort-in-loop", "slow.ts", 10), self.found)
        self.assertNotIn(("sort-in-loop", "slow.py", 7), self.found)

    def test_each_hit_is_counted_once_per_line(self):
        hits, _ = scan.run_smells(self.root, [self.root])
        keys = [(h["rule"], h["file"], h["line"]) for h in hits]
        self.assertEqual(len(keys), len(set(keys)))


class LeadTest(unittest.TestCase):
    def test_hits_in_one_function_become_one_ranked_lead_with_excerpt(self):
        root = Path(tempfile.mkdtemp(prefix="perf-leads-"))
        (root / "routes").mkdir()
        (root / "routes" / "a.ts").write_text(
            "export async function load(ids) {\n  for (const id of ids) {\n    await get(id);\n"
            "    await put(id);\n  }\n}\n")
        (root / "scripts").mkdir()
        (root / "scripts" / "b.py").write_text("import copy\ndef once(x):\n    return copy.deepcopy(x)\n")
        hits = [{"rule": "await-in-loop", "file": "routes/a.ts", "line": 3, "code": "await get(id);"},
                {"rule": "await-in-loop", "file": "routes/a.ts", "line": 4, "code": "await put(id);"},
                {"rule": "deep-copy", "file": "scripts/b.py", "line": 3, "code": "copy.deepcopy(x)"}]
        leads = scan.build_leads(hits, root)
        self.assertEqual([g["id"] for g in leads], ["L1", "L2"])
        first = leads[0]
        self.assertEqual((first["rule"], first["fn"], first["lines"], first["heat"]),
                         ("await-in-loop", "load", [3, 4], "hot"))
        self.assertTrue(any("await get(id)" in ln for ln in first["excerpt"]))
        self.assertEqual((leads[1]["fn"], leads[1]["heat"]), ("once", "cold"))
        text = scan.leads_text(leads, "")
        self.assertIn("L1 await-in-loop [io, hot] routes/a.ts:3,4 in load() ×2", text)
        self.assertIn("route: run the items concurrently (Promise.all", text)

    def test_every_rule_names_a_known_specialist_and_has_route_and_check(self):
        for rid, r in scan.RULES.items():
            self.assertIn(r["specialist"], scan.SPECIALISTS, rid)
            self.assertTrue(r["route"] and r["check"], rid)


if __name__ == "__main__":
    unittest.main()
