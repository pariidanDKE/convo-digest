// Mission Control, driven through the engine's own test kit: the collector, the app's
// task server, `open` and the model are stubbed beneath the plugin, so these tests
// exercise the hooks, the drawn trees on each surface, and the timeline Client.
import type { On } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'

import { timeline, workedMinutes } from '../hooks/mission-control/layout'
import type { McSession, McSnapshot } from '../types'

const TZ = 120                                         // CEST
const DAY = Date.UTC(2026, 9, 7, 22)                   // local midnight, Thu 8 Oct
const at = (h: number, m = 0) => DAY + (h * 60 + m) * 60_000
const WEEK = ['Fri 2 Oct', 'Sat 3 Oct', 'Sun 4 Oct', 'Mon 5 Oct', 'Tue 6 Oct', 'Wed 7 Oct', 'Thu 8 Oct']

function session(id: string, fields: Partial<McSession>): McSession {
  const segments = fields.segments ?? [[at(9), at(10)]]
  return {
    id, app: `local_${id}`, apps: [`local_${id}`], title: id, appTitle: id,
    project: 'webapp', root: '/r/webapp', cwd: '/r/webapp', category: 'work',
    status: 'solved', changedSinceDigest: false, digested: true, gist: null, open: null,
    topics: [], tickets: [], branch: null, segments,
    first: segments[0]![0], last: segments[segments.length - 1]![1],
    activeMin: 60, scheduled: null, archived: false, needsAction: null, workstream: null, kind: null,
    ...fields,
  }
}

const SNAP: McSnapshot = {
  generatedAt: at(11, 30), tookMs: 12, tzOffsetMin: TZ,
  range: { key: 'today', label: 'Today', from: DAY, to: DAY + 86_400_000,
    days: [{ date: '2026-10-08', label: 'Thu 8 Oct', from: DAY, to: DAY + 86_400_000 }] },
  sessions: [
    session('nightly', { title: 'Convo digest nightly', scheduled: 'convo-digest-nightly',
      status: 'new', digested: false, segments: [[at(9, 20), at(9, 26)]] }),
    session('wiz', { title: '4821: Goals wizard prompt fix', status: 'unresolved',
      workstream: 'Goals wizard', kind: 'fix',
      gist: 'Stricter prompt; switched model.', open: 'Follow-up Todo for JSON escaping.',
      tickets: ['4821'], segments: [[at(9), at(10)]] }),
    session('review', { title: 'Review 763313', status: 'new', digested: false, app: null, apps: [],
      segments: [[at(10, 30), at(11, 30)]] }),
  ],
  totals: { conversations: 2, activeMin: 120, open: 1, new: 1, projects: 1 },
  routines: [{
    task: 'convo-digest-nightly', name: 'Convo digest nightly', description: null, oneTime: false,
    runs: [{ start: at(9, 20), end: at(9, 26), status: 'ok', detail: '14 indexed', app: 'local_nightly',
      cli: 'nightly', title: 'Convo digest nightly' }],
  }],
  digest: { lastRun: { start: at(9, 20), end: at(9, 26), trigger: 'scheduled', status: 'ok',
    indexed: 14, renamed: 11, note: null }, lastOk: at(9, 20), issues: [], issuesSinceOk: 0 },
  standup: { app: 'local_standup', cli: 'standup', at: at(9, 42), heading: 'Standup script (Wed 7 Oct)',
    script: '**Yesterday**\n- 4821: fixed the Goals wizard prompt', table: '## Full table\n| # | Time |', preamble: null },
  projects: ['convo-digest', 'webapp'],
  workstreams: [{ name: 'Goals wizard', description: 'Stopping the wizard inventing numbers.', count: 3 }],
}

const PANE_PROPS = {
  title: 'Mission Control', isFocused: true, bodyColumns: 120, placement: 'dock' as const,
  scroll: { offset: 0, bodyRows: 80 }, view: {},
}

