---
name: coordinated-worker
description: Work as one lane of a coordinated multi-agent project. Use when you were started with a role and a brief by a coordinator, when the session context says you are a worker, or when told to resume a lane from its card.
---

# Working as a lane

`coord` is the coordinator CLI and `bus` the agent-bus CLI, both named in your session context. Your role is in your launch line and in `AGENT_BUS_ROLE`.

## Start (less than 25k tokens of reading)
1. The rules core, which the session context names.
2. Your brief.
3. Your lane card. If it is missing, write it from the template in the cards folder before anything else.
4. With the bus: `bus digest --as <role>`. Mail sent to your role before you started waits there.
5. Only the sections your card or brief points to, by range.

## While working
- **Lane card:** keep it current at each milestone. It is the only thing your successor reads.
- **Heavy jobs** (builds, full test suites, packaging, GPU work):
  1. Ask `scheduler` for a GO.
  2. Run `coord preflight` first.
  3. Release the slot when done.
- **Questions only the user can answer:** `coord ask "<question>" --options "a|b"`. Then keep working on everything that does not depend on the answer.
- **To the coordinator:** only a decision you need, a blocker, or a FINAL (3 lines at most plus a path). No acks.
- **Other lanes:** talk to them directly on the bus.
- **The shared tree:** never revert, reset, stash or reformat files you did not create. Undo your own changes by editing your hunks back.
- **When idle:** arm the bus watch Monitor if the context asks. Do not poll.

## Retiring
When the plugin says you are past the token threshold, or the coordinator sends `retire`:
1. Finish or park the current step.
2. Update your card: state, next 3 actions, anchors (file:line), open messages, gotchas.
3. Release any scheduler slot you hold.
4. Send the coordinator a FINAL.
5. End with `SAFE TO CLOSE`, or with `KEEP OPEN: <reason>` if something must keep running.
