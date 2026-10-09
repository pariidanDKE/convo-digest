// Pure layout for the Mission Control pane: snapshot + view → the timeline's rows,
// the legend, and the small formatters. No `$`, no clock: times are placed against
// the local midnights the collector computed and formatted with its timezone offset,
// so the sandbox's own timezone never matters.
import type {
  McBar,
  McColorMode,
  McRoutine,
  McRow,
  McRowsMode,
  McSession,
  McSnapshot,
  McTick,
  McTimelineProps,
  McView,
} from '../../types'

const HOUR = 3_600_000
const DAY = 86_400_000

export const STATUS_COLORS: Record<string, string> = {
  solved: '#3fb950',
  unresolved: '#d29922',
  exploratory: '#58a6ff',
  abandoned: '#8b949e',
  new: '#bc8cff',
  routine: '#8b949e',
  'routine-failed': '#f85149',
  'routine-warn': '#d29922',
}

export const STATUS_LABELS: Record<string, string> = {
  solved: 'solved',
  unresolved: 'open',
  exploratory: 'exploratory',
  abandoned: 'abandoned',
  new: 'not summarized yet',
  routine: 'routine run',
}

const PROJECT_COLORS = ['#58a6ff', '#3fb950', '#d29922', '#bc8cff', '#f778ba', '#39c5cf',
  '#ff9a4c', '#a5d6ff', '#7ee787', '#e3b341']

/** A run's status as a coloured dot (the routine history) and a word. */
export type Dot = { glyph: string; color: string; word: string }
const DOTS: Record<string, Dot> = {
  ok: { glyph: '●', color: '#3fb950', word: 'ok' },
  nothing: { glyph: '●', color: '#6e7681', word: 'nothing to do' },
  warn: { glyph: '●', color: '#d29922', word: 'needs a look' },
  failed: { glyph: '●', color: '#f85149', word: 'failed' },
  running: { glyph: '●', color: '#58a6ff', word: 'running' },
}
const UNKNOWN_DOT: Dot = { glyph: '○', color: '#6e7681', word: 'no summary' }
export const runDot = (status: string | null | undefined): Dot =>
  (status ? DOTS[status] : undefined) ?? UNKNOWN_DOT
export const statusColor = (status: string): string => STATUS_COLORS[status] ?? '#8b949e'
export const statusLabel = (status: string): string => STATUS_LABELS[status] ?? status

const NAMES: Record<string, string> = {
  'convo-digest-nightly': 'Digest',
  'standup-brief': 'Standup brief',
  'ai-daily-writeup': 'AI daily write-up',
}

/** "convo-digest-nightly" → "Digest"; others title-cased, "ai" kept as "AI". */
export function routineName(r: Pick<McRoutine, 'task' | 'name'>): string {
  const known = NAMES[r.task]
  if (known) return known
  const words = r.task.replace(/-\d{4}-\d\d-\d\d$/, '').split('-').filter(Boolean)
  const name = words.map((w, i) => (w === 'ai' ? 'AI' : i === 0 ? w[0]!.toUpperCase() + w.slice(1) : w)).join(' ')
  const date = r.task.match(/(\d{4}-\d\d-\d\d)$/)?.[1]
  return date ? `${name} (one-off ${date})` : name
}

// ---------------------------------------------------------------- formatting
const pad = (n: number) => String(n).padStart(2, '0')

/** Local clock parts of an epoch-ms instant, from the collector's offset. */
export function local(ms: number, tzOffsetMin: number) {
  const d = new Date(ms + tzOffsetMin * 60_000)
  return { hh: d.getUTCHours(), mm: d.getUTCMinutes(), day: d.getUTCDate(),
    month: d.getUTCMonth(), dow: d.getUTCDay() }
}

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export function hm(ms: number, tz: number): string {
  const p = local(ms, tz)
  return `${pad(p.hh)}:${pad(p.mm)}`
}

export function dayLabel(ms: number, tz: number): string {
  const p = local(ms, tz)
  return `${DOW[p.dow]} ${p.day} ${MON[p.month]}`
}