type Calls = { argv: string[][]; questions: string[]; opened: number; prompts: string[]; toasts: string[];
  copied: string[] }

/** The world beneath the plugin: the collector, `open`, the task server, the model. */
/** Paths that exist on the test's disk beyond what the plugin wrote. */
const exists = new Set<string>()

function world(on: On, opts: { sessionId?: () => string } = {}): Calls {
  exists.clear()
  const calls: Calls = { argv: [], questions: [], opened: 0, prompts: [], toasts: [], copied: [] }
  mock.clock(on, { now: at(11) })
  mock.store(on)
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  on('ui.open', async () => {
    calls.opened += 1
    return { value: { isPlaced: true as const } }
  })
  on('ui.panes', async () => ({ value: [] }))
  on('ui.status', async () => ({ value: undefined }))
  on('ui.toast', async ($, e) => {
    calls.toasts.push(String((e as { text?: unknown }).text ?? ''))
    return { value: undefined }
  })
  on('session.id', async () => ({ value: opts.sessionId?.() ?? 'self-session' }))
  on('env.get', async ($, e) => ({ value: e.name === 'CLAUDE_CODE_HOST_SESSION_ID'
    ? `local_${opts.sessionId?.() ?? 'self-session'}` : e.name === 'HOME' ? '/home/test' : undefined }))
  // the disk: what the plugin wrote, read back (a missing file throws, as on disk)
  const disk = new Map<string, string>()
  on('fs.exists', async ($, e) => ({ value: disk.has(e.path) || exists.has(e.path) }))
  on('fs.write', async ($, e) => {
    disk.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.read', async ($, e) => {
    const text = disk.get(e.path)
    if (text === undefined) throw new Error(`ENOENT: ${e.path}`)
    return { value: text }
  })
  // the engine's end of a turn: hand back what the turn answered
  on('turn.complete', async ($, e) => ({ text: e.answer }))
  const ok = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false,
    isStderrTruncated: false } })
  on('process.run', async ($, e) => {
    calls.argv.push([...e.argv])
    if (e.argv.includes('snapshot')) return ok(JSON.stringify(SNAP))
    if (e.argv.includes('move')) return ok(JSON.stringify({ ok: true }))
    if (e.argv.includes('transcript')) {
      return ok(JSON.stringify({ id: e.argv[e.argv.indexOf('--id') + 1], mtime: 1, total: 2, hidden: 0, turns: [
        { role: 'user', ts: at(9), text: 'The Goals wizard invents numbers again', tools: [] },
        { role: 'assistant', ts: at(9, 5), text: 'Tightened the prompt and switched model.', tools: ['Read ×2', 'Edit'] },
      ] }))
    }
    if (e.argv.includes('ask')) {
      return ok(JSON.stringify({ system: 'sys', prompt: 'p',
        refs: { 1: { id: 'wiz', app: 'local_wiz', title: SNAP.sessions[1]!.title } } }))
    }
    return ok('')
  })
  on('mcp.call', async () => ({ value: { content: [{ type: 'text' as const, text: '[]' }], isError: false } }))
  on('model.complete', async ($, e) => {
    calls.questions.push(e.prompt)
    const text = 'You fixed the Goals wizard prompt [1].'
    return { value: { isAnswered: true as const, text,
      usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 } } }
  })
  on('ui.copy', async ($, e) => {
    calls.copied.push(e.text)
    return { value: { isCopied: true as const } }
  })
  on('command.run', { command: 'convo-digest:digest' }, async ($, e) => {
    calls.prompts.push(`/${e.command} ${e.args}`)
    return { text: '' }
  })
  on('command.run', { command: 'convo-digest:session' }, async ($, e) => {
    calls.prompts.push(`/${e.command} ${e.args}`)
    return { text: '' }
  })
  // what the engine draws above the prompt when no plugin draws there: nothing
  on('ui.render', { component: 'AbovePrompt' }, async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{''}</Text>
  })
  return calls
}

