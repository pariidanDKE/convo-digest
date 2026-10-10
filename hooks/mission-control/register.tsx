// Mission Control: one pane over all your Claude Code work.
//
//   /mission [today|yesterday|week]   open the pane (optionally on a range)
//   /mission ask <question>           open it and ask the agent straight away
//   /mission home [off]               this session opens the pane by itself
//   /mission band on|off              the one-line way in above every prompt
//
// Top to bottom: the day's standup brief (folded, unfolds in full), an Ask box
// answered from the digest index, a timeline of your conversations grouped by workstream
// (click one for its gist and a way to open it), then the routines (scheduled tasks)
// and the digest's run health. A status-line entry keeps the routines' state visible
// with the pane closed.
//
// Data comes from src/mission_control.py (read-only over the transcripts, the digest
// index, the app's session files and the digest run ledger).
import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register, RenderElement, RenderInput } from 'claude-code'

import type {
  McAsk,
  McColorMode,
  McKind,
  McRangeKey,
  McReader,
  McRef,
  McRoutine,
  McRowsMode,
  McSession,
  McSnapshot,
  McStatus,
  McTask,
  McView,
} from '../../types'
import {
  duration,
  hm,
  rowsOptions,
  routineName,
  runDot,
  statusColor,
  statusLabel,
  timeline,
  visible as inView,
  workedMinutes,
  timelineStrips,
  truncate,
  when,
} from './layout'

const PLUGIN = 'convo-digest'
const PANE = 'mission-control'
const TITLE = 'Mission Control'
const ASK_MODEL = 'sonnet'
const VIEW_VERSION = 3                // bump to move everyone onto a new default view
const PX_PER_CELL = 8                 // the desktop's pane width per text cell, about
const WIDE = 140                      // a pane this many cells wide reads beside the timeline
const READ_LAST = 40                  // turns the reader shows; "Show earlier" adds as many
const READ_POLL_MS = 4000             // how often an open reader looks for new messages

const snapAtom = atom({ plugin: 'convo-digest', key: 'snap' } as const, null)
const viewAtom = atom({ plugin: 'convo-digest', key: 'view' } as const,
  { range: 'today', rows: 'workstream', color: 'project' } as McView)
const selectedAtom = atom({ plugin: 'convo-digest', key: 'selected' } as const, null)
const askAtom = atom({ plugin: 'convo-digest', key: 'ask' } as const,
  { question: '', busy: false, answer: null, refs: {}, error: null } as McAsk)
const statusAtom = atom({ plugin: 'convo-digest', key: 'status' } as const,
  { loading: false, error: null, tasks: null } as McStatus)
const bandAtom = atom({ plugin: 'convo-digest', key: 'band' } as const, true)
const standupOpenAtom = atom({ plugin: 'convo-digest', key: 'standupOpen' } as const, false)
const summarizingAtom = atom({ plugin: 'convo-digest', key: 'summarizing' } as const, null as number | null)
const collapsedAtom = atom({ plugin: 'convo-digest', key: 'collapsed' } as const, [] as string[])
const actingAtom = atom({ plugin: 'convo-digest', key: 'acting' } as const, null as string | null)
const readerAtom = atom({ plugin: 'convo-digest', key: 'reader' } as const, null as McReader | null)
const resummarizingAtom = atom({ plugin: 'convo-digest', key: 'resummarizing' } as const, null as string | null)

/** Fold or unfold one timeline group, by its heading. */
async function toggleGroup($: $T, label: string): Promise<void> {
  await update($, collapsedAtom, list => (list.includes(label) ? list.filter(l => l !== label) : [...list, label]))
}
const DIGEST = { command: 'convo-digest:digest', args: 'now' } as const
const SESSION_ACTION = 'convo-digest:session'       // this plugin's skill: the app's archive and pin tools

type $T = EngineInterface
/** What the Pane render hook receives. */
type PaneRender = RenderInput<'Pane'>
type El = Pick<Elements['desktop'], 'Box' | 'Text' | 'Button' | 'Select' | 'Input' | 'Markdown' | 'Client'>

const errText = (err: unknown) => (err instanceof Error ? err.message : String(err))

/** Best-effort trace at ~/.claude/mission-control/mod-status.json: when the module
 *  started, the last refresh and its error, so a pane that stays empty can be diagnosed. */
async function breadcrumb($: $T, patch: Record<string, unknown>): Promise<void> {
  try {
    const home = await $.env.get('HOME')
    if (!home) return
    const path = `${home}/.claude/mission-control/mod-status.json`
    let cur: Record<string, unknown> = {}
    try {
      cur = JSON.parse(String(await $.fs.read(path))) as Record<string, unknown>
    } catch {
      cur = {}
    }
    await $.fs.write(path, JSON.stringify({ ...cur, ...patch }, null, 1))
  } catch {
    // tracing must never break the pane
  }
}

// ------------------------------------------------------------------ data
async function collect($: $T, args: string[]): Promise<unknown> {
  const script = `${$.plugin.root}/src/mission_control.py`
  const r = await $.process.run(['python3', script, ...args], { timeoutMs: 120_000 })
  if (r.exitCode !== 0) {
    const last = r.stderr.trim().split('\n').pop()
    throw new Error(last || `collect.py exited ${r.exitCode}`)
  }
  return JSON.parse(r.stdout)
}

async function loadTasks($: $T): Promise<McTask[] | null> {
  try {
    const res = await $.mcp.call('scheduled-tasks', 'list_scheduled_tasks', {})
    if (res.isError) return null
    const block = res.content.find(b => b.type === 'text') as { text?: string } | undefined
    const list = JSON.parse(block?.text ?? '[]') as Record<string, unknown>[]
    return list.map(t => ({
      taskId: String(t.taskId),
      enabled: t.enabled !== false,
      schedule: typeof t.schedule === 'string' ? t.schedule : null,
      nextRunAt: typeof t.nextRunAt === 'string' ? t.nextRunAt : null,
    }))
  } catch {
    return null                       // the app's task server isn't reachable from here
  }
}

const MARK: Record<string, string> = { ok: '✓', nothing: '✓', warn: '!', failed: '✗', running: '…' }

/** The status line says only what needs attention (a routine whose last run failed or
 *  warned, digest issues since its last good run) and is empty otherwise. */
function statusLine($: $T, snap: McSnapshot): void {
  const parts: string[] = []
  for (const r of snap.routines.filter(x => !x.oneTime)) {
    const run = r.runs[0]
    if (run && (run.status === 'failed' || run.status === 'warn')) {
      parts.push(`${routineName(r).toLowerCase()} ${MARK[run.status]}`)
    }
  }
  if (snap.digest.issuesSinceOk) parts.push(`${snap.digest.issuesSinceOk} digest issue(s)`)
  $.ui.status(parts.length ? `◉ MC · ${parts.join(' · ')}` : undefined)
}

/** A local calendar day `back` days before `now`, as YYYY-MM-DD and as a label. */
function dayAt(now: number, tzOffsetMin: number, back: number): { value: string; label: string } {
  const d = new Date(now + tzOffsetMin * 60_000 - back * 86_400_000)
  const value = d.toISOString().slice(0, 10)
  const label = back === 0 ? 'Today' : back === 1 ? 'Yesterday'
    : `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getUTCDay()]} ${d.getUTCDate()} ` +
      `${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getUTCMonth()]}` +
      (back > 300 ? ` ${d.getUTCFullYear()}` : '')
  return { value, label }
}

const CUSTOM_DAYS = 60                  // how far back the pickers reach (a Select holds 64 at most)
const MAX_SPAN = 31                     // the collector's MAX_DAYS

/** The collector's --range for a view: a custom range is its two days. */
function rangeArg(view: McView): string {
  return view.range === 'custom' && view.from && view.to ? `${view.from}..${view.to}` : view.range === 'custom'
    ? '7d' : view.range
}

/** Whether the view shows a single day (where one row per day means nothing). */
function oneDay(view: McView): boolean {
  return view.range === 'today' || view.range === 'yesterday' ||
    (view.range === 'custom' && !!view.from && view.from === view.to)
}

