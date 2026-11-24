export const meta = {
  name: 'code-performance',
  description: 'DEEP mode of code-performance (opt-in, ~13 agents): scout, specialists, adversarial verify',
  whenToUse: 'Run by the code-performance skill after scan.py wrote facts.json',
  phases: [
    { title: 'Plan', detail: '1 scout maps hot paths and assigns specialists' },
    { title: 'Find', detail: 'up to 6 specialists, each in its own scope' },
    { title: 'Verify', detail: 'one skeptic per specialist tries to refute and measures' },
  ],
}

// args: { facts, dir, scope, root, goal, target?, focus?: string[] }
// goal = the run's one-paragraph acceptance spec, written by the main session before launch.
// Every stage has a written acceptance spec (DONE WHEN) so agents stop at "done", not "almost".
// At most MAX_PARALLEL agents run at once, across all stages; at most 1 + 2*MAX_SPECIALISTS in total.
const MAX_PARALLEL = 5
const MAX_SPECIALISTS = 6
const MAX_PROPOSALS = 6

const A = args || {}
if (!A.facts || !A.dir || !A.goal) throw new Error('args.facts, args.dir (from scan.py) and args.goal are required')
const GOAL = `RUN GOAL (acceptance spec for this run): ${A.goal}`

let active = 0
const waiting = []
async function limited(run) {
  while (active >= MAX_PARALLEL) await new Promise((wake) => waiting.push(wake))
  active++
  try {
    return await run()
  } finally {
    active--
    const next = waiting.shift()
    if (next) next()
  }
}

const TOOLS = 'USE ONLY TOKEN-SAVING TOOLS per root CLAUDE.md: CodeMapper (outline/expand/path), jg, sg, rg before ' +
  'reading; read only the lines you cite; jq on facts.json, never cat it whole; scoped shell output.'
const NEVER_EDIT = 'Never edit, create or delete files inside the repo. Scratch work goes under ' + A.dir + '.'
const SAME = 'Same functionality means: same return values and output bytes, same order, same errors and ' +
  'rejections, same side effects in the same order, same public API. A change that alters any of these is ' +
  'NOT a proposal (a bug you notice may be mentioned in `searched` as "bug, out of scope").'

const SPECIALISTS = {
  algo: {
    name: 'algorithms & data structures',
    playbook: [
      'linear lookup inside a loop (includes/indexOf/find/`x in list`) → build a Set/Map once outside it',
      'nested loops joining two lists by key → index one side by key (hash join)',
      'sort per item, or a full sort to get min/max/top-k → sort once outside, single pass, or a heap',
      'same pure value recomputed each iteration → hoist it out of the loop',
      'array.shift() as a queue, O(n²) dedupe, reduce with spread → index pointer, Set, push',
      'regex compiled per item → compile once; Python str += in big loops → "".join',
    ],
  },
  io: {
    name: 'file I/O, async & concurrency',
    playbook: [
      'independent awaits run one by one → Promise.all/asyncio.gather with a concurrency bound; result order kept;' +
        ' state how the error behaviour stays the same (first failure rejects in both?)',
      'sync fs/exec (readFileSync, execSync) on a request or per-item path → async version (startup-only use is fine)',
      'whole file read into memory when it is processed line by line or piped on → stream it',
      'exists/stat check before open → open and handle the error (also removes a race)',
      'many tiny writes → one buffered write; polling with sleep → fs.watch/event',
    ],
  },
  data: {
    name: 'database & network calls',
    playbook: [
      'one query per item (N+1) → one IN query, a join, or a batch loader',
      'one HTTP or bridge call per item (fetch, axios, CEP evalScript/ExtendScript, IPC) → one batched call,' +
        ' or bounded parallel; reuse keep-alive agents/clients',
      'same request repeated within one operation → reuse the first result (request-scoped memo)',
      'row-by-row inserts/updates or a transaction per row → bulk insert, one transaction',
      'SELECT * / unused fields, filters or sorts without an index (cite the schema) → narrow select, index',
    ],
  },
  proc: {
    name: 'subprocesses & media pipelines',
    playbook: [
      'ffprobe/ffmpeg or another tool spawned per item where one call can do several (multiple outputs, -map,' +
        ' filter_complex split, one ffprobe for all streams) → one process',
      'the same input decoded or probed more than once → decode/probe once and reuse',
      'intermediate files between steps → pipes, when the output stays byte-identical',
      'shelling out for what the language does natively (cat, ls, grep, cp via exec) → native API',
      'independent jobs run sequentially → bounded parallel (CPU count)',
      're-encode where stream copy (-c copy) yields the same result: only when there is no filter and no cut' +
        ' off a keyframe; anything that changes output bytes (preset, threads, codec) is NOT the same functionality',
    ],
  },
  mem: {
    name: 'memory, copies & caching',
    playbook: [
      'deep clone (JSON round-trip, deepcopy, structuredClone) where nothing mutates the copy → shallow copy or none;' +
        ' prove no mutation by reading every use',
      'spread/concat building a new array or object per item → mutate a local accumulator',
      'Buffer.concat or slice copies in a loop → collect then concat once; subarray views',
      'pure function called repeatedly with the same arguments on a hot path → memoize (prove purity)',
      'whole dataset loaded to count/filter/sum → iterate or stream; numeric arrays in DSP → typed arrays',
      'caches that never evict → bounded LRU (results stay the same, only recompute cost changes)',
    ],
  },
  startup: {
    name: 'startup, imports, bundle & build',
    playbook: [
      'heavy module imported at top level but used only by one rare path → lazy/dynamic import',
      'work done at module load (reading files, building tables) → lazy init on first use or build-time precompute',
      'whole-library imports (lodash, moment, icon packs) → per-function imports or native API',
      'dev-only code in the production bundle, no route code-splitting → split, tree-shake',
      'measure: hyperfine on the CLI start, `node --cpu-prof`, `python -X importtime`',
    ],
  },
  ui: {
    name: 'frontend rendering & reactivity',
    playbook: [
      'deep watcher → watch the one field that matters; large data made deeply reactive → shallowRef/markRaw',
      'heavy method called from the template → computed (cached)',
      'v-for with index keys or v-if on the same element; React list without stable keys → stable keys, filter first',
      'expensive child re-rendered on every parent change → memo/computed props; inline object props recreated',
      'layout read after write inside a loop (layout thrash) → batch reads then writes',
      'scroll/resize/input handlers doing heavy work per event → requestAnimationFrame batching',
    ],
  },
}

