/** One recipient's receipt: queued, delivered, seen, failed, expired or posted (a channel post). */
export type Receipt = { to: string; r: string; tick: string; why: string }

/** One message as `bus.py snapshot` prints it. */
export type BusMessage = {
  id: string
  from: string
  time: string
  text: string
  body: boolean
  prio: string
  topic: string | null
  chan: string | null
  thread: string | null
  reply_to: string | null
  to: string[]
  cc: string[]
  reactions: Record<string, string[]>
  pinned: boolean
  edited: boolean
  deleted: boolean
  receipts: Receipt[]
  /** The per-recipient id of the copy addressed to `me`, when there is one (what `read` marks seen). */
  rid: string | null
  unread: boolean
  mention: boolean
  reply_to_me: boolean
}

export type BusRole = { name: string; kind: string | null; mode: string | null; unread: number; watch: boolean }

export type BusHealth = {
  deliver: boolean
  deliver_age: number | null
  watch: boolean
  last_write_age: number | null
}

export type BusSnapshot = {
  bus: boolean
  dir?: string
  me?: string
  cursor?: string
  unchanged?: boolean
  health?: BusHealth
  messages?: BusMessage[]
  roles?: BusRole[]
  channels?: string[]
  unread?: number
  mentions?: number
}

/** normal: keys move and act; react: the emoji row is up. Typing goes to the composer Input. */
export type BusMode = 'normal' | 'react'

/** What an expanded message shows below its row: the body, once fetched. */
export type BusDetail = { id: string; body: string | null; loading: boolean }

declare module 'claude-code' {
  interface PluginState {
    'agent-bus': {
      snap: BusSnapshot | null
      error: string | null
      selected: string | null
      expanded: BusDetail | null
      mode: BusMode
      draft: string
      replyTo: string | null
      notice: string | null
    }
  }
}