async function refresh($: $T): Promise<void> {
  const key = rangeArg(await read($, viewAtom))
  await update($, statusAtom, s => ({ ...s, loading: true }))
  try {
    const [snap, tasks] = await Promise.all([
      collect($, ['snapshot', '--range', key]) as Promise<McSnapshot>,
      loadTasks($),
    ])
    await update($, snapAtom, () => snap)
    await update($, statusAtom, s => ({ ...s, loading: false, error: null, tasks: tasks ?? s.tasks }))
    statusLine($, snap)
    await breadcrumb($, { lastRefreshAt: snap.generatedAt, range: key, tookMs: snap.tookMs,
      tasksReachable: tasks !== null, lastError: null })
  } catch (err) {
    await update($, statusAtom, s => ({ ...s, loading: false, error: errText(err) }))
    await breadcrumb($, { lastErrorAt: await $.clock.now(), lastError: errText(err) })
  }
}

async function setView($: $T, patch: Partial<McView>): Promise<void> {
  const before = await read($, viewAtom)
  const next = { ...before, ...patch }
  if (next.range === 'custom') {
    // From after To swaps; a span past what the collector draws keeps its end
    if (next.from && next.to && next.from > next.to) [next.from, next.to] = [next.to, next.from]
    if (next.from && next.to) {
      const span = (Date.parse(next.to) - Date.parse(next.from)) / 86_400_000 + 1
      if (span > MAX_SPAN) {
        const keep = patch.from ? 'from' : 'to'
        const shift = (MAX_SPAN - 1) * 86_400_000
        if (keep === 'from') next.to = new Date(Date.parse(next.from) + shift).toISOString().slice(0, 10)
        else next.from = new Date(Date.parse(next.to) - shift).toISOString().slice(0, 10)
      }
    }
  }
  if (oneDay(next) && next.rows === 'day') next.rows = 'project'
  await update($, viewAtom, () => next)
  await $.store.set('view', { ...next, version: VIEW_VERSION })
  if (rangeArg(next) !== rangeArg(before)) {
    await update($, selectedAtom, () => null)
    await refresh($)
  }
}

function findSession(snap: McSnapshot | null, ask: McAsk, id: string): { app: string | null; title: string } | null {
  const s = snap?.sessions.find(x => x.id === id)
  if (s) return { app: s.app, title: s.title }
  const ref = Object.values(ask.refs).find(r => r.id === id)
  return ref ? { app: ref.app, title: ref.title } : null
}

async function openSession($: $T, id: string, surface?: Parameters<$T['ui']['copy']>[0]['surface']): Promise<void> {
  const found = findSession(await read($, snapAtom), await read($, askAtom), id)
  if (!found?.app) {
    await $.ui.copy({ text: `claude --resume ${id}`, surface })
    $.ui.toast('No desktop session for this one — copied `claude --resume` to the clipboard')
    return
  }
  const r = await $.process.run(['open', `claude://claude.ai/epitaxy/${found.app}`])
  if (r.exitCode !== 0) $.ui.toast(`Couldn't open the session: ${r.stderr.trim() || r.exitCode}`)
}

// ------------------------------------------------------------------ reader
/**
 * Open a conversation in the reader: beside the timeline in a wide pane, in place of
 * the overview in a narrow one. It reads the transcript, read-only, and reads it
 * again every few seconds while it's open, so a running conversation shows its replies.
 */
async function openReader($: $T, id: string, wide: boolean): Promise<void> {
  const cur = await read($, readerAtom)
  if (cur?.id === id) return closeReader($)
  await update($, readerAtom, () => ({ id, last: READ_LAST, loading: true, expanded: false }))
  await update($, selectedAtom, () => id)
  await loadReader($, true)
  await $.ui.scroll(wide ? { to: { key: 'reader' }, in: PANE, block: 'nearest' } : { to: 'start', in: PANE })
    .catch(() => undefined)
}

async function closeReader($: $T): Promise<void> {
  const cur = await read($, readerAtom)
  await update($, readerAtom, () => null)
  // back where the conversation was clicked
  if (cur) await $.ui.scroll({ to: { key: `go-${cur.id}` }, in: PANE, block: 'center' }).catch(() => undefined)
}

/** Read the open conversation; unless `force`, only when its file has changed. */
async function loadReader($: $T, force: boolean): Promise<void> {
  const cur = await read($, readerAtom)
  if (!cur) return
  const args = ['transcript', '--id', cur.id, '--last', String(cur.last)]
  if (!force && cur.mtime !== undefined) args.push('--if-newer', String(cur.mtime))
  try {
    const got = await collect($, args) as Omit<McReader, 'last' | 'loading'> & { unchanged?: boolean }
    await update($, readerAtom, r => {
      if (!r || r.id !== cur.id) return r                  // closed or switched meanwhile
      if (got.unchanged) return { ...r, loading: false }
      return { ...got, id: r.id, last: r.last, loading: false, expanded: r.expanded }
    })
  } catch (err) {
    await update($, readerAtom, r => (r && r.id === cur.id ? { ...r, loading: false, error: errText(err) } : r))
  }
}

async function readEarlier($: $T): Promise<void> {
  await update($, readerAtom, r => (r ? { ...r, last: r.last + READ_LAST, loading: true } : r))
  await loadReader($, true)
}

/** The reader: what the conversation was about, the way in, then its messages. */
function Reader(el: El, $: $T, r: McReader, s: McSession | null, snap: McSnapshot, now: number, self: string,
  busy: string | null) {
  const { Box, Text, Button, Markdown } = el
  const tz = snap.tzOffsetMin
  const color = s ? statusColor(s.scheduled ? 'routine' : s.status) : '#30363d'
  const title = s?.title ?? r.id.slice(0, 8)
  const live = s ? now - s.last < 5 * 60_000 : false
  const turns = r.turns ?? []
  return (
    <Box key="reader" flexDirection="column" borderStyle="round" borderColor={color} paddingX={1}>
      <Box flexDirection="row" justifyContent="space-between" gap={2}>
        <Text bold wrap="wrap">{title}</Text>
        <Box flexDirection="row" gap={2}>
          <Button key="reader-open" label={!s || s.app ? 'Open ↗' : 'Copy resume command'} variant="primary"
            onPress={e => { void openSession($, r.id, e.surface) }} />
          <Button key="reader-close" label="Close ✕" plain dimColor onPress={() => { void closeReader($) }} />
        </Box>
      </Box>
      {s
        ? (
          <Text dimColor wrap="wrap">
            {`${s.project}${s.branch ? ` · ${s.branch}` : ''} · ${when(s.first, snap, now)}–${hm(s.last, tz)} · ` +
              `${duration(s.activeMin)} active${s.tickets.length ? ` · ${s.tickets.join(', ')}` : ''}` +
              (s.archived ? ' · archived' : '')}
            {live ? <Text color="#3fb950">{'  ● live'}</Text> : ''}
          </Text>
        )
        : null}
      {s?.gist ? <Markdown text={s.gist} /> : null}
      {s?.open ? <Text dimColor wrap="wrap">{`Left for later: ${s.open}`}</Text> : null}
      {s && !s.scheduled
        ? (
          <Box flexDirection="row" gap={2} marginTop={1}>
            {SummarizeButton(el, $, s, `summarize-r-${s.id}`, self, busy)}
            {PinButton(el, $, s, `pin-r-${s.id}`)}
            {ArchiveButton(el, $, s, `archive-r-${s.id}`, self)}
            {MoveTo(el, $, s, snap)}
          </Box>
        )
        : null}
      <Box flexDirection="row" gap={2} marginTop={1} alignItems="center">
        <Button key="reader-toggle" plain
          label={`${r.expanded ? '▾' : '▸'} Conversation · ${r.loading && r.total === undefined ? '…'
            : `${r.total ?? 0} message${r.total === 1 ? '' : 's'}`}`}
          onPress={() => { void update($, readerAtom, x => (x ? { ...x, expanded: !x.expanded } : x)) }} />
        {r.expanded
          ? <Text dimColor>{`${r.hidden ? `last ${turns.length} shown · ` : ''}read-only, updates while open`}</Text>
          : null}
      </Box>
      {r.error ? <Text color="#f85149" wrap="wrap">{r.error}</Text> : null}
      {r.expanded && r.hidden
        ? <Button key="reader-earlier" label={`↑ Show earlier messages (${r.hidden} more)`} plain dimColor
            onPress={() => { void readEarlier($) }} />
        : null}
      {(r.expanded ? turns : []).map((t, i) => t.role === 'note'
        ? <Text key={`t-${i}`} dimColor italic>{`— ${t.text} —`}</Text>
        : (
          <Box key={`t-${i}`} flexDirection="column" marginTop={1}
            {...(t.role === 'user' ? { backgroundColor: '#262626', paddingX: 1 } : {})}>
            <Text bold color={t.role === 'user' ? '#58a6ff' : '#d2a8ff'}>
              {t.role === 'user' ? 'You' : 'Claude'}
              <Text dimColor>{t.ts ? `  ${when(t.ts, snap, now)}` : ''}</Text>
            </Text>
            {t.text ? <Markdown text={t.text} /> : null}
            {t.tools.length ? <Text dimColor wrap="wrap">{`⚙ ${t.tools.join(' · ')}`}</Text> : null}
          </Box>
        ))}
      {r.expanded && turns.length > 3
        ? (
          <Box flexDirection="row" gap={2} marginTop={1}>
            <Button key="reader-open-2" label={!s || s.app ? 'Open ↗' : 'Copy resume command'} plain
              onPress={e => { void openSession($, r.id, e.surface) }} />
            <Button key="reader-collapse" label="▴ Fold the conversation" plain dimColor
              onPress={() => { void update($, readerAtom, x => (x ? { ...x, expanded: false } : x)) }} />
            <Button key="reader-close-2" label="Close ✕" plain dimColor onPress={() => { void closeReader($) }} />
          </Box>
        )
        : null}
    </Box>
  )
}