const PROPOSAL = {
  type: 'object',
  properties: {
    id: { type: 'string', description: 'specialist key + number, e.g. io1, algo3' },
    title: { type: 'string', description: '≤60 chars, plain words: what gets faster' },
    where: { type: 'string', description: 'repo-relative file:start-end of the code to change' },
    now: { type: 'string', description: '≤70 chars: what the code does now and why it is slow' },
    change: { type: 'string', description: '≤70 chars: the faster route, naming the API or structure' },
    why_same: { type: 'string', description: '≤90 chars: observable behaviour kept and the condition it relies on' },
    gain: { type: 'string', description: '≤40 chars, e.g. "O(n·m) → O(n+m)", "50 spawns → 1"' },
    gain_class: { enum: ['high', 'med', 'low'] },
    hot: { enum: ['hot', 'warm', 'cold', 'unknown'], description: 'how often this code runs' },
    effort: { enum: ['S', 'M', 'L'] },
    risk: { type: 'string', description: '≤60 chars: what could break' },
    smells: { type: 'array', items: { type: 'string' }, description: 'smell ids from facts.json it covers' },
  },
  required: ['id', 'title', 'where', 'now', 'change', 'why_same', 'gain', 'gain_class', 'hot', 'effort', 'risk'],
}

const SCOUT_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string', description: '≤120 chars: where this code spends its time' },
    hot_paths: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          entry: { type: 'string', description: 'file:line of the route, command, handler or loop' },
          why_hot: { type: 'string', description: '≤70 chars: per request / per item / per frame / startup' },
          steps: { type: 'array', items: { type: 'string' }, description: 'file:line fn() down to the heavy work' },
        },
        required: ['name', 'entry', 'why_hot', 'steps'],
      },
    },
    assignments: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          specialist: { enum: Object.keys(SPECIALISTS) },
          brief: { type: 'string', description: '≤300 chars: what to look at and why' },
          focus: { type: 'array', items: { type: 'string' }, description: 'repo-relative files or file:line' },
        },
        required: ['specialist', 'brief', 'focus'],
      },
    },
    skipped: {
      type: 'array',
      items: {
        type: 'object',
        properties: { specialist: { type: 'string' }, reason: { type: 'string' } },
        required: ['specialist', 'reason'],
      },
    },
  },
  required: ['summary', 'hot_paths', 'assignments', 'skipped'],
}