describe('mission control', () => {
  test('the command opens the pane and the routine and timeline draw on terminal and desktop', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    expect(calls.argv.some(a => a.includes('snapshot'))).toBe(true)
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'convo-digest', surface, component: 'Pane',
        requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
      expect(await ui.find({ text: /Mission Control · Today/ })).toBeDefined()
      expect(await ui.find({ text: /Digest/ })).toBeDefined()
      if (surface === 'desktop') {
        // there each row is a clickable name beside its own vector strip of bars
        // the hour axis plus one strip per conversation; routine runs aren't drawn here
        expect((await ui.findAll({ type: 'Svg' })).length).toBe(3)
        expect(await ui.find({ text: /Goals wizard/ })).toBeDefined()
        expect(await ui.find({ key: 'go-wiz' })).toBeDefined()
      } else {
        // routines have their own section; the timeline is conversations only
        expect(await ui.find({ in: 'timeline', text: /Digest/ })).toBeUndefined()
        // grouped by workstream, the unsummarized last
        expect(await ui.find({ in: 'timeline', text: /Goals wizard/ })).toBeDefined()
        expect(await ui.find({ in: 'timeline', text: /Not summarized yet/ })).toBeDefined()
      }
      await ui.unmount()
    }
  })

  test('on the desktop a click shows a conversation in place, and Open in app opens it', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.press({ key: 'go-wiz' })
    // nothing jumps on a click: the gist shows under the row, with the way in
    expect(calls.argv.some(a => a[0] === 'open')).toBe(false)
    expect(await ui.find({ type: 'Markdown', text: /Stricter prompt; switched model/ })).toBeDefined()
    await ui.press({ key: 'open-wiz' })
    expect(calls.argv).toContainEqual(['open', 'claude://claude.ai/epitaxy/local_wiz'])
    // a second click on the name folds it away again
    await ui.press({ key: 'go-wiz' })
    expect(await ui.find({ key: 'open-wiz' })).toBeUndefined()
    // a conversation with no desktop session offers its resume command instead
    await ui.press({ key: 'go-review' })
    expect(await ui.find({ key: 'open-review', text: /Copy resume command/ })).toBeDefined()
    await ui.unmount()
  })

  test('in the terminal clicking a bar selects it, and Open in app opens its claude:// link', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'terminal', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    // grouped by project for a fixed layout (themes put each conversation under its own heading)
    await ui.select({ key: 'rows', value: 'project' })
    // 120 columns: a 66-cell label column, a gap, then 53 track cells over 08:00–16:00
    // (6.625 a hour). Rows: axis 0, webapp 1, then one row per conversation:
    // Goals (09:00–10:00, cells 6–14) at 2, Review at 3.
    await ui.pointer({ type: 'down', button: 'left', x: 66 + 1 + 10, y: 2, in: 'timeline' })
    await ui.pointer({ type: 'up', button: 'left', x: 66 + 1 + 10, y: 2, in: 'timeline' })
    expect(await ui.find({ text: /Still open: Follow-up Todo/ })).toBeDefined()
    await ui.press({ key: 'open-selected' })
    expect(calls.argv).toContainEqual(['open', 'claude://claude.ai/epitaxy/local_wiz'])
    // a conversation's name in the label column selects it too
    await ui.pointer({ type: 'down', button: 'left', x: 4, y: 3, in: 'timeline' })
    expect(await ui.find({ key: 'open-selected', text: /Copy resume command/ })).toBeDefined()
    await ui.unmount()
  })

  test('arrow keys step through conversations; one without an app session copies its resume command', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'terminal', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    for (let i = 0; i < 3; i++) await ui.key({ key: 'down', in: 'timeline' })
    expect(await ui.find({ text: /Review 763313/ })).toBeDefined()
    expect(await ui.find({ key: 'open-selected', text: /Copy resume command/ })).toBeDefined()
    await ui.press({ key: 'open-selected' })
    expect(calls.argv.some(a => a[0] === 'open')).toBe(false)
    await ui.unmount()
  })

  test('the Ask box answers from the index and links the conversations it cites', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    // the desktop draws its own full-width field: click it, type (a paste arrives whole), fix a typo, Enter
    expect(await ui.find({ key: 'ask' })).toBeUndefined()
    await ui.pointer({ type: 'down', button: 'left', x: 10, y: 1, in: 'askbar' })
    await ui.key({ key: 'what did I fix today', in: 'askbar' })
    for (const k of ['x', 'backspace', '?']) await ui.key({ key: k, in: 'askbar' })
    await ui.key({ key: 'return', in: 'askbar' })
    expect(calls.argv.some(a => a.includes('ask') && a.includes('what did I fix today?'))).toBe(true)
    // Cmd+A selects it all: copy goes to the clipboard, and typing replaces the selection
    await ui.key({ key: 'a', meta: true, in: 'askbar' })
    await ui.key({ key: 'c', meta: true, in: 'askbar' })
    expect(calls.copied).toEqual(['what did I fix today?'])
    await ui.key({ key: 'why', in: 'askbar' })
    await ui.key({ key: 'return', in: 'askbar' })
    expect(calls.argv.some(a => a.includes('ask') && a.includes('why'))).toBe(true)
    expect(await ui.find({ type: 'Markdown', text: /Goals wizard/ })).toBeDefined()
    await ui.press({ key: 'cite-0' })
    expect(calls.argv).toContainEqual(['open', 'claude://claude.ai/epitaxy/local_wiz'])
    // Clear drops the answer, and the field empties for the next question
    await ui.press({ key: 'ask-clear' })
    expect(await ui.find({ type: 'Markdown', text: /You fixed the Goals wizard/ })).toBeUndefined()
    expect(await ui.find({ key: 'ask-clear' })).toBeUndefined()
    const bar = await ui.find({ key: 'askbar' })
    expect((bar?.props as { props?: { question?: string } } | undefined)?.props?.question).toBe('')
    // a suggested question asks with one click
    await ui.press({ key: 'suggest-1' })
    expect(calls.argv.some(a => a.includes('What is still unfinished that I should pick back up?'))).toBe(true)
    // the field shows the question that was asked, and its pill asks it again
    const asks = calls.argv.filter(a => a.includes('ask')).length
    await ui.pointer({ type: 'down', button: 'left', x: 115, y: 1, in: 'askbar' })
    expect(calls.argv.filter(a => a.includes('ask')).length).toBe(asks + 1)
    expect(calls.argv[calls.argv.length - 1]).toContain('What is still unfinished that I should pick back up?')
    await ui.unmount()
  })

  test('the terminal keeps its own Ask input', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'terminal', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.input({ key: 'ask', text: 'what did I fix today?' })
    expect(calls.argv.some(a => a.includes('ask') && a.includes('what did I fix today?'))).toBe(true)
    await ui.unmount()
  })

  test('conversations are grouped by their workstream, and filtered by workstream and kind', async ($, on) => {
    world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    // a workstream heading is a fold button with the epic's description under it
    expect(await ui.find({ type: 'Button', text: /Goals wizard/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Stopping the wizard inventing numbers/ })).toBeDefined()
    expect(await ui.find({ type: 'Button', text: /Not summarized yet/ })).toBeDefined()
    // only the fixes: the review (not summarized, no kind) drops out
    await ui.select({ key: 'filter-kind', value: 'fix' })
    expect(await ui.find({ key: 'go-review' })).toBeUndefined()
    expect(await ui.find({ key: 'go-wiz' })).toBeDefined()
    expect(await ui.find({ text: /Showing 1 of 2 conversations/ })).toBeDefined()
    await ui.press({ key: 'filter-clear' })
    expect(await ui.find({ key: 'go-review' })).toBeDefined()
    await ui.select({ key: 'filter-ws', value: 'Goals wizard' })
    expect(await ui.find({ key: 'go-review' })).toBeUndefined()
    await ui.unmount()
  })

  test('the standup brief is up top, folded to its first bullets, unfolding in full', async ($, on) => {
    world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    expect(await ui.find({ text: /Standup script \(Wed 7 Oct\)/ })).toBeDefined()
    // folded: the first bullets only; unfolded: the whole brief, table included
    expect(await ui.find({ type: 'Markdown', text: /fixed the Goals wizard prompt/ })).toBeDefined()
    expect(await ui.find({ type: 'Markdown', text: /Full table/ })).toBeUndefined()
    await ui.press({ key: 'standup-toggle' })
    expect(await ui.find({ type: 'Markdown', text: /Full table/ })).toBeDefined()
    await ui.press({ key: 'standup-toggle' })
    expect(await ui.find({ type: 'Markdown', text: /Full table/ })).toBeUndefined()
    await ui.press({ key: 'standup-copy' })
    await ui.unmount()
  })

  test('the band above the prompt opens Mission Control in one click, and can be hidden', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    const band = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'AbovePrompt',
      props: { hasSurvey: false } as never })
    await band.press({ key: 'band-open' })
    expect(calls.opened).toBe(1)
    expect(calls.argv.some(a => a.includes('snapshot'))).toBe(true)
    await band.unmount()
    await $.command.run({ command: 'mission', args: 'band off', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const quiet = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'AbovePrompt',
      props: { hasSurvey: false } as never })
    expect(await quiet.find({ key: 'band-open' })).toBeUndefined()
    await quiet.unmount()
  })

  test('with a home session, the band in any other chat goes there; "open here" stays', async ($, on) => {
    let sid = 'home'
    const calls = world(on, { sessionId: () => sid })
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: 'home', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    // in the home chat itself the band just opens the pane
    const atHome = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'AbovePrompt',
      props: { hasSurvey: false } as never })
    expect(await atHome.find({ key: 'band-here' })).toBeUndefined()
    await atHome.unmount()
    // another chat: the band links to the home chat
    sid = 'other'
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    const band = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'AbovePrompt',
      props: { hasSurvey: false } as never })
    const opened = calls.opened
    // no raise app installed: it says so, and opens nothing
    await band.press({ key: 'band-open' })
    expect(calls.argv.some(a => a[0] === 'open')).toBe(false)
    expect(calls.toasts.some(t => /Mission Control Raise\.app is not in/.test(t))).toBe(true)
    // with it, the band opens that, which switches to the home chat's own window
    calls.argv.length = 0
    exists.add('/home/test/Applications/Mission Control Raise.app')
    await band.press({ key: 'band-open' })
    expect(calls.argv).toEqual([['open', '-a', '/home/test/Applications/Mission Control Raise.app']])
    expect(calls.opened).toBe(opened)                                   // no pane in this chat
    await band.press({ key: 'band-here' })
    expect(calls.opened).toBe(opened + 1)
    await band.unmount()
  })

  test('Summarize now hands the digest a turn and refreshes when that turn ends', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    // two conversations in the fixture were never summarized (Review, and the nightly is a routine)
    expect(await ui.find({ key: 'summarize', text: /Summarize 1 now/ })).toBeDefined()
    await ui.press({ key: 'summarize' })
    expect(calls.toasts).toEqual([])
    expect(calls.prompts).toEqual(['/convo-digest:digest now'])
    expect(await ui.find({ key: 'summarize', text: /Summarizing/ })).toBeDefined()
    // a second press while it runs starts nothing new
    await ui.press({ key: 'summarize' })
    expect(calls.prompts.length).toBe(1)
    // a summarizer agent finishing is not the digest finishing
    const before = calls.argv.filter(a => a.includes('snapshot')).length
    await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 'a1', agentId: 'sub', reason: 'answer' })
    expect(await ui.find({ key: 'summarize', text: /Summarizing/ })).toBeDefined()
    await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
    expect(calls.argv.filter(a => a.includes('snapshot')).length).toBeGreaterThan(before)
    expect(await ui.find({ key: 'summarize', text: /Summarize 1 now/ })).toBeDefined()
    await ui.unmount()
  })

  test('a group folds away to one combined strip and unfolds again; all start unfolded', async ($, on) => {
    world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.select({ key: 'rows', value: 'project' })
    expect(await ui.find({ key: 'go-wiz' })).toBeDefined()
    expect(await ui.find({ key: 'fold-0', text: /▾ webapp/ })).toBeDefined()
    await ui.press({ key: 'fold-0' })
    expect(await ui.find({ key: 'go-wiz' })).toBeUndefined()
    expect(await ui.find({ key: 'fold-0', text: /▸ webapp/ })).toBeDefined()
    // the folded heading carries its group's bars in one strip: axis + that strip
    expect((await ui.findAll({ type: 'Svg' })).length).toBe(2)
    await ui.press({ key: 'fold-0' })
    expect(await ui.find({ key: 'go-wiz' })).toBeDefined()
    await ui.press({ key: 'fold-all' })
    expect(await ui.find({ key: 'go-wiz' })).toBeUndefined()
    await ui.press({ key: 'fold-all' })
    expect(await ui.find({ key: 'go-wiz' })).toBeDefined()
    await ui.unmount()
  })

  test('in the terminal a click on a heading folds its group', async ($, on) => {
    world(on)
    await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'terminal', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.select({ key: 'rows', value: 'project' })
    expect(await ui.find({ in: 'timeline', text: /Goals wizard/ })).toBeDefined()
    await ui.pointer({ type: 'down', button: 'left', x: 2, y: 1, in: 'timeline' })
    expect(await ui.find({ in: 'timeline', text: /Goals wizard/ })).toBeUndefined()
    expect(await ui.find({ in: 'timeline', text: /▸ webapp/ })).toBeDefined()
    await ui.unmount()
  })

  test('a conversation can be moved to another project from its panel', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.press({ key: 'go-wiz' })
    const before = calls.argv.filter(a => a.includes('snapshot')).length
    await ui.select({ key: 'move-wiz', value: 'convo-digest' })
    expect(calls.argv.some(a => a.includes('move') && a.includes('wiz') && a.includes('convo-digest'))).toBe(true)
    // the pane re-collects so the conversation shows under its new project
    expect(calls.argv.filter(a => a.includes('snapshot')).length).toBeGreaterThan(before)
    expect(calls.toasts.some(t => /Moved .* to convo-digest/.test(t))).toBe(true)
    await ui.unmount()
  })

  test('Archive hands the app a turn for that one session, with no copy-resume button beside it', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.press({ key: 'go-wiz' })
    expect(await ui.find({ key: 'copy-wiz' })).toBeUndefined()
    await ui.press({ key: 'archive-wiz' })
    expect(calls.prompts).toEqual(['/convo-digest:session archive local_wiz'])
    // one at a time: a second click while that turn runs starts nothing
    await ui.press({ key: 'archive-wiz' })
    expect(calls.prompts.length).toBe(1)
    const before = calls.argv.filter(a => a.includes('snapshot')).length
    await $.turn.complete({ answer: 'Archived.', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
    expect(calls.argv.filter(a => a.includes('snapshot')).length).toBeGreaterThan(before)
    // pinning goes the same way, once the archive turn is over
    await ui.press({ key: 'pin-wiz' })
    expect(calls.prompts).toEqual(['/convo-digest:session archive local_wiz', '/convo-digest:session pin local_wiz'])
    // a conversation with no desktop session can't be archived or pinned from here
    await ui.press({ key: 'go-review' })
    expect(await ui.find({ key: 'archive-review' })).toBeUndefined()
    expect(await ui.find({ key: 'pin-review' })).toBeUndefined()
    await ui.unmount()
  })

  test('a custom range collects the days picked, both ends included', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    expect(await ui.find({ key: 'from' })).toBeUndefined()
    // a first custom range is the last two weeks
    await ui.select({ key: 'range', value: 'custom' })
    expect(calls.argv.some(a => a.includes('2026-09-25..2026-10-08'))).toBe(true)
    await ui.select({ key: 'from', value: '2026-10-01' })
    expect(calls.argv.some(a => a.includes('2026-10-01..2026-10-08'))).toBe(true)
    // a To before From swaps the two
    await ui.select({ key: 'to', value: '2026-09-28' })
    expect(calls.argv.some(a => a.includes('2026-09-28..2026-10-01'))).toBe(true)
    await ui.unmount()
  })

  test('/mission takes a range of days too', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'mission', args: '2026-09-01..2026-09-30', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    expect(calls.argv.some(a => a.includes('2026-09-01..2026-09-30'))).toBe(true)
  })

  test('in a narrow pane a conversation is read in place of the overview, and Close comes back', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.press({ key: 'go-wiz' })
    await ui.press({ key: 'read-wiz' })
    expect(calls.argv.some(a => a.includes('transcript') && a.includes('wiz'))).toBe(true)
    // the gist first; the messages are folded behind their count until asked for
    expect(await ui.find({ key: 'reader-toggle', text: /▸ Conversation · 2 messages/ })).toBeDefined()
    expect(await ui.find({ type: 'Markdown', text: /invents numbers again/ })).toBeUndefined()
    await ui.press({ key: 'reader-toggle' })
    expect(await ui.find({ type: 'Markdown', text: /invents numbers again/ })).toBeDefined()
    expect(await ui.find({ text: /Read ×2 · Edit/ })).toBeDefined()
    expect(await ui.find({ key: 'go-review' })).toBeUndefined()           // the overview made way
    await ui.press({ key: 'reader-open' })
    expect(calls.argv).toContainEqual(['open', 'claude://claude.ai/epitaxy/local_wiz'])
    await ui.press({ key: 'reader-close' })
    expect(await ui.find({ key: 'reader' })).toBeUndefined()
    expect(await ui.find({ key: 'go-review' })).toBeDefined()
    await ui.unmount()
  })

  test('in a wide pane a click reads the conversation beside the timeline', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 220 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: { ...PANE_PROPS, bodyColumns: 200 }, viewport: { columns: 200, rows: 80 } })
    await ui.press({ key: 'go-wiz' })
    expect(await ui.find({ key: 'reader' })).toBeDefined()
    expect(await ui.find({ key: 'go-review' })).toBeDefined()             // the timeline stays
    expect(await ui.find({ key: 'read-wiz' })).toBeUndefined()            // no gist panel under the row
    // another conversation replaces it; a second click on it closes the reader
    await ui.press({ key: 'go-review' })
    expect(calls.argv.some(a => a.includes('transcript') && a.includes('review'))).toBe(true)
    await ui.press({ key: 'go-review' })
    expect(await ui.find({ key: 'reader' })).toBeUndefined()
    await ui.unmount()
  })

  test('one conversation can be summarized, or summarized again, from its panel', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'desktop', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.press({ key: 'go-wiz' })
    expect(await ui.find({ key: 'summarize-wiz', text: /Re-summarize/ })).toBeDefined()
    await ui.press({ key: 'summarize-wiz' })
    expect(calls.prompts).toEqual(['/convo-digest:digest conversation wiz'])
    expect(await ui.find({ key: 'summarize-wiz', text: /Summarizing…/ })).toBeDefined()
    // one at a time
    await ui.press({ key: 'summarize-wiz' })
    expect(calls.prompts.length).toBe(1)
    const before = calls.argv.filter(a => a.includes('snapshot')).length
    await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
    expect(calls.argv.filter(a => a.includes('snapshot')).length).toBeGreaterThan(before)
    expect(await ui.find({ key: 'summarize-wiz', text: /Re-summarize/ })).toBeDefined()
    // a conversation never summarized says so
    await ui.press({ key: 'go-review' })
    expect(await ui.find({ key: 'summarize-review', text: /^Summarize$/ })).toBeDefined()
    await ui.unmount()
  })

  test('picking a range re-collects for that range', async ($, on) => {
    const calls = world(on)
    await $.session.start({ cwd: '/tmp', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: true, columns: 160 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'terminal', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS, viewport: { columns: 120, rows: 80 } })
    await ui.select({ key: 'range', value: '7d' })
    expect(calls.argv.some(a => a.includes('--range') && a.includes('7d'))).toBe(true)
    await ui.unmount()
  })

  test('the narrow surfaces get a plain list', async ($, on) => {
    world(on)
    await $.session.start({ cwd: '/tmp', surface: 'mobile', isInteractive: true })
    await $.command.run({ command: 'mission', args: '', origin: { kind: 'composer' },
      presentation: { isFullscreen: false, columns: 60 } })
    const ui = await $.ui.mount({ plugin: 'convo-digest', surface: 'mobile', component: 'Pane',
      requestId: 'mission-control', props: PANE_PROPS })
    expect(await ui.find({ text: /Goals wizard/ })).toBeDefined()
    await ui.unmount()
  })
})

