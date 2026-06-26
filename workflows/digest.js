export const meta = {
  name: 'digest',
  description: 'Summarize changed Claude Code conversations into the recall index',
  phases: [
    { title: 'Prep', detail: 'enumerate changed convos, strip + tier (prepare.py)' },
    { title: 'Summarize', detail: 'one passive Read-only agent per whole-tier convo → 6-field record; gist tightener' },
    { title: 'Sample', detail: 'over-cap convos: convo-sampler reads a downsampled view, expands gaps via expand.py under a token budget → 6-field record' },
    { title: 'Index', detail: 'parallel chunk-writes → one deterministic index.py merge; advance change-detector' },
  ],
}

// ============================================================================
// .claude/workflows/digest.js — the nightly/catch-up summarization pass (SPEC §4, §7).
// Registered as the NAMED workflow `digest` (meta.name) so it runs unattended; a
// dynamic scriptPath invocation would hit a "review before running" gate headless.
//
//   Prep      : digest-runner runs prepare.py → list of changed convos (work files).
//   Summarize : per whole-tier convo, convo-summarizer (Read-only) returns the 6
//               §4.7 fields; a gist tightener re-runs any over-budget gist.
//   Sample    : per over-cap convo (~5%), convo-sampler (Read + scoped Bash) reads
//               the downsampled view prepare.py wrote, optionally reveals hidden
//               exchanges via expand.py (which enforces a hard token cap), and
//               returns the same 6 fields. Same gist tightener.
//   Index     : the validated records are written in small PARALLEL chunks (no single
//               agent re-serializes the whole batch — that was a slow, lossy serial
//               step), then ONE `index.py --batch-glob` merge reads them all, builds
//               lean records, merges the store, and advances the change-detector ONLY
//               after each record is written (§4.5).
//
// The orchestrator never reads conversation content (no fs access); each agent reads
// exactly the file(s) it is pointed at. expand.py does all gap extraction so no
// exchange text ever crosses a workflow stage — only paths and exchange indices.
// ============================================================================

const A = (typeof args === 'string' ? JSON.parse(args) : args) || {}
// SRC is the convo-digest plugin's src/ dir. The plugin ships this file with the
// __CONVO_DIGEST_SRC__ placeholder; the freshness hook resolves it to the real
// <plugin>/src when it installs this workflow to ~/.claude/workflows/ (baked in at
// install time, so it works even if the digest skill forgets to pass args.src).
const SRC   = A.src   || '__CONVO_DIGEST_SRC__'
if (SRC === ['__CONVO', 'DIGEST_SRC__'].join('_')) throw new Error(
  'digest workflow: src path unresolved — reinstall the convo-digest plugin so the ' +
  'freshness hook can bake the path in, or invoke with args.src=<plugin>/src.')
const HOME  = A.home  || '~/.claude/digest'          // bash expands ~ in the runner
const WORK  = A.work  || `${HOME}/work`
const INDEX = A.index || `${HOME}/index.json`   // also the change-detector (provenance.last_ts per record)
// The Index step's batch file is the ONLY thing an agent writes with the Write tool,
// which is blocked under ~/.claude (protected dir) and outside cwd. So it lands in the
// session cwd, NOT ${WORK} (=~/.claude/digest/work); index.py reads it (python3, which
// can touch ~/.claude) and --cleanup unlinks it after. cwd-relative → works for both
// the dev repo and a plugin user, wherever the job is launched.
const BATCH_PREFIX = A.batchPrefix || '_digest_batch_'   // chunk files: _digest_batch_<i>.json
const CHUNK = A.chunk || 6                                // records per chunk write (small → reliable)
const MODEL = A.model || 'haiku'
const LIMIT = A.limit || 20                           // whole-tier convos per run (batched draining)
// Custom agent types resolve from .claude/agents at session start. Overridable so a
// mid-session validation run can fall back to built-ins (which don't need a reload).
const SUMMARIZER_AGENT = A.summarizerAgent || 'convo-summarizer'
const SAMPLER_AGENT = A.samplerAgent || 'convo-sampler'
const RUNNER_AGENT = A.runnerAgent || 'digest-runner'
const GIST_MAX_WORDS = A.gistMaxWords || 70          // target ~60; re-summarize above this

