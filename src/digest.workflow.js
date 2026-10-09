export const meta = {
  name: 'digest',
  description: 'Summarize changed Claude Code conversations into the recall index',
  phases: [
    { title: 'Prep', detail: 'enumerate changed convos, strip + tier (prepare.py)' },
    { title: 'Summarize', detail: 'one passive Read-only agent per whole-tier convo → 6-field record; gist tightener' },
    { title: 'Sample', detail: 'over-cap convos: convo-sampler reads a downsampled view, expands gaps via expand.py under a token budget → 6-field record' },
    { title: 'Index', detail: 'parallel chunk staging → one deterministic index.py merge; advance change-detector' },
    { title: 'Tag', detail: 'a few batches of older records get a workstream and a kind from their stored summary' },
    { title: 'Curate', detail: 'new workstreams that duplicate existing ones are merged into them' },
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
//   Index     : the validated records are staged in small PARALLEL chunks (no single
//               agent re-serializes the whole batch — that was a slow, lossy serial
//               step), then ONE `index.py --batch-glob` merge reads them all, builds
//               lean records, merges the store, and advances the change-detector ONLY
//               after each record is written (§4.5). Each summary names a workstream
//               (an epic) and a kind; the merge registers new workstreams.
//   Tag       : records summarized before workstreams existed get one, and a kind,
//               from their stored title/gist/next step — a few batches per run, so a
//               backlog drains over the normal runs (convo-tagger, Read-only).
//   Curate    : when this run created workstreams, workstream-curator says which are
//               the same epic as another, and workstreams.py merges them.
//
// RESULT: {status, summarized, indexed, lost, …}. `status` is what the digest skill's
// drain loop keys on — never the counts alone, because a zero can mean two different
// things (#15):
//   'drained'  — nothing left to summarize and nothing left to tag. Stop.
//   'progress' — records landed or were tagged this batch; launch the workflow again.
//   'failed'   — there was work but nothing landed (prep died, every summarizer failed,
//                the merge died). `stage` + `error` say where. Not a success.
// `lost` counts work that was attempted but didn't land even when status is 'progress'
// (a summarizer or chunk that failed); those convos are retried on a later run because
// their change-detector never advanced.
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
// Index chunks are staged by `index.py --stage-chunk`, which reads the records on stdin
// (a quoted heredoc) and writes them under prep's per-run stage_dir. No agent uses the
// Write tool any more: unattended, it could wait forever on a permission prompt (#14),
// and it could only write into the session cwd, where a dead run's chunk files lingered.
const CHUNK = A.chunk || 6                                // records per chunk write (small → reliable)
// Every runner Bash call gets the tool's maximum timeout: a cold-cache prepare.py can run
// past the default 2 minutes and used to kill the whole run (#13).
const BASH_TIMEOUT = 600000
const MODEL = A.model || 'haiku'
const LIMIT = A.limit || 20                           // whole-tier convos per run (batched draining)
// Windowed backfill (issue: huge first run). SINCE limits summarization to convos
// newer than the window ('7d','48h', or an ISO date); SEED_REST stamps the excluded
// older convos as handled in the same pass so they don't linger as "pending".
const SINCE = A.since || null
const SEED_REST = A.seedRest || false
// ONLY: summarize just this conversation (a session id), even unchanged or still active —
// Mission Control's per-conversation Summarize button. Such a run tags nothing else.
const ONLY = A.only || null
if (ONLY && !/^[0-9A-Za-z-]{8,64}$/.test(ONLY)) throw new Error(`digest workflow: bad conversation id ${ONLY}`)
// Agent names are NAMESPACED when installed as a plugin (convo-digest:<name>). The
// freshness hook bakes the real namespace into __CONVO_DIGEST_NS__ at install time
// (same mechanism as SRC). A user-level workflow has no plugin-namespace context, so
// bare names won't resolve — they MUST be baked. For a bare dev checkout the hook
// collapses the placeholder to '' so the bare agent names register unnamespaced.
const NS = '__CONVO_DIGEST_NS__'
const SUMMARIZER_AGENT = A.summarizerAgent || `${NS}convo-summarizer`
const SAMPLER_AGENT = A.samplerAgent || `${NS}convo-sampler`
const TAGGER_AGENT = A.taggerAgent || `${NS}convo-tagger`
const CURATOR_AGENT = A.curatorAgent || `${NS}workstream-curator`
const TAG_BATCH = A.tagBatch || 25                    // records per tagger call
const TAG_ROUNDS = A.only ? 0 : A.tagRounds === undefined ? 4 : A.tagRounds   // tagger calls per run
const KINDS = ['build', 'fix', 'review', 'investigate', 'plan', 'admin']
const RUNNER_AGENT = A.runnerAgent || `${NS}digest-runner`
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
    workstream: { type: 'string' },
    workstream_description: { type: ['string', 'null'] },
    kind: { type: 'string', enum: KINDS },
  },
  // key_entities is intentionally NOT required: the agent guidance tells the model to
  // skip entities already in `facets`, so on file/command-heavy convos there's often
  // nothing left and Haiku drops the field entirely rather than emitting []. Requiring
  // it turned that into a hard StructuredOutput failure that burned the retry cap (5×)
  // and lost the whole record. index.py already defaults a missing/null value to [].
  // workstream_description is only set for a new workstream: not required either.
  required: ['title', 'topics', 'gist', 'status', 'unresolved', 'workstream', 'kind'],
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
    stage_dir: { type: 'string' },
    error: { type: 'string' },
  },
  required: ['convos'],
}