/** "today 09:22", "yesterday 18:04", or "Mon 5 Oct 09:22". */
export function when(ms: number, snap: McSnapshot, nowMs: number): string {
  const tz = snap.tzOffsetMin
  const sameDay = (a: number, b: number) => {
    const x = local(a, tz)
    const y = local(b, tz)
    return x.day === y.day && x.month === y.month
  }
  if (sameDay(ms, nowMs)) return `today ${hm(ms, tz)}`
  if (sameDay(ms, nowMs - DAY)) return `yesterday ${hm(ms, tz)}`
  return `${dayLabel(ms, tz)} ${hm(ms, tz)}`
}

/**
 * Time worked across conversations, counting parallel ones once: the union of their
 * active stretches, optionally clipped to [from, to).
 */
export function workedMinutes(sessions: McSession[], from = -Infinity, to = Infinity): number {
  const merged: [number, number][] = []
  const segs = sessions.flatMap(s => s.segments)
    .map(([a, b]) => [Math.max(a, from), Math.min(b, to)] as [number, number])
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0])
  for (const [a, b] of segs) {
    const last = merged[merged.length - 1]
    if (last && a <= last[1]) last[1] = Math.max(last[1], b)
    else merged.push([a, b])
  }
  return Math.round(merged.reduce((n, [a, b]) => n + (b - a) / 60_000, 0))
}

export function duration(min: number): string {
  if (min < 60) return `${min}m`
  const h = Math.floor(min / 60)
  const m = min % 60
  return m ? `${h}h${pad(m)}` : `${h}h`
}

export function truncate(text: string, max: number): string {
  const t = text.replace(/\s+/g, ' ').trim()
  return t.length <= max ? t : `${t.slice(0, Math.max(1, max - 1))}…`
}

// ---------------------------------------------------------------- colouring
export function projectColors(sessions: McSession[]): Record<string, string> {
  const names = [...new Set(sessions.filter(s => !s.scheduled).map(s => s.project))].sort()
  return Object.fromEntries(names.map((n, i) => [n, PROJECT_COLORS[i % PROJECT_COLORS.length] ?? '#58a6ff']))
}

/** The colour half-way to grey: what a bar looks like while another one is selected. */
export function mute(hex: string): string {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex)
  if (!m) return hex
  const grey = 0x6e
  const mix = (c: string) => Math.round((parseInt(c, 16) + grey * 2) / 3).toString(16).padStart(2, '0')
  return `#${mix(m[1]!)}${mix(m[2]!)}${mix(m[3]!)}`
}

function runStatus(s: McSession, snap: McSnapshot): string {
  for (const r of snap.routines) {
    const run = r.runs.find(x => x.cli === s.id)
    if (run?.status === 'failed') return 'routine-failed'
    if (run?.status === 'warn') return 'routine-warn'
  }
  return 'routine'
}

export function colorOf(s: McSession, snap: McSnapshot, mode: McColorMode,
  projects: Record<string, string>): string {
  if (s.scheduled) return statusColor(runStatus(s, snap))
  if (mode === 'project') return projects[s.project] ?? '#8b949e'
  return statusColor(s.status)
}

export function legend(snap: McSnapshot, view: McView): { label: string; color: string }[] {
  if (view.color === 'project') {
    return Object.entries(projectColors(snap.sessions)).map(([label, color]) => ({ label, color }))
  }
  const present = new Set(snap.sessions.filter(s => !s.scheduled).map(s => s.status))
  const out = ['solved', 'unresolved', 'exploratory', 'abandoned', 'new']
    .filter(k => present.has(k))
    .map(k => ({ label: statusLabel(k), color: statusColor(k) }))
  return out
}

// ---------------------------------------------------------------- timeline
function tip(s: McSession, tz: number): string {
  const status = s.scheduled ? 'routine run' : statusLabel(s.status)
  return `${hm(s.first, tz)}–${hm(s.last, tz)} · ${duration(s.activeMin)} active · ${s.project} · ` +
    `${status} · ${s.title}`
}

/** Where a conversation without a workstream or kind yet goes, last. */
export const UNFILED = 'Not summarized yet'