/** Drop the answer and empty the field, ready for the next question. */
async function clearAsk($: $T): Promise<void> {
  await update($, askAtom, a => a.busy ? a : { ...a, question: '', answer: null, refs: {}, error: null })
}

async function runAsk($: $T, question: string): Promise<void> {
  const q = question.trim()
  if (!q) return
  await update($, askAtom, a => ({ ...a, question: q, busy: true, error: null }))
  try {
    const view = await read($, viewAtom)
    const ctx = await collect($, ['ask', '--question', q, '--range', rangeArg(view)]) as {
      system: string; prompt: string; refs: Record<string, McRef>
    }
    const res = await $.model.complete({
      model: ASK_MODEL, system: ctx.system, prompt: ctx.prompt, maxTokens: 900, timeoutMs: 120_000,
    })
    if (!res.isAnswered) throw new Error(`the model gave no answer (${res.reason})`)
    await update($, askAtom, a => ({ ...a, busy: false, answer: res.text, refs: ctx.refs }))
  } catch (err) {
    await update($, askAtom, a => ({ ...a, busy: false, error: errText(err) }))
  }
}

async function runNow($: $T, r: McRoutine): Promise<void> {
  try {
    const res = await $.mcp.call('scheduled-tasks', 'run_scheduled_task', { taskId: r.task })
    const text = res.content.find(b => b.type === 'text') as { text?: string } | undefined
    $.ui.toast(res.isError ? `Couldn't start ${routineName(r)}: ${text?.text ?? 'error'}` : `Started ${routineName(r)}`)
    if (!res.isError) setTimeout30($)
  } catch (err) {
    $.ui.toast(`Couldn't reach the app's scheduled tasks: ${errText(err)}`)
  }
}

function setTimeout30($: $T): void {
  $.clock.after(30_000, () => { void refresh($) })
}

// ------------------------------------------------------------------ drawing
function cites(answer: string, refs: Record<string, McRef>): McRef[] {
  const seen = new Set<string>()
  const out: McRef[] = []
  for (const m of answer.matchAll(/\[(\d+)\]/g)) {
    const ref = m[1] ? refs[m[1]] : undefined
    if (ref && !seen.has(ref.id)) {
      seen.add(ref.id)
      out.push(ref)
    }
  }
  return out.slice(0, 8)
}

/** True when a run ended badly enough to call out. */
const isBad = (status: string | undefined) => status === 'failed' || status === 'warn'

function Routines(el: El, $: $T, snap: McSnapshot, tasks: McTask[] | null, now: number) {
  const { Box, Text, Button } = el
  // one-off tasks only earn a line when their run went wrong
  const list = snap.routines.filter(r => !r.oneTime || isBad(r.runs[0]?.status))
  if (!list.length) return <Text dimColor>No scheduled tasks have run in the last two weeks.</Text>
  return (
    <Box flexDirection="column">
      {list.map(r => {
        const last = r.runs[0]
        const dot = runDot(last?.status)
        const history = [...r.runs].slice(0, 7).reverse()
        const task = tasks?.find(t => t.taskId === r.task)
        const next = task?.nextRunAt ? Date.parse(task.nextRunAt) : NaN
        const lastText = last?.start ? `${when(last.start, snap, now)} · ${dot.word}` : 'no runs yet'
        return (
          <Box key={`routine-${r.task}`} flexDirection="column">
            <Box flexDirection="row" gap={1} alignItems="center">
              <Text color={dot.color}>●</Text>
              <Box width={22}><Text bold wrap="truncate-end">{routineName(r)}</Text></Box>
              <Box width={30}>
                <Text dimColor wrap="truncate-end">
                  {`${lastText}${Number.isFinite(next) ? ` · next ${when(next, snap, now)}` : ''}` +
                    `${task && !task.enabled ? ' · paused' : ''}`}
                </Text>
              </Box>
              <Text>{history.map(x => <Text color={runDot(x.status).color}>{runDot(x.status).glyph}</Text>)}</Text>
              {last?.app
                ? <Button key={`open-run-${r.task}`} label="Open" plain dimColor
                    onPress={() => { void openSessionByApp($, last.app as string) }} />
                : null}
              {!r.oneTime && tasks !== null
                ? <Button key={`run-${r.task}`} label="Run now" plain dimColor
                    onPress={() => { void runNow($, r) }} />
                : null}
            </Box>
            {isBad(last?.status) && last?.detail
              ? <Text color={dot.color} wrap="truncate-end">{`    ${truncate(last.detail, 160)}`}</Text>
              : null}
          </Box>
        )
      })}
    </Box>
  )
}

/** Conversations the digest hasn't summarized yet, or that moved on since it did —
 *  less this session, which the digest never summarizes while it is being written. */
function unsummarized(snap: McSnapshot, selfId: string): McSession[] {
  return snap.sessions.filter(s => !s.scheduled && s.id !== selfId && (!s.digested || s.changedSinceDigest))
}

/** Summarize now: run `/convo-digest:digest now` in this session, as if typed. The skill
 *  does the rest — run log, lock, title sync — and `turn.complete` refreshes the pane
 *  when it is done. (A mod may not type a slash command into the prompt; it runs it.) */
async function summarizeNow($: $T): Promise<void> {
  if (await read($, summarizingAtom)) return              // one at a time
  const startedAt = await $.clock.now()
  await update($, summarizingAtom, () => startedAt)
  // not awaited: the command is queued until the session is idle and runs a long turn
  $.command.run({ command: DIGEST.command, args: DIGEST.args }).catch(async err => {
    await update($, summarizingAtom, () => null)
    $.ui.toast(`Couldn't start the digest: ${errText(err)}`)
  })
}

/** Whether a drawn tree shows nothing: what the band's chain gives back when no other
 *  plugin draws there. */
function drawsNothing(el: RenderElement | null | undefined): boolean {
  if (!el) return true
  if (el.type !== 'Box' && el.type !== 'Text') return false
  return (el.children ?? []).every(k =>
    typeof k === 'string' ? k.trim() === '' : drawsNothing(k as RenderElement))
}

// ------------------------------------------------------------------ home session
// Which chat is Mission Control's home lives in a file, not $.store: every copy of the
// plugin (the installed one, a development one) keeps a store of its own, and the band
// in every chat has to agree on where home is.
type Home = { cli: string; app: string | null }

