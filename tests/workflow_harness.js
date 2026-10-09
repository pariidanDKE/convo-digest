// Runs src/digest.workflow.js under stubbed workflow hooks, for tests/test_workflow.py.
// Usage: node workflow_harness.js '<scenario JSON>'  → prints {"result", "calls"}.
//
// A scenario maps an agent label prefix (prepare, sum, batch, merge, merge-retry) to what
// that agent returns: an object, null (the agent died), or the string "ok" for the
// default success shape. `sum` may also be a list, one entry per convo in order.
const fs = require('fs')
const path = require('path')

const scenario = JSON.parse(process.argv[2] || '{}')
let src = fs.readFileSync(path.join(__dirname, '..', 'src', 'digest.workflow.js'), 'utf8')
src = src.replace('export const meta', 'const meta')

const calls = []
const SUMMARY = { title: 't', topics: ['x'], gist: 'short gist', status: 'solved',
                  unresolved: null, key_entities: [], workstream: 'Digest', kind: 'build' }
const EOF_MARK = "<<'__CONVO_DIGEST_CHUNK__'\n"
let sumIndex = 0

async function agent(prompt, opts) {
  const label = (opts && opts.label) || ''
  const kind = label.split(':')[0]
  calls.push(label)
  let r = scenario[kind]
  if (kind === 'sum' && Array.isArray(r)) r = r[sumIndex++]
  if (r === undefined) r = 'ok'
  if (r !== 'ok') return r
  if (kind === 'sum' || kind === 'sample' || kind === 'tighten') return { ...SUMMARY }
  if (kind === 'batch') {
    // The heredoc body must be the chunk, intact: parse it the way index.py would.
    const start = prompt.indexOf(EOF_MARK) + EOF_MARK.length
    const line = prompt.slice(start, prompt.indexOf('\n', start))
    return { path: `/stage/${label}.json`, count: JSON.parse(line).length }
  }
  // by default nothing waits for a tag, so a run without tagging behaves as before
  if (kind === 'untagged') return { path: '/work/tags/batch.json', count: 0, remaining: 0 }
  throw new Error(`no stub for agent ${label}`)
}

async function parallel(thunks) {
  return Promise.all(thunks.map(t => t().catch(() => null)))
}

async function pipeline(items, ...stages) {
  return Promise.all(items.map(async (item, i) => {
    let r = item
    for (const [n, stage] of stages.entries()) {
      try { r = await stage(n === 0 ? item : r, item, i) } catch (e) { return null }
    }
    return r
  }))
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', src)
body(scenario.args || { src: '/plugin/src' }, agent, parallel, pipeline, () => {}, () => {})
  .then(result => process.stdout.write(JSON.stringify({ result, calls })))
  .catch(e => { process.stdout.write(JSON.stringify({ error: String(e), calls })); process.exit(1) })