const SUMMARY_SCHEMA = {
  type: 'object',
  properties: {
    title: { type: 'string' },
    topics: { type: 'array', items: { type: 'string' }, maxItems: 4 },
    gist: { type: 'string' },
    status: { type: 'string', enum: ['solved', 'unresolved', 'exploratory', 'abandoned'] },
    unresolved: { type: ['string', 'null'] },
    key_entities: { type: 'array', items: { type: 'string' }, maxItems: 8 },
  },
  required: ['title', 'topics', 'gist', 'status', 'unresolved', 'key_entities'],
}

const PREP_SCHEMA = {
  type: 'object',
  properties: {
    convos: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          key: { type: 'string' }, id: { type: 'string' }, project: { type: 'string' },
          source: { type: 'string' }, work_path: { type: 'string' },
          view_path: { type: ['string', 'null'] },
          tier: { type: 'string' }, tokens: { type: 'integer' },
        },
        required: ['key', 'work_path', 'tier'],
      },
    },
    counts: { type: 'object' },
  },
  required: ['convos'],
}

const INDEX_RESULT_SCHEMA = {
  type: 'object',
  properties: {
    written: { type: 'integer' },
    index_size: { type: 'integer' },
    failed: { type: 'array', items: { type: 'object' } },
  },
  required: ['written'],
}

function wordCount(s) { return (s || '').trim().split(/\s+/).filter(Boolean).length }

// --- Prep -------------------------------------------------------------------
phase('Prep')
const prep = await agent(
  `Run this EXACT command and return its stdout JSON (it prints one JSON object):\n` +
  `  python3 ${SRC}/prepare.py --work ${WORK} --index ${INDEX} --limit ${LIMIT}\n` +
  `Return the parsed object unchanged.`,
  { schema: PREP_SCHEMA, agentType: RUNNER_AGENT, model: MODEL, label: 'prepare', phase: 'Prep' }
)
const all = (prep && prep.convos) || []
const whole = all.filter(c => c.tier === 'whole')
const sampled = all.filter(c => c.tier === 'sample')
const trivial = all.filter(c => c.tier === 'trivial')
if (trivial.length) log(`${trivial.length} convo(s) under token floor → trivial (skipped — recall noise)`)
log(`Prep: ${all.length} changed, ${whole.length} whole-tier, ${sampled.length} over-cap (sampler)`)
if (!whole.length && !sampled.length) { log('nothing to summarize'); return { summarized: 0, indexed: 0 } }

// gist tightener — shared stage 2 for both tiers. `src` is the file the agent can
// re-read if it needs to (the work file for whole, the view file for sampled).
const tightenStage = (agentType, phaseName) => async (item, c) => {
  if (!item || wordCount(item.summary.gist) <= GIST_MAX_WORDS) return item
  const src = c.view_path || c.work_path
  const tighter = await agent(
    `The gist in this summary is too long (${wordCount(item.summary.gist)} words; ` +
    `target ≤60). Rewrite ONLY the gist as ≤60 words of prose with the same meaning; ` +
    `keep every other field identical. Source if needed: ${src}\n\n` +
    `CURRENT SUMMARY:\n${JSON.stringify(item.summary)}`,
    { schema: SUMMARY_SCHEMA, agentType, model: MODEL,
      label: `tighten:${(c.id || c.key).slice(0, 8)}`, phase: phaseName }
  )
  return tighter ? { ...item, summary: tighter } : item
}

// --- Summarize whole-tier (+ gist tightener) -------------------------------
let wholeOk = []
if (whole.length) {
  phase('Summarize')
  const results = await pipeline(
    whole,
    c => agent(
      `Summarize the conversation in this work file: ${c.work_path}`,
      { schema: SUMMARY_SCHEMA, agentType: SUMMARIZER_AGENT, model: MODEL,
        label: `sum:${(c.id || c.key).slice(0, 8)}`, phase: 'Summarize' }
    ).then(s => (s ? { key: c.key, work_path: c.work_path, summary: s } : null)),
    tightenStage(SUMMARIZER_AGENT, 'Summarize')
  )
  wholeOk = results.filter(x => x && x.summary)
  log(`Summarize: ${wholeOk.length}/${whole.length} produced records`)
}

