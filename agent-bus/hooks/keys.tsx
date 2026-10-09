import type { ClientKeyEvent, ClientModule, JsonValue } from 'claude-code'

/** What the pane hands the key region: the mode shown and the accent it is drawn in. */
type KeysProps = { mode: string; hint: string; accent: string }

type Sent = { i: number; k: string; ctrl?: true; shift?: true }

/**
 * The pane's raw-key region: once it has the focus (a click on it), every key the person presses is
 * posted to the hooks module, which owns all behaviour. Posts carry a running count and the last keys,
 * so a key pressed in the same frame as another is not lost when one post replaces an undelivered one.
 */
const Keys: ClientModule<JsonValue, { ready: true }> = (props, surface) => {
  const { Box, Text } = surface.elements
  const p = (props ?? {}) as Partial<KeysProps>
  if (surface.state === undefined) {
    let n = 0
    let recent: Sent[] = []
    surface.onKey((ev: ClientKeyEvent) => {
      n += 1
      const s: Sent = { i: n, k: ev.key }
      if (ev.ctrl) s.ctrl = true
      if (ev.shift) s.shift = true
      recent = [...recent.slice(-15), s]
      surface.post({ n, keys: recent } as unknown as JsonValue)
    })
    surface.setState({ ready: true })
  }
  return (
    <Box gap={1}>
      <Text inverse bold color={p.accent ?? 'claude'}>{` ${(p.mode ?? 'normal').toUpperCase()} `}</Text>
      <Text dimColor wrap="truncate-end">{p.hint ?? 'click here for vim keys'}</Text>
    </Box>
  )
}

export default Keys