const INDEX_RESULT_SCHEMA = {
  type: 'object',
  properties: {
    written: { type: 'integer' },
    index_size: { type: 'integer' },
    failed: { type: 'array', items: { type: 'object' } },
    replayed: { type: 'boolean' },
    new_workstreams: { type: 'array', items: { type: 'string' } },
    error: { type: 'string' },
  },
  required: ['written'],
}

function wordCount(s) { return (s || '').trim().split(/\s+/).filter(Boolean).length }

// A run with work to do that landed nothing. Returned (not thrown) so the skill can log
// exactly where it broke; the skill treats it as a failure, never as "drained".
function failed(stage, error, extra) {
  log(`FAILED at ${stage}: ${error}`)
  return { status: 'failed', stage, error, summarized: 0, indexed: 0, ...(extra || {}) }
}

const runCmd = (cmd, extra) =>
  `Run this EXACT command with the Bash tool, passing timeout: ${BASH_TIMEOUT}, and return ` +
  `its stdout JSON (it prints one JSON object):\n  ${cmd}\n` +
  `Return the parsed object unchanged. ${extra || ''}`

// --- Prep -------------------------------------------------------------------
phase('Prep')
const prepCmd = `python3 ${SRC}/prepare.py --work ${WORK} --index ${INDEX} --limit ${LIMIT}`
  + (SINCE ? ` --since ${SINCE}` : '') + (SEED_REST ? ' --seed-rest' : '') + (ONLY ? ` --only ${ONLY}` : '')
const prep = await agent(
  runCmd(prepCmd, 'If the command fails or prints no JSON, return ' +
    '{"convos": [], "error": "<its error output>"} — never an empty success.'),
  { schema: PREP_SCHEMA, agentType: RUNNER_AGENT, model: MODEL, label: 'prepare', phase: 'Prep' }
)
if (!prep) return failed('prep', 'the prep agent died before returning (API error, ' +
  'revoked login, or stalled)')
if (prep.error) return failed('prep', prep.error)
const all = prep.convos || []
const whole = all.filter(c => c.tier === 'whole')
const sampled = all.filter(c => c.tier === 'sample')
const trivial = all.filter(c => c.tier === 'trivial')
if (trivial.length) log(`${trivial.length} convo(s) under token floor → trivial (skipped — recall noise)`)
log(`Prep: ${all.length} changed, ${whole.length} whole-tier, ${sampled.length} over-cap (sampler)`)
const SUMMARIZING = whole.length > 0 || sampled.length > 0
if (!SUMMARIZING) log('nothing to summarize')
if (SUMMARIZING && !prep.stage_dir) return failed('prep', 'prepare.py returned no stage_dir')
const STAGE = prep.stage_dir
let indexed = 0
let idx = null
let ok = []
const lost = { summaries: [], chunks: 0, merge: [], tags: [] }
if (SUMMARIZING) {

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
const lostKeys = []          // convos whose summarizer/sampler produced nothing
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
  lostKeys.push(...whole.filter((c, i) => !(results[i] && results[i].summary)).map(c => c.key))
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
  lostKeys.push(...sampled.filter((c, i) => !(results[i] && results[i].summary)).map(c => c.key))
  log(`Sample: ${sampleOk.length}/${sampled.length} produced records`)
}

ok = [...wholeOk, ...sampleOk]
if (!ok.length) return failed('summarize',
  `all ${whole.length + sampled.length} summarizer(s) failed`, { lost: { summaries: lostKeys } })

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
  properties: { path: { type: 'string' }, count: { type: 'integer' }, error: { type: 'string' } },
  required: ['count'] }