async function homeFile($: $T): Promise<string | null> {
  const home = await $.env.get('HOME').catch(() => undefined)
  return home ? `${home}/.claude/mission-control/home.json` : null
}

async function readHome($: $T): Promise<Home | null> {
  const path = await homeFile($)
  if (!path) return null
  try {
    const h = JSON.parse(String(await $.fs.read(path))) as Partial<Home>
    return typeof h.cli === 'string' ? { cli: h.cli, app: typeof h.app === 'string' ? h.app : null } : null
  } catch {
    return null
  }
}

/** Make this chat the home, with its desktop id so the band elsewhere can link to it. */
async function writeHome($: $T): Promise<void> {
  const path = await homeFile($)
  if (!path) return
  const raw = await $.env.get('CLAUDE_CODE_HOST_SESSION_ID').catch(() => undefined)
  const app = typeof raw === 'string' && /^local_[\w-]+$/.test(raw) ? raw : null
  await $.fs.write(path, JSON.stringify({ cli: await $.session.id(), app }, null, 1))
}

/**
 * Go to the home chat's own window. The app's link can only open a chat in the main window,
 * so the band opens ~/Applications/Mission Control Raise.app, a tiny AppleScript app with
 * Accessibility permission that switches to the other Claude window through the Window menu,
 * and reports when it can't. No fallback: without it the band says so.
 */
async function goHome($: $T): Promise<void> {
  const home = await $.env.get('HOME').catch(() => undefined)
  const raise = home ? `${home}/Applications/Mission Control Raise.app` : null
  if (!raise || !(await $.fs.exists(raise).catch(() => false))) {
    $.ui.toast('Mission Control Raise.app is not in ~/Applications, so the band cannot switch windows')
    return
  }
  const r = await $.process.run(['open', '-a', raise])
  if (r.exitCode !== 0) $.ui.toast(`Couldn't start Mission Control Raise: ${r.stderr.trim() || r.exitCode}`)
}

async function clearHome($: $T): Promise<void> {
  const path = await homeFile($)
  if (path) await $.fs.write(path, '{}')
}

/**
 * Open the pane and fill it: what the command, the band and the home session share.
 * The desktop asks for a wide pane (room to read a conversation beside the timeline);
 * the home session, which is there only for this, for all the room the app will give.
 * Both are requests: a width the person dragged the pane to wins, and is kept.
 */
async function openPane($: $T, isHome = false): Promise<boolean> {
  const desktop = (await $.session.surface().catch(() => null)) === 'desktop'
  const columns = desktop ? (isHome ? 1000 : 240) : undefined
  const opened = await $.ui.open({ id: PANE, title: TITLE, ...(columns ? { columns } : {}) })
  await refresh($)
  return opened.isPlaced
}

/**
 * Archive a conversation or bring it back, pin it or unpin it. Only the app's own session
 * and sidebar tools can, and a plugin can't reach the app's tools, so this hands the
 * session a short turn that calls them; the pane refreshes when that turn ends.
 */
type SessionVerb = 'archive' | 'unarchive' | 'pin' | 'unpin'
const DOING: Record<SessionVerb, string> = { archive: 'Archiving', unarchive: 'Unarchiving', pin: 'Pinning',
  unpin: 'Unpinning' }

async function sessionAction($: $T, s: McSession, verb: SessionVerb): Promise<void> {
  if (!s.app || (await read($, actingAtom))) return
  await update($, actingAtom, () => s.app)
  $.ui.toast(`${DOING[verb]} “${truncate(s.title, 40)}”`)
  // not awaited: the command is queued until the session is idle
  $.command.run({ command: SESSION_ACTION, args: `${verb} ${s.app}` }).catch(async err => {
    await update($, actingAtom, () => null)
    $.ui.toast(`Couldn't ${verb} it: ${errText(err)}`)
  })
}

/**
 * Summarize one conversation now, or again: hand this session a digest turn for just that
 * conversation (`/convo-digest:digest conversation <id>`), even if it is unchanged. The
 * pane refreshes when the turn ends. One at a time.
 */
async function summarizeOne($: $T, s: Pick<McSession, 'id' | 'title' | 'digested'>): Promise<void> {
  if (await read($, resummarizingAtom)) return
  await update($, resummarizingAtom, () => s.id)
  $.ui.toast(`${s.digested ? 'Re-summarizing' : 'Summarizing'} “${truncate(s.title, 40)}”`)
  // not awaited: the command is queued until the session is idle
  $.command.run({ command: DIGEST.command, args: `conversation ${s.id}` }).catch(async err => {
    await update($, resummarizingAtom, () => null)
    $.ui.toast(`Couldn't start the digest: ${errText(err)}`)
  })
}

/** Summarize or Re-summarize — not for routine runs, which the digest keeps out. */
function SummarizeButton(el: El, $: $T, s: McSession, key: string, self: string, busy: string | null) {
  const { Button } = el
  if (s.scheduled) return null
  const label = busy === s.id ? 'Summarizing…' : s.digested ? 'Re-summarize' : 'Summarize'
  return <Button key={key} label={label} plain dimColor onPress={() => { void summarizeOne($, s) }} />
}

/** Archive or Unarchive — never for the session the pane runs in, which archiving would end. */
function ArchiveButton(el: El, $: $T, s: McSession, key: string, self: string) {
  const { Button } = el
  if (!s.app || s.id === self) return null
  return <Button key={key} label={s.archived ? 'Unarchive' : 'Archive'} plain dimColor
    onPress={() => { void sessionAction($, s, s.archived ? 'unarchive' : 'archive') }} />
}

/** Pin it in the app's sidebar. Whether it is pinned already only the app knows (in no
 *  file a plugin can read), so this pins; pinning a pinned chat changes nothing, and
 *  unpinning stays in the sidebar's own menu. */
function PinButton(el: El, $: $T, s: McSession, key: string) {
  const { Button } = el
  if (!s.app) return null
  return <Button key={key} label="Pin to sidebar" plain dimColor onPress={() => { void sessionAction($, s, 'pin') }} />
}

/** Pin a conversation to another project; the collector remembers it. */
async function moveSession($: $T, s: McSession, project: string): Promise<void> {
  if (project === s.project) return
  try {
    await collect($, ['move', '--id', s.id, '--project', project])
    $.ui.toast(`Moved “${truncate(s.title, 40)}” to ${project}`)
    await refresh($)
  } catch (err) {
    $.ui.toast(`Couldn't move it: ${errText(err)}`)
  }
}

/** The project picker under a conversation; the list is every known project. */
function MoveTo(el: El, $: $T, s: McSession, snap: McSnapshot) {
  const { Box, Text, Select } = el
  const names = [...new Set([...(snap.projects ?? []), s.project])].sort((a, b) => a.localeCompare(b))
  return (
    <Box key={`mv-${s.id}`} flexDirection="row" gap={1}>
      <Text dimColor>Project</Text>
      <Select key={`move-${s.id}`} value={s.project}
        options={names.map(n => ({ value: n, label: n }))}
        onSelect={v => { void moveSession($, s, v) }} />
    </Box>
  )
}

async function openSessionByApp($: $T, app: string): Promise<void> {
  const r = await $.process.run(['open', `claude://claude.ai/epitaxy/${app}`])
  if (r.exitCode !== 0) $.ui.toast(`Couldn't open the session: ${r.stderr.trim() || r.exitCode}`)
}

