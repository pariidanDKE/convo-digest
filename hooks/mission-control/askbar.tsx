// The Ask bar on the desktop: a Client surface module, because the desktop's own Input
// draws at a fixed narrow width with no way to widen it. This one fills the row: a
// filled field with a caret, the placeholder while empty, and an Ask pill at the end.
//
// A click gives it the keyboard (Escape hands it back). Cmd/Ctrl+A selects the text, so
// typing replaces it and Backspace clears it; Cmd/Ctrl+C and X copy and cut it through
// the hooks module, which has the clipboard. The text lives in local state;
// Enter, or a click on the pill, posts the question to the hooks module, which asks.
// When the hooks module asks something else (a suggestion was clicked) the field takes
// that question over, so what's in the field is always what was last asked.
import type { ClientKeyEvent, ClientModule, ClientPointerEvent, RenderElement } from 'claude-code'

import type { McAskBarProps } from '../../types'

type Local = { text: string; cursor: number; focused: boolean; seen: string; pillHover: boolean; all: boolean }

const FILL = '#262626'
const FILL_FOCUS = '#2d2d2d'
const ACCENT = '#a371f7'
const SELECTED = '#4c3a75'
const PILL = '#3a3a3a'
const PILL_HOVER = '#4a4a4a'
const PILL_W = 9                       // " Ask  ↵ " and a cell of air
const ICON_W = 3

/** Key names that are keys, not text; anything else that arrives is typed (or pasted). */
const SPECIAL = new Set(['up', 'down', 'left', 'right', 'return', 'enter', 'tab', 'backspace', 'delete',
  'pageup', 'pagedown', 'home', 'end', 'escape', 'insert'])

const AskBar: ClientModule<McAskBarProps, Local> = (props, surface) => {
  const { Box, Text } = surface.elements
  const columns = surface.columns
  if (!columns) return <Text dimColor>…</Text>

  const prev = surface.state
  // a question asked from outside the field (a suggestion) replaces what's typed
  const st: Local = !prev
    ? { text: props.question, cursor: props.question.length, focused: false, seen: props.question, pillHover: false,
        all: false }
    : prev.seen !== props.question
      ? { ...prev, text: props.question, cursor: props.question.length, seen: props.question, all: false }
      : prev

  const set = (patch: Partial<Local>) => surface.setState({ ...st, ...patch })
  const submit = () => {
    const q = st.text.trim()
    if (q && !props.busy) surface.post({ type: 'ask', question: q })
  }
  const pillFrom = columns - 1 - PILL_W

  surface.onPointer((ev: ClientPointerEvent) => {
    const onPill = ev.x >= pillFrom && ev.x < columns - 1
    if (ev.type === 'down' && ev.button === 'left') {
      if (onPill) submit()
      if (!st.focused || st.all) set({ focused: true, all: false })
    } else if (ev.type === 'move' || ev.type === 'enter') {
      if (onPill !== st.pillHover) set({ pillHover: onPill })
    } else if (ev.type === 'leave' && st.pillHover) {
      set({ pillHover: false })
    }
  })

  surface.onKey((ev: ClientKeyEvent) => {
    const { text, cursor } = st
    const k = ev.key
    const mod = ev.meta || ev.ctrl
    if (k === 'return' || k === 'enter') return submit()
    if (mod && k === 'a') return set({ all: text.length > 0, cursor: text.length, focused: true })
    if (mod && (k === 'c' || k === 'x')) {
      if (!st.all || !text) return
      surface.post({ type: 'copy', text })
      if (k === 'x') set({ text: '', cursor: 0, all: false })
      return
    }
    if (st.all) {
      // the whole text is selected: an edit replaces it, a move drops the selection
      if (k === 'backspace' || k === 'delete') return set({ text: '', cursor: 0, all: false })
      if (k === 'left' || k === 'home' || k === 'up') return set({ cursor: 0, all: false })
      if (k === 'right' || k === 'end' || k === 'down') return set({ cursor: text.length, all: false })
      if (SPECIAL.has(k) || /^f\d{1,2}$/.test(k) || mod) return set({ all: false })
      const typed = (k === 'space' ? ' ' : k).replace(/[\r\n\t]+/g, ' ')
      return set({ text: typed, cursor: typed.length, all: false })
    }
    if (k === 'backspace') {
      if (ev.meta || (ev.ctrl && !ev.shift)) return set({ text: text.slice(cursor), cursor: 0 })
      if (cursor > 0) set({ text: text.slice(0, cursor - 1) + text.slice(cursor), cursor: cursor - 1 })
      return
    }
    if (k === 'delete') {
      if (cursor < text.length) set({ text: text.slice(0, cursor) + text.slice(cursor + 1) })
      return
    }
    if (k === 'left') return set({ cursor: ev.meta ? 0 : Math.max(0, cursor - 1) })
    if (k === 'right') return set({ cursor: ev.meta ? text.length : Math.min(text.length, cursor + 1) })
    if (k === 'home') return set({ cursor: 0 })
    if (k === 'end' || (ev.ctrl && k === 'e')) return set({ cursor: text.length })
    if (ev.ctrl && k === 'u') return set({ text: text.slice(cursor), cursor: 0 })
    if (SPECIAL.has(k) || /^f\d{1,2}$/.test(k) || ev.ctrl || ev.meta) return
    const typed = (k === 'space' ? ' ' : k).replace(/[\r\n\t]+/g, ' ')
    set({ text: text.slice(0, cursor) + typed + text.slice(cursor), cursor: cursor + typed.length, focused: true })
  })

  // what fits between the icon and the pill; scrolled so the caret stays in view
  const room = Math.max(4, columns - 2 - ICON_W - PILL_W)
  const start = Math.max(0, st.cursor - (room - 2))
  const shown = st.text.slice(start, start + room - 1)
  const at = st.cursor - start

  let field: RenderElement[]
  if (!st.text) {
    field = [
      ...(st.focused ? [<Text color={ACCENT}>▏</Text>] : []),
      <Text dimColor wrap="truncate-end">{props.placeholder}</Text>,
    ]
  } else if (st.all && !props.busy) {
    field = [<Text backgroundColor={SELECTED}>{shown}</Text>]
  } else if (st.focused && !props.busy) {
    field = [<Text>{shown.slice(0, at)}</Text>, <Text color={ACCENT}>▏</Text>, <Text>{shown.slice(at)}</Text>]
  } else {
    field = [<Text wrap="truncate-end">{shown}</Text>]
  }

  return (
    <Box flexDirection="row" width={columns} height={3} alignItems="center"
      backgroundColor={st.focused ? FILL_FOCUS : FILL}>
      <Box width={1} height={3} />
      <Box width={ICON_W} height={1}><Text color={ACCENT}>✦</Text></Box>
      <Box width={room} height={1} flexDirection="row" overflow="hidden">{field}</Box>
      <Box width={PILL_W} height={1} justifyContent="center"
        backgroundColor={props.busy ? FILL : st.pillHover ? PILL_HOVER : PILL}>
        {props.busy ? <Text dimColor>thinking…</Text> : <Text bold>Ask ↵</Text>}
      </Box>
      <Box width={1} height={3} />
    </Box>
  )
}

export default AskBar