describe('layout', () => {
  test('a day is one named row per conversation under its project, no routine runs', async () => {
    const snap: McSnapshot = { ...SNAP, sessions: [...SNAP.sessions,
      session('parallel', { title: 'Parallel work', segments: [[at(9, 30), at(10, 15)]] })] }
    const t = timeline(snap, { range: 'today', rows: 'project', color: 'status' }, at(11))
    expect(t.hourFrom).toBe(8)
    expect(t.hourTo).toBe(16)
    expect(t.rows.map(r => r.label)).toEqual(['webapp',
      '4821: Goals wizard prompt fix', 'Parallel work', 'Review 763313'])
    expect(t.order).toEqual(['wiz', 'parallel', 'review'])
  })

  test('in every grouping, the conversation with the most time spent comes first', async () => {
    const snap: McSnapshot = { ...SNAP, sessions: [
      session('short', { title: 'Short early one', status: 'solved', activeMin: 10, segments: [[at(8), at(8, 10)]] }),
      session('long', { title: 'Long late one', status: 'solved', activeMin: 120, segments: [[at(12), at(14)]] }),
      session('mid', { title: 'Middle one', status: 'solved', activeMin: 45, segments: [[at(10), at(10, 45)]] }),
    ] }
    for (const rows of ['project', 'status'] as const) {
      const t = timeline(snap, { range: 'today', rows, color: 'project' }, at(15))
      expect(t.order).toEqual(['long', 'mid', 'short'])
    }
  })

  test('a heading counts conversations that ran side by side once', async () => {
    const snap: McSnapshot = { ...SNAP, sessions: [
      session('a', { segments: [[at(9), at(11)]], activeMin: 120 }),
      session('b', { segments: [[at(10), at(12)]], activeMin: 120 }),
    ] }
    expect(workedMinutes(snap.sessions)).toBe(180)
    expect(workedMinutes(snap.sessions, at(10, 30), at(11, 30))).toBe(60)
    const t = timeline(snap, { range: 'today', rows: 'project', color: 'project' }, at(11))
    const header = t.rows.find(r => r.kind === 'header')
    expect(header?.sub).toBe('2 conversations · 3h')
  })

  test('the week view can be one row per day', async () => {
    const days = WEEK.map((label, i) => ({ date: String(i), label,
      from: DAY - (6 - i) * 86_400_000, to: DAY - (5 - i) * 86_400_000 }))
    const snap: McSnapshot = { ...SNAP, range: { key: '7d', label: 'Last 7 days',
      from: days[0]!.from, to: days[6]!.to, days } }
    const t = timeline(snap, { range: '7d', rows: 'day', color: 'project' }, at(11))
    expect(t.rows.map(r => r.label)).toEqual(WEEK)
  })
})
