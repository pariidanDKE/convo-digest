// The Mission Control timeline: a Client surface module, so hover and click work on
// both the desktop and the terminal.
//
// Everything is laid out in whole cells as a row of fixed-width boxes — never by
// padding text with spaces, since the desktop draws text in a proportional font, and
// never with absolute offsets, which the desktop resolves against the whole region
// rather than the row. A label column of fixed width, then a track: each run of cells
// is one box, a solid coloured bar where a session was active, a 1-cell hour guide
// or "now" mark, or blank space.
// Clicks and keys go back to the hooks module through `post`; the selection lives in
// the plugin's state and arrives here as props, only the hover is local.
import type { ClientModule, ClientPointerEvent, RenderElement } from 'claude-code'

import type { McBar, McRow, McTimelineProps } from '../../types'

type Local = { hover: string | null }
type Shown = McRow | { kind: 'spacer' }

const HOUR = 3_600_000
const NOW = '#f85149'
const SUB_W = 6                     // the active-time column at the end of a label

const Timeline: ClientModule<McTimelineProps, Local> = (props, surface) => {
  const { Box, Text } = surface.elements
  const columns = surface.columns
  if (!columns) return <Text dimColor>…</Text>

  // the title matters more than the hours: it gets most of the row
  const labelW = Math.max(24, Math.min(90, Math.floor(columns * 0.55)))
  const trackW = Math.max(12, columns - labelW - 1)
  const span = Math.max(1, props.hourTo - props.hourFrom)
  const perHour = trackW / span
  const cellAt = (ms: number, base: number) => ((ms - base) / HOUR - props.hourFrom) * perHour
  const hover = surface.state?.hover ?? null

  // a blank line before every heading but the first: the rows as drawn, axis at y 0
  const shown: Shown[] = []
  let folded = false
  props.rows.forEach((row, i) => {
    if (row.kind === 'header') {
      if (i > 0) shown.push({ kind: 'spacer' })
      folded = props.collapsed.includes(row.label)
      shown.push(row)
    } else if (!folded) {
      shown.push(row)
    }
  })

  const spans = (row: Extract<McRow, { kind: 'track' }>) =>
    row.bars.flatMap((b: McBar) => b.segs.map(([a, z]) => {
      const c0 = Math.max(0, Math.floor(cellAt(a, row.base)))
      const c1 = Math.min(trackW, Math.max(c0 + 1, Math.ceil(cellAt(z, row.base))))
      return { bar: b, c0, c1 }
    })).filter(s => s.c0 < trackW && s.c1 > 0)

  const hit = (x: number, y: number): string | null => {
    const row = shown[y - 1]
    if (!row || row.kind !== 'track') return null
    if (x < labelW) return row.id
    const col = x - labelW - 1
    const found = spans(row).find(s => col >= s.c0 - 1 && col < s.c1 + 1)
    return found ? found.bar.id : row.id
  }

  surface.onPointer((ev: ClientPointerEvent) => {
    if (ev.type === 'leave') {
      if (hover !== null) surface.setState({ hover: null })
      return
    }
    const id = hit(ev.x, ev.y)
    if (ev.type === 'down' && ev.button === 'left') {
      const row = shown[ev.y - 1]
      if (row?.kind === 'header') surface.post({ type: 'toggle', group: row.label })
      else surface.post({ type: 'select', id })
      return
    }
    if ((ev.type === 'move' || ev.type === 'enter') && id !== hover) surface.setState({ hover: id })
  })

  surface.onKey(ev => {
    const order = props.order
    const at = props.selected ? order.indexOf(props.selected) : -1
    if (ev.key === 'down' || ev.key === 'right') {
      surface.post({ type: 'select', id: order[Math.min(order.length - 1, at + 1)] ?? null })
    } else if (ev.key === 'up' || ev.key === 'left') {
      surface.post({ type: 'select', id: order[Math.max(0, at - 1)] ?? null })
    } else if ((ev.key === 'return' || ev.key === 'o') && props.selected) {
      surface.post({ type: 'open', id: props.selected })
    }
  })

  const tickCells = props.ticks
    .map(t => ({ ...t, c: Math.round((t.at - props.hourFrom) * perHour) }))
    .filter(t => t.c >= 0 && t.c < trackW)
  const nowCell = (base: number) =>
    props.now !== null && props.nowBase === base ? Math.floor(cellAt(props.now, base)) : -1
  const axisNow = props.nowBase !== null ? nowCell(props.nowBase) : -1

  // the axis: tick labels and the "now" mark, each a box starting at its cell
  const marks = [
    ...tickCells.map(t => ({ c: t.c, text: t.label, isNow: false })),
    ...(axisNow >= 0 ? [{ c: axisNow, text: '▼', isNow: true }] : []),
  ].sort((a, b) => a.c - b.c)
  const axisParts: RenderElement[] = []
  let pos = 0
  marks.forEach((m, i) => {
    if (m.c < pos) return                                  // no room left for this one
    if (m.c > pos) axisParts.push(<Box width={m.c - pos} height={1} />)
    const next = marks.slice(i + 1).find(n => n.c > m.c)?.c ?? trackW
    const w = Math.max(1, Math.min(next - m.c, m.isNow ? 1 : m.text.length + 1))
    axisParts.push(
      <Box width={w} height={1} overflow="hidden">
        {m.isNow ? <Text color={NOW}>{m.text}</Text> : <Text dimColor wrap="truncate-end">{m.text}</Text>}
      </Box>,
    )
    pos = m.c + w
  })
  const axis = (
    <Box flexDirection="row" height={1}>
      <Box width={labelW + 1} height={1} />
      <Box width={trackW} height={1} flexDirection="row">{axisParts}</Box>
    </Box>
  )

  /** One track as consecutive fixed-width boxes. */
  const track = (row: Extract<McRow, { kind: 'track' }>): RenderElement[] => {
    const owner: (McBar | null)[] = new Array(trackW).fill(null)
    for (const s of spans(row)) for (let c = s.c0; c < s.c1; c++) owner[c] = s.bar
    const ticks = new Set(tickCells.map(t => t.c))
    const now = nowCell(row.base)
    const parts: RenderElement[] = []
    let c = 0
    while (c < trackW) {
      const bar = owner[c]
      if (bar) {
        let e = c + 1
        while (e < trackW && owner[e] === bar) e++
        const lit = props.selected === null || bar.id === props.selected || bar.id === hover
        parts.push(<Box width={e - c} height={1} backgroundColor={lit ? bar.color : bar.muted} />)
        c = e
      } else if (c === now) {
        parts.push(<Box width={1} height={1}><Text color={NOW}>│</Text></Box>)
        c += 1
      } else if (ticks.has(c)) {
        parts.push(<Box width={1} height={1}><Text dimColor>┆</Text></Box>)
        c += 1
      } else {
        let e = c + 1
        while (e < trackW && !owner[e] && e !== now && !ticks.has(e)) e++
        parts.push(<Box width={e - c} height={1} />)
        c = e
      }
    }
    return parts
  }

  const lines: RenderElement[] = shown.map(row => {
    if (row.kind === 'spacer') return <Box height={1} />
    if (row.kind === 'header') {
      return (
        <Box flexDirection="row" height={1} overflow="hidden">
          <Text bold>{`${props.collapsed.includes(row.label) ? '▸' : '▾'} ${row.label}`}</Text>
          <Text dimColor wrap="truncate-end">
            {`${row.sub ? `  ${row.sub}` : ''}${row.note ? `  —  ${row.note}` : ''}`}
          </Text>
        </Box>
      )
    }
    const isSel = row.id !== null && row.id === props.selected
    const isHover = row.id !== null && row.id === hover
    return (
      <Box flexDirection="row" height={1}>
        <Box width={labelW} height={1} flexDirection="row" overflow="hidden">
          <Box width={2} height={1}>
            {isSel
              ? <Text bold>▸</Text>
              : row.id && row.bars[0] ? <Text color={row.bars[0].color}>●</Text> : <Text> </Text>}
          </Box>
          <Box width={labelW - 2 - SUB_W} height={1} overflow="hidden">
            <Text wrap="truncate-end" bold={isSel} underline={isHover}
              dimColor={!isSel && !isHover && props.selected !== null}>
              {row.label}
            </Text>
          </Box>
          <Box width={SUB_W} height={1} justifyContent="flex-end">
            <Text dimColor>{row.sub}</Text>
          </Box>
        </Box>
        <Box width={1} height={1} />
        <Box width={trackW} height={1} flexDirection="row">{track(row)}</Box>
      </Box>
    )
  })

  const focus = props.rows
    .flatMap(r => (r.kind === 'track' ? r.bars : []))
    .find(b => b.id === (hover ?? props.selected))
  return (
    <Box flexDirection="column">
      {axis}
      {lines.length ? lines : [<Text dimColor>Nothing in this range.</Text>]}
      <Box height={1} />
      <Text dimColor={!focus} wrap="truncate-end">
        {focus ? focus.tip : 'Hover a bar for details · click a bar or a name to select it · ↑/↓ step · Enter opens'}
      </Text>
    </Box>
  )
}

export default Timeline