// --- Sample over-cap convos (+ gist tightener) -----------------------------
let sampleOk = []
if (sampled.length) {
  phase('Sample')
  const results = await pipeline(
    sampled,
    c => agent(
      `Summarize this over-cap conversation from its downsampled view file: ${c.view_path}\n` +
      `Read the view first; the kept exchanges usually suffice. Expand a gap (per the ` +
      `view's 'expand' instructions) ONLY if needed to capture the outcome or a pivotal ` +
      `decision, then Read the view again. If expand reports 'budget_exhausted', ` +
      `summarize immediately. Never read the full conversation file directly.`,
      { schema: SUMMARY_SCHEMA, agentType: SAMPLER_AGENT, model: MODEL,
        label: `sample:${(c.id || c.key).slice(0, 8)}`, phase: 'Sample' }
    ).then(s => (s ? { key: c.key, work_path: c.work_path, summary: s } : null)),
    tightenStage(SAMPLER_AGENT, 'Sample')
  )
  sampleOk = results.filter(x => x && x.summary)
  log(`Sample: ${sampleOk.length}/${sampled.length} produced records`)
}

const ok = [...wholeOk, ...sampleOk]
if (!ok.length) return { summarized: 0, indexed: 0 }

// --- Index (parallel chunk-writes → one deterministic merge) ----------------
// Records are already produced (validated) by the parallel summarizers. The old
// design had ONE agent re-serialize the whole array via Write — a serial ~10-15K
// *output*-token step (slow + the pricey token kind) that also silently dropped
// entries on big batches. Instead: split into small chunks, write them in PARALLEL
// (each agent re-emits only a few records → fast + reliable), then a single Python
// merge reads them all with ZERO re-transcription. A mangled chunk just self-heals
// next run (its convos' change-detector never advanced).
phase('Index')
const chunks = []
for (let i = 0; i < ok.length; i += CHUNK) chunks.push(ok.slice(i, i + CHUNK))
const CHUNK_SCHEMA = { type: 'object',
  properties: { path: { type: 'string' }, count: { type: 'integer' } }, required: ['count'] }
const writes = await parallel(chunks.map((chunk, i) => () => agent(
  `Write this EXACT JSON array, verbatim and complete, to the file ${BATCH_PREFIX}${i}.json ` +
  `in the current working directory using the Write tool (do NOT write under ~/.claude — ` +
  `it's protected). Return {"path","count"} where count is the number of array elements ` +
  `you wrote.\n\nARRAY (${chunk.length} elements):\n${JSON.stringify(chunk)}`,
  { schema: CHUNK_SCHEMA, agentType: RUNNER_AGENT, model: MODEL,
    label: `batch:${i}`, phase: 'Index' }
)))
const wroteCount = writes.filter(Boolean).reduce((n, w) => n + (w.count || 0), 0)
if (wroteCount < ok.length) log(`Index: chunk-writes recorded ${wroteCount}/${ok.length} (merge will reconcile)`)

const idx = await agent(
  `Run this single command and return its stdout JSON (one object):\n` +
  `  python3 ${SRC}/index.py --batch-glob '${BATCH_PREFIX}*.json' --index ${INDEX} --model haiku-4-5 --cleanup\n` +
  `Return the parsed object unchanged.`,
  { schema: INDEX_RESULT_SCHEMA, agentType: RUNNER_AGENT, model: MODEL, label: 'merge', phase: 'Index' }
)
log(`Index: merged ${idx && idx.written}/${ok.length} records → ${INDEX} (size ${idx && idx.index_size})`)
return { summarized: ok.length, indexed: (idx && idx.written) || 0,
         failed: (idx && idx.failed) || [] }