// JSON.stringify emits a single line, so the heredoc terminator can never appear inside it.
const EOF_MARK = '__CONVO_DIGEST_CHUNK__'
const writes = await parallel(chunks.map((chunk, i) => () => agent(
  `Stage these index records. Run the command below with the Bash tool, passing timeout: ` +
  `${BASH_TIMEOUT}. Copy it exactly — the JSON line between the heredoc markers must be ` +
  `verbatim and complete. Do NOT use the Write tool.\n\n` +
  `python3 ${SRC}/index.py --stage-chunk ${STAGE}/chunk_${i}.json <<'${EOF_MARK}'\n` +
  `${JSON.stringify(chunk)}\n${EOF_MARK}\n\n` +
  `It prints {"path","count"} — return that. If it prints an "error" (the JSON didn't ` +
  `survive the copy), run it once more, copying more carefully; if it fails again, return ` +
  `its {"count": 0, "error": ...} output.`,
  { schema: CHUNK_SCHEMA, agentType: RUNNER_AGENT, model: MODEL,
    label: `batch:${i}`, phase: 'Index' }
)))
const stagedCount = writes.reduce((n, w) => n + ((w && !w.error && w.count) || 0), 0)
const lostChunks = chunks.filter((c, i) => !writes[i] || writes[i].error || writes[i].count < c.length)
if (lostChunks.length) log(`Index: ${lostChunks.length}/${chunks.length} chunk(s) failed to stage`)
if (!stagedCount) return failed('stage', 'no chunk could be staged',
  { summarized: ok.length, lost: { summaries: lostKeys, chunks: lostChunks.length } })

// One merge over this run's stage dir (absolute — the merge agent's cwd is irrelevant).
// --result-file makes it safe to run twice: a retry after the merge already landed
// replays the saved result instead of reporting written: 0 (N3).
const mergeCmd = `python3 ${SRC}/index.py --batch-glob '${STAGE}/chunk_*.json' ` +
  `--index ${INDEX} --model haiku-4-5 --cleanup --result-file ${STAGE}/merge_result.json`
const merge = label => agent(runCmd(mergeCmd), {
  schema: INDEX_RESULT_SCHEMA, agentType: RUNNER_AGENT, model: MODEL, label, phase: 'Index' })
idx = await merge('merge')
if (!idx) { log('Index: merge agent died — retrying once'); idx = await merge('merge-retry') }
if (!idx) return failed('merge', 'the merge agent died twice',
  { summarized: ok.length, lost: { summaries: lostKeys, chunks: lostChunks.length } })

indexed = idx.written || 0
log(`Index: merged ${indexed}/${ok.length} records → ${INDEX} (size ${idx.index_size})` +
  (idx.replayed ? ' [replayed]' : ''))
Object.assign(lost, { summaries: lostKeys, chunks: lostChunks.length, merge: idx.failed || [] })
if (!indexed) return failed('merge', idx.error || 'the merge wrote no records',
  { summarized: ok.length, lost })
}

// --- Tag older records with a workstream and a kind --------------------------
// Each round: workstreams.py writes the next untagged records (newest first) with the
// current workstream list to a batch file; the tagger reads it and returns one tag per
// record; workstreams.py stores them. Rounds run one after another so a workstream one
// round creates is in the list the next round sees.
const UNTAGGED_SCHEMA = { type: 'object',
  properties: { path: { type: 'string' }, count: { type: 'integer' },
                remaining: { type: 'integer' }, error: { type: 'string' } },
  required: ['count'] }
const TAGS_SCHEMA = { type: 'object', properties: { tags: { type: 'array', items: {
  type: 'object',
  properties: { key: { type: 'string' }, workstream: { type: 'string' },
                kind: { type: 'string', enum: KINDS },
                workstream_description: { type: ['string', 'null'] } },
  required: ['key', 'workstream', 'kind'] } } }, required: ['tags'] }
const APPLY_SCHEMA = { type: 'object',
  properties: { applied: { type: 'integer' }, new: { type: 'array', items: { type: 'string' } },
                skipped: { type: 'array', items: { type: 'object' } }, error: { type: 'string' } },
  required: ['applied'] }