const FIND_SCHEMA = {
  type: 'object',
  properties: {
    searched: { type: 'array', items: { type: 'string' }, description: '≤70 chars each: what you checked, incl. dismissed leads' },
    proposals: { type: 'array', items: PROPOSAL },
  },
  required: ['searched', 'proposals'],
}

const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    file: { type: 'string' },
    confirmed: { type: 'number' },
    unsure: { type: 'number' },
    rejected: { type: 'number' },
  },
  required: ['file', 'confirmed', 'unsure', 'rejected'],
}

const focusLine = A.focus && A.focus.length ? `Only these specialists may be assigned: ${A.focus.join(', ')}.` : ''
const targetLine = A.target
  ? `TARGETED RUN: the user wants "${A.target}" faster. Find it, trace its flow, and assign only specialists ` +
    'that have something to look at inside that flow. Hot paths = that flow and what it calls.'
  : 'OVERALL RUN: cover the whole scope; rank hot paths by how often they run.'

// ── Stage 1: Plan ────────────────────────────────────────────────────────────
phase('Plan')
const scout = await limited(() => agent(
  `${TOOLS}\nYou are the scout of a performance review of ${A.root} (scope: ${A.scope}). Goal: find where this code spends ` +
  `its time and hand each specialist a narrow, concrete scope.\n${GOAL}\n${targetLine}\n${focusLine}\n\n` +
  `Inputs: ${A.facts} (jq '.inventory', '.rules', '.commands') and ${A.dir}/leads.txt (ranked leads with excerpts; read with head).\n` +
  `Specialists: ${Object.entries(SPECIALISTS).map(([k, s]) => `${k} = ${s.name}`).join('; ')}.\n\n` +
  'Do:\n1. Find entry points (routes, CLI commands, handlers, workers, render loops, per-file batch loops) with ' +
  'rg/sg/CodeMapper, and the heaviest work under them (complexity hotspots, smell clusters, spawns, queries).\n' +
  '2. Pick hot paths: what runs per request, per item, per frame, or at every start.\n' +
  `3. Assign up to ${MAX_SPECIALISTS} specialists. Each gets a brief and focus files on or near the hot paths. ` +
  'A specialist with no leads in this code goes in `skipped` with the reason.\n' +
  `4. Write your returned object as JSON to ${A.dir}/scout.json.\n\n` +
  'DONE WHEN (acceptance):\n- 1–5 hot paths, each with a real entry file:line you opened, why_hot, and steps down to the work.\n' +
  `- Every assignment's focus files exist; briefs say what to look at, not "review everything".\n` +
  `- Every specialist key appears in assignments or skipped exactly once; at most ${MAX_SPECIALISTS} assigned.\n` +
  `- ${A.dir}/scout.json written.\nNothing invented: if you could not find an entry, say so in summary.\n\n` +
  NEVER_EDIT,
  { label: 'scout', phase: 'Plan', schema: SCOUT_SCHEMA },
))
if (!scout) throw new Error('scout returned nothing; rerun or check the transcript')

const seen = new Set()
let assignments = scout.assignments.filter((a) => {
  if (!SPECIALISTS[a.specialist] || seen.has(a.specialist)) return false
  if (A.focus && A.focus.length && !A.focus.includes(a.specialist)) return false
  seen.add(a.specialist)
  return true
})
if (assignments.length > MAX_SPECIALISTS) {
  log(`dropped specialists over the cap: ${assignments.slice(MAX_SPECIALISTS).map((a) => a.specialist).join(', ')}`)
  assignments = assignments.slice(0, MAX_SPECIALISTS)
}
log(`hot paths: ${scout.hot_paths.length} · specialists: ${assignments.map((a) => a.specialist).join(', ') || 'none'}`)
const hotPaths = scout.hot_paths.map((h) => `- ${h.name}: ${h.entry} (${h.why_hot}) → ${h.steps.join(' → ')}`).join('\n')