function Detail(el: El, $: $T, s: McSession, snap: McSnapshot, now: number, self: string, busy: string | null) {
  const { Box, Text, Button, Markdown } = el
  const tz = snap.tzOffsetMin
  const status = s.scheduled ? 'routine run' : statusLabel(s.status)
  const color = statusColor(s.scheduled ? 'routine' : s.status)
  return (
    <Box key="detail" flexDirection="column" borderStyle="round" borderColor={color} paddingX={1}>
      <Text bold wrap="wrap">{s.title}</Text>
      <Text dimColor wrap="wrap">
        {`${s.project}${s.branch ? ` · ${s.branch}` : ''} · ${when(s.first, snap, now)}–${hm(s.last, tz)} · ` +
          `${duration(s.activeMin)} active · `}
        <Text color={color}>{status}</Text>
        {s.changedSinceDigest ? ' · has new activity since its summary' : ''}
        {s.tickets.length ? ` · ${s.tickets.join(', ')}` : ''}
        {s.archived ? ' · archived' : ''}
      </Text>
      {s.gist
        ? <Markdown text={s.gist} />
        : <Text dimColor>{s.scheduled
          ? 'Scheduled runs are kept out of the digest.'
          : 'Not summarized yet — the next digest run will pick it up.'}</Text>}
      {s.open ? <Text color={statusColor('unresolved')} wrap="wrap">{`Still open: ${s.open}`}</Text> : null}
      {s.needsAction ? <Text color={statusColor('routine-failed')} wrap="wrap">{`Needs action: ${s.needsAction}`}</Text> : null}
      <Box flexDirection="row" gap={2} marginTop={1}>
        <Button key="read-selected" label="Read" variant="primary" hotkey="v"
          onPress={() => { void openReader($, s.id, false) }} />
        <Button key="open-selected" label={s.app ? 'Open in app' : 'Copy resume command'}
          hotkey="o" onPress={e => { void openSession($, s.id, e.surface) }} />
        {SummarizeButton(el, $, s, 'summarize-selected', self, busy)}
        {PinButton(el, $, s, 'pin-selected')}
        {ArchiveButton(el, $, s, 'archive-selected', self)}
        <Button key="clear-selected" label="Clear" plain dimColor
          onPress={() => { void update($, selectedAtom, () => null) }} />
        {s.scheduled ? null : MoveTo(el, $, s, snap)}
      </Box>
    </Box>
  )
}

const ASK_HINT = 'Ask about your work — e.g. what is still open from last week?'

function Ask(el: El, $: $T, ask: McAsk, snap: McSnapshot, view: McView, surface: string) {
  const { Box, Text, Button, Input, Markdown, Client } = el
  const cited = ask.answer ? cites(ask.answer, ask.refs) : []
  const range = snap.range.key === 'today' ? 'today' : snap.range.key === 'yesterday' ? 'yesterday'
    : snap.range.key === '7d' ? 'this week' : snap.range.days.length > 1 ? 'in this period' : 'that day'
  // the two workstreams with most time in view
  const time = new Map<string, number>()
  for (const s of inView(snap, view)) if (s.workstream) time.set(s.workstream, (time.get(s.workstream) ?? 0) + s.activeMin)
  const top = [...time.entries()].sort((a, b) => b[1] - a[1]).slice(0, 2).map(([n]) => n)
  const suggestions = [
    `What did I work on ${range}, by workstream?`,
    'What is still unfinished that I should pick back up?',
    ...top.map(n => `Where does "${n}" stand?`),
  ]
  return (
    <Box flexDirection="column" width="100%">
      {surface === 'desktop'
        // the desktop's Input can't be widened; this draws a full-width field itself
        ? <Client key="askbar" module="./askbar.tsx" width="100%" height={3}
            props={{ placeholder: ASK_HINT, question: ask.question, busy: ask.busy }} />
        : <Input key="ask" placeholder={ASK_HINT} submitLabel="Ask" value={ask.busy ? ask.question : undefined}
            onSubmit={value => { void runAsk($, value) }} />}
      <Box flexDirection="row" flexWrap="wrap" gap={1} marginTop={1}>
        {suggestions.map((q, i) => (
          <Box key={`chip-${i}`} borderStyle="round" borderColor="#3a3a3a" paddingX={1}
            hover={{ borderColor: '#a371f7' }}>
            <Button key={`suggest-${i}`} label={q} plain dimColor onPress={() => { void runAsk($, q) }} />
          </Box>
        ))}
      </Box>
      {ask.busy && surface !== 'desktop'
        ? <Text dimColor>{`Thinking about “${truncate(ask.question, 80)}”…`}</Text> : null}
      {ask.error
        ? (
          <Box flexDirection="row" gap={2}>
            <Text color="#f85149" wrap="wrap">{ask.error}</Text>
            <Button key="ask-clear-error" label="Clear" plain dimColor onPress={() => { void clearAsk($) }} />
          </Box>
        )
        : null}
      {!ask.busy && ask.answer
        ? (
          <Box flexDirection="column" marginTop={1} borderStyle="round" borderColor="#30363d" paddingX={1}>
            <Box flexDirection="row" justifyContent="space-between" gap={2}>
              <Text dimColor wrap="wrap">{ask.question}</Text>
              <Button key="ask-clear" label="Clear ✕" plain dimColor onPress={() => { void clearAsk($) }} />
            </Box>
            <Markdown text={ask.answer} />
            {cited.length
              ? (
                <Box flexDirection="column" marginTop={1}>
                  {cited.map((ref, i) => (
                    <Button key={`cite-${i}`} label={`Open: ${truncate(ref.title, 90)}`} plain
                      onPress={e => { void openCited($, ref, e.surface) }} />
                  ))}
                </Box>
              )
              : null}
          </Box>
        )
        : null}
    </Box>
  )
}

/** The first few bullets of the script: what the folded standup shows. */
function preview(script: string, n: number): { text: string; more: number } {
  const lines = script.split('\n')
  const bullets = lines.filter(l => /^\s*[-*] /.test(l))
  const shown = bullets.slice(0, n).map(l => truncate(l, 140))
  return { text: shown.join('\n'), more: Math.max(0, bullets.length - shown.length) }
}

/** The day's standup brief, up top: folded to its first bullets, the rest a click away. */
function Standup(el: El, $: $T, snap: McSnapshot, now: number, isOpen: boolean) {
  const { Box, Text, Button, Markdown } = el
  const st = snap.standup
  if (!st) {
    return <Text dimColor>No standup brief yet — the standup-brief task writes one each weekday morning.</Text>
  }
  const short = preview(st.script, 3)
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="#3fb950" paddingX={1}>
      <Box flexDirection="row" justifyContent="space-between">
        <Text bold>{st.heading}</Text>
        <Text dimColor>{st.at ? `written ${when(st.at, snap, now)}` : ''}</Text>
      </Box>
      {isOpen
        ? (
          <Box flexDirection="column">
            {st.preamble ? <Text dimColor italic wrap="wrap">{st.preamble}</Text> : null}
            <Markdown text={st.script} />
            {st.table ? <Markdown text={st.table} /> : null}
          </Box>
        )
        : <Markdown text={short.text || truncate(st.script, 300)} />}
      <Box flexDirection="row" gap={2} marginTop={1}>
        <Button key="standup-toggle" plain
          label={isOpen ? '▴ Hide the full standup' : `▾ Show the full standup${short.more ? ` (+${short.more} more)` : ''}`}
          onPress={() => { void update($, standupOpenAtom, v => !v) }} />
        <Button key="standup-copy" label="Copy script" plain dimColor onPress={e => {
          void (async () => {
            const r = await $.ui.copy({ text: `${st.heading}\n\n${st.script}`, surface: e.surface })
            $.ui.toast(r.isCopied ? 'Standup script copied' : "Couldn't copy the script")
          })()
        }} />
        {st.app
          ? <Button key="standup-open" label="Open the run" plain dimColor
              onPress={() => { void openSessionByApp($, st.app as string) }} />
          : null}
      </Box>
    </Box>
  )
}