function groupKey(s: McSession, rows: McRowsMode): string {
  if (rows === 'status') return statusLabel(s.status)
  if (rows === 'category') return s.category === 'unknown' ? 'not profiled' : s.category
  if (rows === 'workstream') return s.workstream ?? UNFILED
  if (rows === 'kind') return s.kind ?? UNFILED
  return s.project
}

/** The conversations a view shows: routine runs left out, then its workstream and kind
 *  filters applied. */
export function visible(snap: McSnapshot, view: McView): McSession[] {
  return snap.sessions.filter(s => !s.scheduled && (!view.ws || s.workstream === view.ws) &&
    (!view.kind || s.kind === view.kind))
}

const STATUS_ORDER = ['open', 'not summarized yet', 'exploratory', 'solved', 'abandoned']

/** Snapshot + view → everything the timeline Client draws, plus the legend. */
export function timeline(snap: McSnapshot, view: McView, nowMs: number): McTimelineProps & {
  legend: { label: string; color: string }[]
} {
  const tz = snap.tzOffsetMin
  const projects = projectColors(snap.sessions)
  const bar = (s: McSession): McBar => {
    const color = colorOf(s, snap, view.color, projects)
    return { id: s.id, color, muted: mute(color), segs: s.segments, tip: tip(s, tz),
      ...(s.scheduled ? { routine: true as const } : {}) }
  }
  const convos = visible(snap, view)
  const days = snap.range.days
  const multiDay = days.length > 1
  const byDay = multiDay && view.rows === 'day'
  const rows: McRow[] = []
  const minutes = (g: McSession[]) => workedMinutes(g)

  if (byDay) {
    // one row per day, the hours of the day across: when in the day you work
    for (const d of days) {
      const inDay = (s: McSession) => s.segments.some(([a, b]) => a < d.to && b > d.from)
      const clip = (s: McSession): McSession =>
        ({ ...s, segments: s.segments.filter(([a, b]) => a < d.to && b > d.from) })
      const work = convos.filter(inDay).map(clip)
      const mins = workedMinutes(work, d.from, d.to)
      rows.push({ kind: 'track', label: d.label, sub: mins ? duration(mins) : '', base: d.from, id: null,
        bars: work.map(bar) })
    }
  } else {
    const base = snap.range.from
    const keyOf = (s: McSession) => groupKey(s, view.rows)
    const groups = new Map<string, McSession[]>()
    for (const s of convos) groups.set(keyOf(s), [...(groups.get(keyOf(s)) ?? []), s])
    // most time first; the not-yet-summarized last; status keeps its own order
    const ordered = [...groups.entries()].sort((a, b) =>
      view.rows === 'status'
        ? STATUS_ORDER.indexOf(a[0]) - STATUS_ORDER.indexOf(b[0])
        : a[0] === UNFILED ? 1 : b[0] === UNFILED ? -1 : minutes(b[1]) - minutes(a[1]))
    const described = new Map(snap.workstreams.map(w => [w.name, w.description]))
    for (const [k, g] of ordered) {
      const note = view.rows === 'workstream' ? described.get(k) : null
      rows.push({ kind: 'header', label: k,
        sub: `${g.length} conversation${g.length > 1 ? 's' : ''} · ${duration(minutes(g))}`,
        // Client props are plain data: leave `note` out rather than undefined
        ...(note ? { note } : {}) })
      // the conversation you spent most time on first, in every grouping; ties by start
      for (const s of [...g].sort((a, b) => b.activeMin - a.activeMin || a.first - b.first)) {
        rows.push({ kind: 'track', label: s.title, sub: duration(s.activeMin), base, id: s.id, bars: [bar(s)] })
      }
    }
  }

  // x axis: the days across for a multi-day range, else the hours worth showing
  let hourFrom = 0
  let hourTo = 24
  const ticks: McTick[] = []
  const tracks = rows.filter((r): r is Extract<McRow, { kind: 'track' }> => r.kind === 'track')
  if (multiDay && !byDay) {
    hourTo = days.length * 24
    days.forEach((d, i) => ticks.push({ at: i * 24, label: d.label.split(' ').slice(0, 2).join(' ') }))
  } else {
    const hours: number[] = []
    const work = tracks.some(r => r.bars.some(b => !b.routine))
    for (const row of tracks) {
      for (const b of row.bars) {
        if (work && b.routine) continue
        for (const [x, y] of b.segs) hours.push((x - row.base) / HOUR, (y - row.base) / HOUR)
      }
    }
    const isToday = !multiDay && nowMs >= snap.range.from && nowMs < snap.range.to
    if (isToday) hours.push((nowMs - snap.range.from) / HOUR)
    if (hours.length) {
      hourFrom = Math.max(0, Math.floor(Math.min(...hours)) - 1)
      hourTo = Math.min(24, Math.ceil(Math.max(...hours)) + 1)
    } else {
      hourFrom = 8
      hourTo = 18
    }
    if (hourTo - hourFrom < 8) hourTo = Math.min(24, hourFrom + 8)
    if (hourTo - hourFrom < 8) hourFrom = Math.max(0, hourTo - 8)
    const step = hourTo - hourFrom > 14 ? 2 : 1
    for (let h = Math.ceil(hourFrom / step) * step; h < hourTo; h += step) ticks.push({ at: h, label: pad(h) })
  }

  const isToday = nowMs >= snap.range.from && nowMs < snap.range.to
  const nowBase = !isToday ? null : byDay
    ? (days.find(d => nowMs >= d.from && nowMs < d.to)?.from ?? null)
    : snap.range.from
  // keyboard order: the conversation rows top to bottom, as drawn
  const order = tracks.filter(r => r.id).map(r => r.id as string)
  return { rows, hourFrom, hourTo, ticks, now: isToday ? nowMs : null, nowBase, selected: null,
    order, collapsed: [], legend: legend(snap, view) }
}