// ── Stage 2: Find → Stage 3: Verify, per specialist (pipeline, shared cap) ────
const results = await pipeline(
  assignments,
  (a) => limited(() => agent(
    `${TOOLS}\nYou are the ${SPECIALISTS[a.specialist].name} specialist in a performance review of ${A.root} ` +
    `(scope: ${A.scope}). Goal: find faster routes to the SAME result in existing code.\n${GOAL}\n\n` +
    `Your brief: ${a.brief}\nFocus: ${a.focus.join(', ')}\nHot paths:\n${hotPaths}\n\n` +
    `Your leads (ranked, with excerpt, route and check): rg -A11 '\\[${a.specialist}, ' ${A.dir}/leads.txt | head -300\n` +
    `Patterns you own (slow → faster):\n- ${SPECIALISTS[a.specialist].playbook.join('\n- ')}\n` +
    'Also look beyond the smell list and the playbook inside your focus: anything in your specialty that does ' +
    'more work than the result needs.\n\n' + SAME + '\n\n' +
    'DONE WHEN (acceptance):\n- Every lead on your focus files and hot paths is looked at (up to the 30 hottest); ' +
    '`searched` lists what you checked, including leads you dismissed and why (≤70 chars each).\n' +
    `- 0–${MAX_PROPOSALS} proposals, best first, ids ${a.specialist}1..${a.specialist}${MAX_PROPOSALS}. Each \`where\` ` +
    'is a file:start-end you read; `now`/`change` name the concrete API or structure; `why_same` names what stays ' +
    'identical and the condition it relies on (e.g. "ids are unique", "callers never mutate the result").\n' +
    '- Cold-path proposals only when the gain grows with data size (e.g. O(n²) on user data).\n' +
    '- Zero proposals is a valid answer when nothing qualifies. No style or readability changes.\n\n' +
    NEVER_EDIT,
    { label: `find:${a.specialist}`, phase: 'Find', schema: FIND_SCHEMA },
  )),
  (found, a) => {
    if (!found) return null
    const proposals = found.proposals.slice(0, MAX_PROPOSALS)
    const file = `${A.dir}/verified/${a.specialist}.json`
    return limited(() => agent(
      `${TOOLS}\nYou are a skeptic verifying ${proposals.length} performance proposals for ${A.root}. Your job is to ` +
      'REFUTE each one; confirm only what survives. Default to rejected when same behaviour cannot be shown.\n\n' +
      SAME + `\n\nProposals (JSON):\n${JSON.stringify(proposals)}\n\n` +
      'For each proposal:\n1. Read the code at `where` and every caller of it you can find (CodeMapper expand, rg).\n' +
      '2. Attack `why_same`: empty input, duplicates, ordering, thrown errors and rejections, partial failure, ' +
      'concurrency limits and rate limits, mutation and aliasing of shared objects, float and text encoding, ' +
      'side effects that other code relies on (logs, files, events).\n' +
      '3. Check the gain is real: is the path actually hot, is n large enough to matter?\n' +
      `4. Measure when a micro-benchmark takes under 2 minutes to build: copy the slow and fast versions into ` +
      `${A.dir}/bench/<id>/ (never the repo), run realistic sizes with hyperfine or a timing loop of at least 5 ` +
      'runs, and record mean before/after. Otherwise measured = null and say why in verify_note.\n\n' +
      `Write ${file} as JSON: {"specialist":"${a.specialist}","searched":<the list below>,"proposals":[each ` +
      'proposal with its original fields plus "verdict":"confirmed|unsure|rejected", "verify_note":"≤90 chars citing ' +
      'what you read or ran", "measured":{"before":"12.4 ms","after":"1.1 ms","how":"hyperfine, n=10k, 10 runs"}|null}]}.\n' +
      `searched: ${JSON.stringify(found.searched)}\n\n` +
      'DONE WHEN (acceptance):\n- Every proposal has a verdict and a verify_note citing a line you read or a run you did.\n' +
      `- ${file} is written and parses as JSON (check with jq).\n` +
      '- `git status` in the repo is unchanged by you.\nReturn the file path and the verdict counts.\n\n' +
      NEVER_EDIT,
      { label: `verify:${a.specialist}`, phase: 'Verify', schema: VERIFY_SCHEMA },
    ))
  },
)

const done = results.filter(Boolean)
const missing = assignments.filter((_, i) => !results[i]).map((a) => a.specialist)
if (missing.length) log(`no result from: ${missing.join(', ')}`)
return {
  dir: A.dir,
  summary: scout.summary,
  specialists: done.map((r) => ({ file: r.file, confirmed: r.confirmed, unsure: r.unsure, rejected: r.rejected })),
  skipped: scout.skipped,
  missing,
}