/** The panel under a clicked conversation on the desktop: what it was, then a way in. */
function Expanded(el: El, $: $T, s: McSession, snap: McSnapshot, now: number, color: string, self: string,
  busy: string | null) {
  const { Box, Text, Button, Markdown } = el
  const tz = snap.tzOffsetMin
  return (
    <Box key={`x-${s.id}`} flexDirection="column" borderStyle="round" borderColor={color}
      paddingX={1} marginLeft={2} marginBottom={1}>
      <Text bold wrap="wrap">{s.title}</Text>
      <Text dimColor wrap="wrap">
        {`${s.project}${s.branch ? ` · ${s.branch}` : ''} · ${when(s.first, snap, now)}–${hm(s.last, tz)} · ` +
          `${duration(s.activeMin)} active${s.tickets.length ? ` · ${s.tickets.join(', ')}` : ''}` +
          (s.archived ? ' · archived' : '')}
      </Text>
      {s.gist
        ? <Markdown text={s.gist} />
        : <Text dimColor>{s.scheduled ? 'A scheduled run — kept out of the digest.'
          : 'Not summarized yet — the next digest run will pick it up.'}</Text>}
      {s.open ? <Text dimColor wrap="wrap">{`Left for later: ${s.open}`}</Text> : null}
      <Box flexDirection="row" gap={2} marginTop={1}>
        <Button key={`read-${s.id}`} label="Read conversation" variant="primary"
          onPress={() => { void openReader($, s.id, false) }} />
        <Button key={`open-${s.id}`} label={s.app ? 'Open ↗' : 'Copy resume command'}
          onPress={e => { void openSession($, s.id, e.surface) }} />
        {SummarizeButton(el, $, s, `summarize-${s.id}`, self, busy)}
        {PinButton(el, $, s, `pin-${s.id}`)}
        {ArchiveButton(el, $, s, `archive-${s.id}`, self)}
        {s.scheduled ? null : MoveTo(el, $, s, snap)}
      </Box>
    </Box>
  )
}

async function openCited($: $T, ref: McRef, surface?: Parameters<$T['ui']['copy']>[0]['surface']) {
  if (ref.app) return openSessionByApp($, ref.app)
  await $.ui.copy({ text: `claude --resume ${ref.id}`, surface })
  $.ui.toast('No desktop session for this one — copied `claude --resume` to the clipboard')
}

function Health(el: El, snap: McSnapshot, now: number) {
  const { Box, Text } = el
  const run = snap.digest.lastRun
  const g = runDot(run?.status === 'ok' ? 'ok' : run?.status === 'nothing' ? 'nothing'
    : run?.status === 'failed' ? 'failed' : run?.status ? 'warn' : undefined)
  return (
    <Box flexDirection="column">
      <Text bold>Digest health</Text>
      <Text wrap="wrap">
        <Text color={g.color}>● </Text>
        {run?.start
          ? `last run ${when(run.start, snap, now)} (${run.trigger ?? '?'}): ${run.status ?? 'never finished'}` +
            `${run.indexed != null ? ` · ${run.indexed} indexed` : ''}` +
            `${run.renamed != null ? ` · ${run.renamed} titles synced` : ''}`
          : 'no runs recorded yet'}
      </Text>
      {snap.digest.issuesSinceOk
        ? <Text color={statusColor('unresolved')}>{`${snap.digest.issuesSinceOk} issue(s) since the last good run`}</Text>
        : <Text dimColor>No issues since the last good run.</Text>}
      {snap.digest.issues.slice(0, 3).map((i, n) => (
        <Text dimColor wrap="truncate-end">
          {`  ${i.ts ? when(i.ts, snap, now) : ''} ${i.kind}: ${truncate(i.detail ?? '', 140)}`}
        </Text>
      ))}
    </Box>
  )
}