// ---------------------------------------------------------------- timeline as a picture
// Text colours for a drawing that can't see the app's theme: tuned for the dark theme,
// still legible on a light one.
const INK = '#a5adb7'
const INK_DIM = '#7d8590'
const GRID = '#8b949e'

const esc = (t: string) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
/** Roughly how many characters of `size`-px text fit in `px` (proportional font). */
const fit = (text: string, px: number, size: number) =>
  truncate(text, Math.max(4, Math.floor(px / (size * 0.56))))

// ---------------------------------------------------------------- timeline as strips
/**
 * The timeline as one small vector strip per row, for the desktop: the pane lays the
 * rows out (a clickable name, then its strip), so names and bars always line up, and
 * each strip draws just that row's bars — or, over a multi-day range, one shaded
 * cell per day with the time worked in it. `axis` is the strip of hour/day labels.
 * Every strip is `width` px wide with the same horizontal scale.
 */
export function timelineStrips(t: McTimelineProps, width: number): { axis: string; rows: (string | null)[] } {
  const W = Math.max(200, Math.round(width))
  const span = Math.max(1, t.hourTo - t.hourFrom)
  const multiDay = span > 24
  const days = multiDay ? Math.round(span / 24) : 0
  const dayW = multiDay ? W / days : 0
  const xHour = (h: number) => ((h - t.hourFrom) / span) * W
  const xAt = (ms: number, base: number) => xHour((ms - base) / HOUR)
  const svg = (h: number, body: string) =>
    `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${h}" viewBox="0 0 ${W} ${h}" ` +
    `font-family="-apple-system, BlinkMacSystemFont, Helvetica, sans-serif">${body}</svg>`
  const grid = (h: number) => t.ticks.map(tick => {
    const x = xHour(tick.at).toFixed(1)
    return `<line x1="${x}" y1="0" x2="${x}" y2="${h}" stroke="${GRID}" stroke-opacity="0.18"/>`
  }).join('') + (multiDay ? t.ticks.map((tick, i) => i % 2 ? '' :
    `<rect x="${xHour(tick.at).toFixed(1)}" y="0" width="${dayW.toFixed(1)}" height="${h}" fill="${GRID}" fill-opacity="0.06"/>`).join('') : '')

  // a long range has narrow days: label every few so the labels don't run together
  const labelEvery = multiDay ? Math.max(1, Math.ceil(48 / dayW)) : 1
  const axis = svg(18, t.ticks.map((tick, i) => {
    if (i % labelEvery) return ''
    const x = multiDay ? xHour(tick.at) + dayW / 2 : xHour(tick.at)
    return `<text x="${x.toFixed(1)}" y="13" fill="${INK_DIM}" font-size="11" text-anchor="middle">${esc(tick.label)}</text>`
  }).join('') + (t.now !== null && t.nowBase !== null && !multiDay
    ? `<text x="${(xAt(t.now, t.nowBase) + 3).toFixed(1)}" y="13" fill="#f85149" font-size="10">now</text>` : ''))

  const H = 20
  type Track = Extract<McRow, { kind: 'track' }>
  const strip = (row: Track) => {
    const parts: string[] = [grid(H)]
    if (multiDay) {
      for (let d = 0; d < days; d++) {
        const from = row.base + (t.hourFrom + d * 24) * HOUR
        const to = from + 24 * HOUR
        const mins = Math.round(row.bars.reduce((n, bar) => n + bar.segs.reduce((m, [a, b]) =>
          m + Math.max(0, Math.min(b, to) - Math.max(a, from)) / 60_000, 0), 0))
        if (!mins) continue
        const bar = row.bars.find(b => b.segs.some(([a, z]) => a < to && z > from)) ?? row.bars[0]!
        const strength = 0.5 + 0.5 * Math.min(1, mins / 120)
        const faded = t.selected !== null && !row.bars.some(b => b.id === t.selected)
        const cx = d * dayW
        parts.push(`<rect x="${(cx + 3).toFixed(1)}" y="2" width="${(dayW - 6).toFixed(1)}" height="16" rx="4" ` +
          `fill="${bar.color}" fill-opacity="${(faded ? strength * 0.35 : strength).toFixed(2)}"/>`)
        if (dayW >= 44) {
          parts.push(`<text x="${(cx + dayW / 2).toFixed(1)}" y="14" font-size="10" text-anchor="middle" ` +
            `fill="${strength > 0.8 && !faded ? '#0d1117' : '#f0f6fc'}">${duration(mins)}</text>`)
        }
      }
    } else {
      for (const bar of row.bars) {
        const opacity = t.selected === null ? 0.9 : bar.id === t.selected ? 1 : 0.3
        const [by, bh] = bar.routine && !row.id ? [14, 3] : [4, 12]
        for (const [a, b] of bar.segs) {
          const xa = Math.max(0, xAt(a, row.base))
          const xb = Math.min(W, xAt(b, row.base))
          if (xb <= 0 || xa >= W) continue
          parts.push(`<rect x="${xa.toFixed(1)}" y="${by}" width="${Math.max(3, xb - xa).toFixed(1)}" height="${bh}" ` +
            `rx="${bh > 4 ? 3 : 1}" fill="${bar.color}" fill-opacity="${opacity}"/>`)
        }
      }
      if (t.now !== null && row.base === t.nowBase) {
        const x = xAt(t.now, row.base)
        if (x >= 0 && x <= W) parts.push(`<line x1="${x.toFixed(1)}" y1="0" x2="${x.toFixed(1)}" y2="${H}" stroke="#f85149" stroke-width="1.5"/>`)
      }
    }
    return svg(H, parts.join(''))
  }
  // a heading's strip is its whole group's bars in one row: what a folded group shows
  const rows = t.rows.map((row, i) => {
    if (row.kind === 'track') return strip(row)
    const group: Track[] = []
    for (const r of t.rows.slice(i + 1)) {
      if (r.kind !== 'track') break
      group.push(r)
    }
    if (!group.length) return null
    return strip({ kind: 'track', label: row.label, sub: '', base: group[0]!.base, id: null,
      bars: group.flatMap(r => r.bars) })
  })
  return { axis, rows }
}

/** The rows choices that make sense for a range: "day" only for multi-day ranges. */
export function rowsOptions(multiDay: boolean): { value: McRowsMode; label: string }[] {
  const base: { value: McRowsMode; label: string }[] = [
    { value: 'workstream', label: 'Group: workstream' },
    { value: 'project', label: 'Group: project' },
    { value: 'status', label: 'Group: status' },
    { value: 'kind', label: 'Group: kind of work' },
    { value: 'category', label: 'Group: work / personal' },
  ]
  return multiDay ? [{ value: 'day', label: 'One row per day' }, ...base] : base
}