const TAG_EOF = '__CONVO_DIGEST_TAGS__'
let tagged = 0
let tagRemaining = 0
const created = [...((idx && idx.new_workstreams) || [])]
if (TAG_ROUNDS > 0) phase('Tag')
for (let round = 0; round < TAG_ROUNDS; round++) {
  const next = await agent(runCmd(
    `python3 ${SRC}/workstreams.py untagged --index ${INDEX} --limit ${TAG_BATCH} ` +
    `--out ${WORK}/tags/batch_${round}.json`,
    'If the command fails or prints no JSON, return {"count": 0, "error": "<its error output>"}.'),
    { schema: UNTAGGED_SCHEMA, agentType: RUNNER_AGENT, model: MODEL,
      label: `untagged:${round}`, phase: 'Tag' })
  if (!next || next.error) {
    lost.tags.push(`round ${round}: ${next ? next.error : 'the runner died'}`)
    break
  }
  tagRemaining = (next.remaining || 0) + next.count
  if (!next.count) break
  const t = await agent(`Tag the conversations in this batch file: ${next.path}`,
    { schema: TAGS_SCHEMA, agentType: TAGGER_AGENT, model: MODEL,
      label: `tag:${round}`, phase: 'Tag' })
  if (!t || !t.tags || !t.tags.length) {
    lost.tags.push(`round ${round}: the tagger returned no tags`)
    break
  }
  const applied = await agent(
    `Store these tags. Run the command below with the Bash tool, passing timeout: ` +
    `${BASH_TIMEOUT}. Copy it exactly — the JSON line between the heredoc markers must be ` +
    `verbatim and complete.\n\n` +
    `python3 ${SRC}/workstreams.py apply-tags --index ${INDEX} <<'${TAG_EOF}'\n` +
    `${JSON.stringify(t.tags)}\n${TAG_EOF}\n\n` +
    `Return the JSON it prints. If it prints an "error", return {"applied": 0, "error": ...}.`,
    { schema: APPLY_SCHEMA, agentType: RUNNER_AGENT, model: MODEL,
      label: `apply:${round}`, phase: 'Tag' })
  if (!applied || applied.error) {
    lost.tags.push(`round ${round}: ${applied ? applied.error : 'the runner died'}`)
    break
  }
  tagged += applied.applied || 0
  tagRemaining -= applied.applied || 0
  created.push(...(applied.new || []))
  if ((applied.skipped || []).length) lost.tags.push(...applied.skipped.map(x => `${x.key}: ${x.why}`))
  if (!applied.applied) break                // nothing stuck: don't loop on the same batch
}
if (tagged) log(`Tag: ${tagged} older record(s) tagged, ${tagRemaining} left`)

// --- Curate: merge new workstreams that are an existing epic in other words --
let merged = []
if (created.length) {
  phase('Curate')
  const CURATE_FILE_SCHEMA = { type: 'object',
    properties: { path: { type: 'string' }, count: { type: 'integer' }, error: { type: 'string' } },
    required: ['count'] }
  const file = await agent(runCmd(
    `python3 ${SRC}/workstreams.py curate-file --new '${JSON.stringify(created).replace(/'/g, "'\\''")}' ` +
    `--out ${WORK}/tags/curate.json`),
    { schema: CURATE_FILE_SCHEMA, agentType: RUNNER_AGENT, model: MODEL, label: 'curate-file', phase: 'Curate' })
  const MERGES_SCHEMA = { type: 'object', properties: { merges: { type: 'array', items: {
    type: 'object', properties: { from: { type: 'string' }, into: { type: 'string' } },
    required: ['from', 'into'] } } }, required: ['merges'] }
  const verdict = file && file.path && !file.error
    ? await agent(`Decide which new workstreams to merge. Read this file: ${file.path}`,
        { schema: MERGES_SCHEMA, agentType: CURATOR_AGENT, model: MODEL, label: 'curate', phase: 'Curate' })
    : null
  if (verdict && verdict.merges.length) {
    const res = await agent(
      `Merge these workstreams. Run the command below with the Bash tool, passing timeout: ` +
      `${BASH_TIMEOUT}. Copy it exactly.\n\n` +
      `python3 ${SRC}/workstreams.py merge --index ${INDEX} <<'${TAG_EOF}'\n` +
      `${JSON.stringify(verdict.merges)}\n${TAG_EOF}\n\nReturn the JSON it prints.`,
      { schema: { type: 'object', properties: { merged: { type: 'array', items: { type: 'object' } } },
                  required: ['merged'] },
        agentType: RUNNER_AGENT, model: MODEL, label: 'merge-ws', phase: 'Curate' })
    merged = (res && res.merged) || []
  } else if (!verdict) {
    lost.tags.push('curate: the curator did not answer; duplicates (if any) stay until a later run')
  }
  if (merged.length) log(`Curate: merged ${merged.map(m => `${m.from} → ${m.into}`).join(', ')}`)
}

const status = indexed || tagged ? 'progress' : (tagRemaining && lost.tags.length ? 'failed' : 'drained')
if (status === 'failed') return failed('tag', lost.tags.join('; '), { lost })
return { status, summarized: ok.length, indexed, tagged, tag_remaining: tagRemaining,
         new_workstreams: created, merged_workstreams: merged,
         index_size: idx ? idx.index_size : undefined, lost,
         failed: (idx && idx.failed) || [], counts: prep.counts || {} }