/** The pane, drawn; `selfId` is the session it is drawn in. */
async function drawPane($: $T, e: PaneRender, selfId: string) {
  const snap = await read($, snapAtom)
  const view = await read($, viewAtom)
  const selected = await read($, selectedAtom)
  const ask = await read($, askAtom)
  const status = await read($, statusAtom)
  const standupOpen = await read($, standupOpenAtom)
  const summarizing = await read($, summarizingAtom)
  const collapsed = await read($, collapsedAtom)
  const reader = await read($, readerAtom)
  const busyOne = await read($, resummarizingAtom)
  const now = await $.clock.now()
  // wide enough to read a conversation beside the timeline; else it takes the pane
  const wide = e.surface === 'desktop' && e.props.bodyColumns >= WIDE

  if (e.surface === 'mobile' || e.surface === 'vscode') {
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        <Text bold>{TITLE}</Text>
        {(snap?.sessions ?? []).filter(s => !s.scheduled).slice(-20).map(s => (
          <Text wrap="truncate-end">{`${hm(s.first, snap?.tzOffsetMin ?? 0)} ${s.project} · ${s.title}`}</Text>
        ))}
      </Box>
    )
  }

  const el = $.ui.resolve(e) as unknown as El
  const { Box, Text, Button, Select, Client } = el

  if (!snap) {
    return (
      <Box flexDirection="column">
        <Text bold>{TITLE}</Text>
        {status.error
          ? <Text color="#f85149" wrap="wrap">{`Couldn't collect the data: ${status.error}`}</Text>
          : <Text dimColor>Collecting your conversations…</Text>}
        <Button key="refresh" label="Retry" onPress={() => { void refresh($) }} />
      </Box>
    )
  }

  const tz = snap.tzOffsetMin
  const pending = unsummarized(snap, selfId)
  const t = timeline(snap, view, now)
  const shown = inView(snap, view)
  const filtered = Boolean(view.ws || view.kind)
  // the filters' choices: what this range holds, most time first, and the current pick
  const tally = (key: (s: McSession) => string | null) => {
    const m = new Map<string, number>()
    for (const s of snap.sessions) {
      const k = s.scheduled ? null : key(s)
      if (k) m.set(k, (m.get(k) ?? 0) + s.activeMin)
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k)
  }
  const wsChoices = tally(s => s.workstream)
  if (view.ws && !wsChoices.includes(view.ws)) wsChoices.push(view.ws)
  const kindChoices = tally(s => s.kind)
  if (view.kind && !kindChoices.includes(view.kind)) kindChoices.push(view.kind)
  const sel = selected ? snap.sessions.find(s => s.id === selected) ?? null : null
  const multiDay = snap.range.days.length > 1
  const rows = rowsOptions(multiDay)
  const pickDays = Array.from({ length: CUSTOM_DAYS }, (_, i) => dayAt(now, tz, i))
  // older days come from `/mission YYYY-MM-DD..YYYY-MM-DD`: keep those picked
  const pickWith = (v: string | undefined) => v && !pickDays.some(d => d.value === v)
    ? [...pickDays, { value: v, label: v }] : pickDays
  // axis + rows + a blank line before each heading but the first + blank + footer
  // the terminal timeline's rows: axis, headings (a blank line before all but the
  // first), the conversations of unfolded groups, then a blank line and the footer
  let foldedNow = false
  const visible = t.rows.filter(r => {
    if (r.kind === 'header') {
      foldedNow = collapsed.includes(r.label)
      return true
    }
    return !foldedNow
  }).length
  const height = 1 + visible + t.rows.filter((r, i) => r.kind === 'header' && i > 0).length + 2
  const groups = t.rows.filter(r => r.kind === 'header').map(r => r.label)
  const allFolded = groups.length > 0 && groups.every(g => collapsed.includes(g))
  const tl = { collapsed, rows: t.rows, hourFrom: t.hourFrom, hourTo: t.hourTo, ticks: t.ticks, now: t.now,
    nowBase: t.nowBase, selected, order: t.order }
  const readSession = reader ? snap.sessions.find(s => s.id === reader.id) ?? null : null

  if (reader && !wide) {
    // a narrow pane: the conversation takes it over, the way back on top
    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="row" justifyContent="space-between">
          <Button key="reader-back" label={`← ${TITLE} · ${snap.range.label}`} plain
            onPress={() => { void closeReader($) }} />
          <Text dimColor>{reader.loading ? 'reading…' : `updated ${hm(now, tz)}`}</Text>
        </Box>
        {Reader(el, $, reader, readSession, snap, now, selfId, busyOne)}
      </Box>
    )
  }

  return (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold>{`${TITLE} · ${snap.range.label}`}</Text>
          <Box flexDirection="row" gap={1}>
            <Text dimColor>{status.loading ? 'refreshing…' : `updated ${hm(snap.generatedAt, tz)}`}</Text>
            <Button key="refresh" label="Refresh" hotkey="r" plain onPress={() => { void refresh($) }} />
          </Box>
        </Box>
        <Box flexDirection="row" gap={2} alignItems="center" flexWrap="wrap">
          <Text dimColor wrap="wrap">
            {`${snap.totals.conversations} conversations · ${duration(snap.totals.activeMin)} active · ` +
              `${snap.totals.projects} project${snap.totals.projects === 1 ? '' : 's'}`}
          </Text>
          {pending.length || summarizing
            ? <Button key="summarize" plain variant="primary"
                label={summarizing ? 'Summarizing… (follow it in the conversation)'
                  : `Summarize ${pending.length} now`}
                onPress={() => { void summarizeNow($) }} />
            : <Text dimColor>· everything is summarized</Text>}
        </Box>
        {status.error ? <Text color="#f85149" wrap="wrap">{`Last refresh failed: ${status.error}`}</Text> : null}
      </Box>

      {Standup(el, $, snap, now, standupOpen)}

      {Ask(el, $, ask, snap, view, e.surface)}

      <Box flexDirection="row" gap={2} flexWrap="wrap">
        <Select key="range" value={view.range}
          options={[{ value: 'today', label: 'Today' }, { value: 'yesterday', label: 'Yesterday' },
            { value: '7d', label: 'Last 7 days' }, { value: 'custom', label: 'Custom range…' }]}
          onSelect={v => {
            // a first custom range starts as the last two weeks
            void setView($, v === 'custom' && !view.from
              ? { range: 'custom', from: pickDays[13]!.value, to: pickDays[0]!.value }
              : { range: v as McRangeKey })
          }} />
        {view.range === 'custom'
          ? (
            <Box key="custom-range" flexDirection="row" gap={1} alignItems="center">
              <Text dimColor>From</Text>
              <Select key="from" value={view.from ?? pickDays[13]!.value} options={pickWith(view.from)}
                onSelect={v => { void setView($, { from: v }) }} />
              <Text dimColor>to</Text>
              <Select key="to" value={view.to ?? pickDays[0]!.value} options={pickWith(view.to)}
                onSelect={v => { void setView($, { to: v }) }} />
            </Box>
          )
          : null}
        <Select key="rows" value={rows.some(r => r.value === view.rows) ? view.rows : 'project'}
          options={rows} onSelect={v => { void setView($, { rows: v as McRowsMode }) }} />
        {t.rows.some(r => r.kind === 'header')
          ? <Button key="fold-all" plain dimColor label={allFolded ? '▾ Unfold all' : '▸ Fold all'}
              onPress={() => { void update($, collapsedAtom, () => (allFolded ? [] : groups)) }} />
          : null}
        <Select key="filter-ws" value={view.ws ?? '*'}
          options={[{ value: '*', label: 'All workstreams' }, ...wsChoices.slice(0, 60).map(n => ({ value: n, label: n }))]}
          onSelect={v => { void setView($, { ws: v === '*' ? undefined : v }) }} />
        <Select key="filter-kind" value={view.kind ?? '*'}
          options={[{ value: '*', label: 'All kinds of work' }, ...kindChoices.map(k => ({ value: k, label: k }))]}
          onSelect={v => { void setView($, { kind: v === '*' ? undefined : v as McKind }) }} />
        {filtered
          ? <Button key="filter-clear" label="Clear filters" plain dimColor
              onPress={() => { void setView($, { ws: undefined, kind: undefined }) }} />
          : null}
        <Select key="color" value={view.color}
          options={[{ value: 'project', label: 'Colour: project' }, { value: 'status', label: 'Colour: digest status' }]}
          onSelect={v => { void setView($, { color: v as McColorMode }) }} />
      </Box>

      <Box flexDirection="row" gap={2} alignItems="flex-start">
      <Box flexDirection="column" width={wide && reader ? '45%' : '100%'} flexShrink={0}>
        <Text bold>Timeline</Text>
        {filtered
          ? <Text dimColor>{`Showing ${shown.length} of ${snap.totals.conversations} conversations · ` +
              `${duration(workedMinutes(shown))} active` +
              `${view.ws ? ` · ${view.ws}` : ''}${view.kind ? ` · ${view.kind}` : ''}`}</Text>
          : null}
        {e.surface === 'desktop'
          ? (() => {
            // the desktop draws vectors: each row is a clickable name beside its own
            // small strip of bars, so the rows line up and a click opens the session
            const { Svg } = $.ui.resolve(e)
            // beside the reader the timeline has a bit under half the pane
            const cols = wide && reader ? Math.floor(e.props.bodyColumns * 0.45) : e.props.bodyColumns
            // the title matters more than the hours: it gets most of the row, whole
            const labelCells = Math.max(30, Math.min(110, Math.floor(cols * 0.6)))
            const stripPx = Math.max(160, (cols - labelCells - 2) * PX_PER_CELL)
            const strips = timelineStrips(tl, stripPx)
            return (
              <Box flexDirection="column">
                <Box flexDirection="row">
                  <Box width={labelCells} />
                  <Box flexGrow={1}><Svg source={strips.axis} alt="Hours" /></Box>
                </Box>
                {t.rows.map((r, i) => {
                  if (r.kind === 'header') {
                    const isFolded = collapsed.includes(r.label)
                    return (
                      <Box key={`h-${i}`} flexDirection="column" marginTop={i ? 1 : 0}>
                        <Box flexDirection="row" alignItems="center">
                          <Box width={labelCells} flexDirection="row" overflow="hidden">
                            <Button key={`fold-${i}`} plain label={`${isFolded ? '▸' : '▾'} ${r.label}`}
                              onPress={() => { void toggleGroup($, r.label) }} />
                          </Box>
                          {isFolded && strips.rows[i]
                            ? <Box flexGrow={1}><Svg source={strips.rows[i] as string} alt={`${r.label}, all conversations`} /></Box>
                            : null}
                        </Box>
                        <Text dimColor wrap="truncate-end">{`  ${r.sub}`}</Text>
                        {r.note && !isFolded ? <Text dimColor italic wrap="truncate-end">{`  ${r.note}`}</Text> : null}
                      </Box>
                    )
                  }
                  // a folded group hides its conversations
                  const heading = [...t.rows.slice(0, i)].reverse().find(x => x.kind === 'header')
                  if (heading && collapsed.includes(heading.label)) return null
                  const dot = r.bars[0]?.color ?? '#8b949e'
                  const isSel = r.id !== null && r.id === selected
                  const convo = r.id
                  const session = isSel ? snap.sessions.find(x => x.id === r.id) ?? null : null
                  return (
                    <Box key={`r-${i}`} flexDirection="column">
                    <Box flexDirection="row" alignItems="center">
                      <Box width={labelCells} flexDirection="row" overflow="hidden">
                        {convo
                          ? (
                            <Box flexGrow={1} flexShrink={1} flexDirection="row">
                              <Text color={dot}>{isSel ? '▸ ' : '● '}</Text>
                              <Button key={`go-${convo}`} plain label={r.label}
                                onPress={() => {
                                  // wide: read it beside the timeline; narrow: its gist under the row
                                  void (wide ? openReader($, convo, true)
                                    : update($, selectedAtom, cur => (cur === convo ? null : convo)))
                                }} />
                            </Box>
                          )
                          : <Box flexGrow={1} flexShrink={1} overflow="hidden"><Text bold wrap="truncate-end">{r.label}</Text></Box>}
                        <Box width={6} justifyContent="flex-end"><Text dimColor>{r.sub}</Text></Box>
                      </Box>
                      <Box flexGrow={1}><Svg source={strips.rows[i] ?? ''} alt={r.label} /></Box>
                    </Box>
                    {session && !wide ? Expanded(el, $, session, snap, now, dot, selfId, busyOne) : null}
                    </Box>
                  )
                })}
                <Text dimColor>{wide ? 'Click a conversation to read it here.'
                  : 'Click a conversation to see what it was about, then read it or open it.'}</Text>
              </Box>
            )
          })()
          : <Client key="timeline" module="./timeline.tsx" props={tl} width="100%" height={height} />}
        <Box flexDirection="row" gap={2} flexWrap="wrap">
          {t.legend.map((l, i) => (
            <Text><Text color={l.color}>■</Text>{` ${l.label}`}</Text>
          ))}
        </Box>
      </Box>
      {wide && reader
        ? <Box flexGrow={1} flexShrink={1} width="55%">{Reader(el, $, reader, readSession, snap, now, selfId, busyOne)}</Box>
        : null}
      </Box>

      {sel && e.surface !== 'desktop' ? Detail(el, $, sel, snap, now, selfId, busyOne) : null}

      <Box flexDirection="column">
        <Text bold>Routines</Text>
        {Routines(el, $, snap, status.tasks, now)}
      </Box>

      {Health(el, snap, now)}
    </Box>
  )
}

