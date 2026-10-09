export type Priority = 'urgent' | 'high' | 'med' | 'low'

export type Card = {
  key: string
  title: string
  priority: Priority
  assignee: string | null
  area: string | null
  proposed: string | null
}

export type TaskEvent = {
  id: number
  ts: string
  actor: string
  key: string | null
  title: string | null
  action: string
  detail: Record<string, unknown>
}

export type Conflict = {
  a: string
  a_title: string
  a_assignee: string | null
  a_glob: string
  b: string
  b_title: string
  b_assignee: string | null
  b_glob: string
}

export type Snapshot = {
  me: { name: string; level: string; via: string }
  prefix: string
  stats: {
    by_status: Record<string, number>
    done_today: number
    conflicts: number
    proposed: number
    last_event: number
    areas: Record<string, Record<string, number>>
    labels: Record<string, Record<string, number>>
  }
  counts: Record<string, number>
  board: Record<string, Card[]>
  events: TaskEvent[]
  conflicts: Conflict[]
  areas: string[]
  labels: string[]
  review_for_me: string[]
  unread_notes: number
}

export type Detail = {
  key: string
  title: string
  body: string
  status: string
  priority: string
  assignee: string | null
  proposed: string | null
  reviewer: string | null
  creator: string
  area: string | null
  parent: string | null
  created: string
  updated: string
  labels: string[]
  links: { kind: string; value: string }[]
  touches: string[]
  blocked_by: string[]
  blocks: string[]
  children: { key: string; title: string; status: string; assignee: string | null }[]
  comments: { author: string; ts: string; body: string }[]
  history: { ts: string; actor: string; action: string; detail: Record<string, unknown> }[]
}

/** What `task.py where --json` reports: the resolved configuration. */
export type Where = {
  root: string
  db: string
  db_exists: boolean
  roles: string
  roles_exists: boolean
  prefix: string
  labels: string[]
}

export type Filters = { area: string; priority: string; label: string }

export type View = 'board' | 'list' | 'progress' | 'conflicts' | 'detail'

declare module 'claude-code' {
  interface PluginState {
    'task-tracker': {
      where: Where | null
      snap: Snapshot | null
      error: string | null
      filters: Filters
      view: View
      back: View
      detail: Detail | null
      page: number
    }
  }
}
