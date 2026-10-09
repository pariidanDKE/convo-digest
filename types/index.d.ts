// Mission Control's state contract: what src/mission_control.py hands the pane,
// and the values the pane keeps in `$.state` for the session.

export type McRangeKey = 'today' | 'yesterday' | '7d' | 'custom'
/** How the timeline groups conversations: by workstream (the digest's epic for each),
 *  project, status, kind of work, work/personal — or one row per day. */
export type McRowsMode = 'workstream' | 'project' | 'status' | 'kind' | 'category' | 'day'
/** What sort of work a conversation was, as the digest's summarizer judged it. */
export type McKind = 'build' | 'fix' | 'review' | 'investigate' | 'plan' | 'admin'
/** One of the digest's workstreams (~/.claude/digest/workstreams.json). */
export type McWorkstream = { name: string; description: string | null; count: number }
export type McColorMode = 'status' | 'project'
export type McSegment = [number, number]

export type McSession = {
  id: string
  app: string | null
  apps: (string | null)[]
  title: string
  appTitle: string | null
  project: string
  root: string | null
  cwd: string | null
  category: string
  status: string
  changedSinceDigest: boolean
  digested: boolean
  gist: string | null
  open: string | null
  topics: string[]
  tickets: string[]
  branch: string | null
  segments: McSegment[]
  first: number
  last: number
  activeMin: number
  scheduled: string | null
  archived: boolean
  needsAction: string | null
  /** the digest's epic for it, and what sort of work it was; null until summarized */
  workstream: string | null
  kind: McKind | null
}

export type McRun = {
  start: number | null
  end: number | null
  status: string
  detail: string | null
  app: string | null
  cli: string | null
  title: string | null
}

export type McRoutine = {
  task: string
  name: string
  description: string | null
  oneTime: boolean
  runs: McRun[]
}

export type McDay = { date: string; label: string; from: number; to: number }

export type McLedgerRun = {
  start: number | null
  end?: number | null
  trigger: string | null
  status: string | null
  indexed?: number | null
  renamed?: number | null
  note?: string | null
}

export type McIssue = { ts: number | null; kind: string | null; detail: string | null; severity: string }

/** The latest standup brief, read from the standup-brief task's last run. */
export type McStandup = {
  app: string | null
  cli: string | null
  at: number | null
  heading: string
  script: string
  table: string | null
  preamble: string | null
}

export type McSnapshot = {
  generatedAt: number
  tookMs: number
  tzOffsetMin: number
  range: { key: string; label: string; from: number; to: number; days: McDay[] }
  sessions: McSession[]
  totals: { conversations: number; activeMin: number; open: number; new: number; projects: number }
  routines: McRoutine[]
  digest: { lastRun: McLedgerRun | null; lastOk: number | null; issues: McIssue[]; issuesSinceOk: number }
  standup: McStandup | null
  /** projects a conversation can be moved to */
  projects: string[]
  /** the digest's workstreams, most recently seen first */
  workstreams: McWorkstream[]
}

/** One turn of a conversation in the reader: the person's message or Claude's reply. */
export type McTurn = { role: 'user' | 'assistant' | 'note'; ts: number | null; text: string; tools: string[] }

/** A conversation as the reader shows it (collect.py transcript). */
export type McReader = {
  id: string
  mtime?: number
  total?: number
  hidden?: number
  turns?: McTurn[]
  error?: string
  /** how many turns were asked for: "Show earlier" raises it */
  last: number
  /** whether the messages are unfolded; closed by default, the gist is enough to start */
  expanded: boolean
  loading: boolean
}

/** What the desktop Ask bar (hooks/askbar.tsx) is drawn from. */
export type McAskBarProps = { placeholder: string; question: string; busy: boolean }

export type McView = {
  range: McRangeKey
  rows: McRowsMode
  color: McColorMode
  /** a custom range's first and last day, local YYYY-MM-DD, both included */
  from?: string
  to?: string
  /** show only this workstream / this kind of work; absent shows all */
  ws?: string
  kind?: McKind
}

export type McRef = { id: string; app: string | null; title: string }

export type McAsk = {
  question: string
  busy: boolean
  answer: string | null
  refs: Record<string, McRef>
  error: string | null
}

/** What the app's scheduled-tasks server says about a task, when the mod can reach it. */
export type McTask = {
  taskId: string
  enabled: boolean
  schedule: string | null
  nextRunAt: string | null
}

export type McStatus = {
  loading: boolean
  error: string | null
  tasks: McTask[] | null
}

/** One bar's worth of a session on a timeline row: its active stretches. */
export type McBar = { id: string; color: string; muted: string; segs: McSegment[]; tip: string; routine?: true }

/**
 * One row of the timeline: a group heading, or a track (a conversation, a routine,
 * or a whole day) whose bars are placed against `base`, the local midnight its
 * hours count from. `id` is set when the row IS one conversation, so its label
 * selects it too.
 */
export type McRow =
  | { kind: 'header'; label: string; sub: string; note?: string }
  | { kind: 'track'; label: string; sub: string; base: number; id: string | null; bars: McBar[] }

export type McTick = { at: number; label: string }

export type McTimelineProps = {
  rows: McRow[]
  hourFrom: number
  hourTo: number
  ticks: McTick[]
  now: number | null
  nowBase: number | null
  selected: string | null
  order: string[]
  /** group headings whose conversations are folded away */
  collapsed: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'convo-digest': {
      snap: McSnapshot | null
      view: McView
      selected: string | null
      ask: McAsk
      status: McStatus
      /** whether the one-line "Mission Control" band shows above the prompt */
      band: boolean
      /** whether the standup brief is unfolded in full */
      standupOpen: boolean
      /** when Summarize now handed the digest a turn, until that turn completes */
      summarizing: number | null
      /** timeline groups folded away (by heading); empty, the default, is all unfolded */
      collapsed: string[]
      /** the app session an Archive or Pin click handed a turn for, until that turn completes */
      acting: string | null
      /** the conversation open in the reader, and what was read of it */
      reader: McReader | null
      /** the conversation a Summarize click handed a digest turn for, until that turn ends */
      resummarizing: string | null
    }
  }
}