// ------------------------------------------------------------------ register
export const register: Register = on => {
  let ticks = 0
  let selfId = ''
  let paneDrawn = false

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'mission',
      description: 'Mission Control: routines, a clickable timeline of your conversations, and an Ask box',
      argumentHint: '[today|yesterday|week|YYYY-MM-DD..YYYY-MM-DD] | ask <question> | home [off] | band on|off',
    })
    await breadcrumb($, { sessionStartAt: await $.clock.now(), surface: e.surface })
    selfId = await $.session.id().catch(() => '')
    const stored = await $.store.get('view')
    if (stored && typeof stored === 'object') {
      const saved = stored as Partial<McView> & { version?: number }
      // a new default grouping (workstreams) replaces an older saved one, once
      // and the digest's open/solved guess stops being the default colour, once
      const fresh = saved.version === VIEW_VERSION
      await update($, viewAtom, v => ({ ...v, ...saved,
        rows: fresh && saved.rows ? saved.rows : 'workstream',
        color: fresh && saved.color ? saved.color : 'project' }))
    }
    const band = await $.store.get('band')
    if (band === false) await update($, bandAtom, () => false)
    // the home session opens the pane by itself: click it in the sidebar and it's there
    const home = await readHome($)
    if (home && home.cli === (await $.session.id())) {
      await writeHome($)                                // refreshes the desktop id the band links to
      void openPane($, true)
    } else {
      void refresh($)                                   // fills the status line too
    }
    $.clock.every(READ_POLL_MS, () => {
      void (async () => {
        if (!(await read($, readerAtom))) return
        if ((await $.ui.panes()).some(p => p.id === PANE)) await loadReader($, false)
      })()
    })
    $.clock.every(60_000, () => {
      ticks += 1
      void (async () => {
        const up = (await $.ui.panes()).some(p => p.id === PANE)
        if (up || ticks % 10 === 0) await refresh($)
      })()
    })
    return next(e)
  })

  on('command.run', { command: 'mission' }, async ($, e) => {
    const args = e.args.trim()
    const word = args.split(/\s+/)[0]?.toLowerCase() ?? ''
    const second = args.split(/\s+/)[1]?.toLowerCase()
    if (word === 'home') {
      if (second === 'off') {
        await clearHome($)
        return { text: 'This is no longer the Mission Control home session.' }
      }
      await writeHome($)
      await openPane($, true)
      return { text: 'This session is now Mission Control\'s home: it opens the pane by itself, as wide as ' +
        'the app allows, and the Mission Control band in every other chat brings you here. Pin it in the ' +
        'sidebar (right-click → Pin), and give it a window of its own to keep it full screen.' }
    }
    if (word === 'band') {
      const show = second !== 'off'
      await update($, bandAtom, () => show)
      await $.store.set('band', show)
      return { text: show ? 'The Mission Control band shows above the prompt.' : 'The band is hidden. `/mission band on` brings it back.' }
    }
    const range: McRangeKey | undefined = word === 'today' ? 'today' : word === 'yesterday'
      ? 'yesterday' : word === 'week' || word === '7d' ? '7d' : undefined
    const custom = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/.exec(word ?? '')
    if (range) await setView($, { range })
    else if (custom) await setView($, { range: 'custom', from: custom[1], to: custom[2] })
    const placed = range || custom ? (await $.ui.open({ id: PANE, title: TITLE })).isPlaced : await openPane($)
    const opened = { isPlaced: placed }
    if (word === 'ask' && args.length > 3) void runAsk($, args.slice(3))
    return { text: opened.isPlaced ? 'Mission Control is open.' : 'Mission Control opens once there is room for the pane.' }
  })

  on('ui.message', async ($, e, next) => {
    if (e.requestId === PANE && e.element === 'askbar') {
      const data = e.data as { type?: string; question?: unknown; text?: unknown }
      if (data.type === 'copy' && typeof data.text === 'string') {
        await $.ui.copy({ text: data.text, surface: e.surface })
      } else if (typeof data.question === 'string') {
        await runAsk($, data.question)
      }
      return {}
    }
    if (e.requestId !== PANE || e.element !== 'timeline') return next(e)
    const data = e.data as { type?: string; id?: string | null }
    if (data.type === 'select') await update($, selectedAtom, () => data.id ?? null)
    if (data.type === 'open' && data.id) await openSession($, data.id, e.surface)
    if (data.type === 'toggle' && typeof (data as { group?: unknown }).group === 'string') {
      await toggleGroup($, (data as { group: string }).group)
    }
    return {}
  })

  // the digest turn Summarize now started has ended: show what it summarized
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    // the main thread's turn, not one of the digest's own summarizer agents
    if (!e.agentId && (await read($, resummarizingAtom))) {
      await update($, resummarizingAtom, () => null)
      await refresh($)
    }
    if (!e.agentId && (await read($, actingAtom))) {
      await update($, actingAtom, () => null)
      await refresh($)
    }
    if (!e.agentId && (await read($, summarizingAtom))) {
      await update($, summarizingAtom, () => null)
      await refresh($)
    }
    return result
  })

  // a one-line band above the prompt in every session: the way in without typing
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(await read($, bandAtom))) return next(e)
    const snap = await read($, snapAtom)
    const { Box, Text, Button } = $.ui.resolve(e)
    // with a home session elsewhere the band goes there (its window, where it has one);
    // "here" still opens the pane in this chat
    const home = await readHome($)
    const homeApp = home?.app ?? null
    const elsewhere = homeApp !== null && home?.cli !== selfId
    // this chat, summarized now or again (the digest summarizes the chat it runs in only
    // when asked for it by id)
    const here = snap?.sessions.find(s => s.id === selfId)
    const busy = await read($, resummarizingAtom)
    const thisChat = { id: selfId, title: here?.title ?? 'this chat', digested: here?.digested ?? false }
    // other plugins draw here too: ours on top, theirs (what the rest of the chain draws)
    // beneath, so installing this never hides another mod's band
    const rest = await next(e)
    const ours = (
      <Box flexDirection="row" gap={2}>
        {elsewhere
          ? <Button key="band-open" label="◉ Mission Control ↗" plain onPress={() => { void goHome($) }} />
          : <Button key="band-open" label="◉ Mission Control" plain onPress={() => { void openPane($) }} />}
        {elsewhere ? <Button key="band-here" label="open here" plain dimColor onPress={() => { void openPane($) }} /> : null}
        {selfId && !here?.scheduled
          ? <Button key="band-summarize" plain dimColor
              label={busy === selfId ? 'Summarizing this chat…' : thisChat.digested ? 'Re-summarize this chat' : 'Summarize this chat'}
              onPress={() => { void summarizeOne($, thisChat) }} />
          : null}
      </Box>
    )
    return drawsNothing(rest) ? ours : <Box flexDirection="column">{ours}{rest}</Box>
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    // a draw that throws shows its error here, with a way to retry, instead of the
    // engine's blank "has not drawn"; the first good draw and any error go to the trace
    try {
      const drawn = await drawPane($, e, selfId)
      if (!paneDrawn) {
        paneDrawn = true
        await breadcrumb($, { firstPaneDrawAt: await $.clock.now() })
      }
      return drawn
    } catch (err) {
      await breadcrumb($, { lastDrawError: errText(err), lastDrawErrorAt: await $.clock.now(),
        lastDrawStack: String((err as Error)?.stack ?? '').slice(0, 2000) })
      const { Box, Text, Button } = $.ui.resolve(e)
      return (
        <Box flexDirection="column" gap={1}>
          <Text bold>{TITLE}</Text>
          <Text color="#f85149" wrap="wrap">{`Couldn't draw the pane: ${errText(err)}`}</Text>
          <Button key="refresh" label="Retry" onPress={() => { void refresh($) }} />
        </Box>
      )
    }
  })
}

