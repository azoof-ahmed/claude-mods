"""orca-bus: one message bus for every terminal tab, whatever runs in it (Claude Code, Codex, a shell).

  orca-bus register <name> [--kind claude|codex|shell] [--handle H] [--session S] [--note N] [--owner NAME]
                    [--mode typed|inbox|file]
  orca-bus send <to[,to2]> "<text>" [--cc a,b] [--prio urgent|normal|fyi] [--topic T] [--body-file F]
                [--from NAME] [--re ID] [--now]
  orca-bus watch [--as NAME] [--fyi]        run under a Monitor: one line per new message (unread backlog first)
  orca-bus digest [--as NAME] [--mark]      unread, grouped by prio and topic, one line each
  orca-bus read <id>... [--as NAME]         full text + body; marks read
  orca-bus inbox [--unread] [--as NAME] [--json] [--follow]
  orca-bus ack <id> [--as NAME]
  orca-bus who [--json]
  orca-bus status [ID]                      with an ID: per-recipient receipts (. queued, v delivered, vv seen,
                                            ! failed, x expired) and reactions; no turn spent by anyone
  orca-bus react <id> <emoji> [--off]       a reaction: never notifies, never starts a turn (UI + digest only)
  orca-bus sub|unsub <id>                   follow a thread: its replies reach your inbox
  orca-bus thread <id>                      the whole thread, oldest first
  orca-bus pin|unpin <id>   edit <id> "<text>"   delete <id>   (edit/delete: your own, within 5 minutes)
  orca-bus deliver [--once] [--interval S] [--alert-after S] [--draft-grace S] [--max-attempts N]

Who is notified (a group chat without spam): `to`, `cc`, every registered @name in the text, the author of the
message you reply to (--re), and the thread's subscribers. Nobody else; they can browse the stream. `to` may be a
channel ('#train', '#all') for a post that notifies nobody by itself. @all is for admins (user, coordinator,
scheduler).

Modes: typed (the daemon types every message in), file (v2: only prio=urgent is typed, when the tab is idle; the
tab runs `watch` under a Monitor and reads `digest`), inbox (nothing typed; legacy). fyi is never typed in any mode.
Messages wait for the NAME, so a successor tab registering the same name reads the backlog.

Never type into another agent's tab with a raw `orca terminal send`: it bypasses the per-tab typing lock, and two
writers in one input box concatenate into one prompt. Use `send` (the daemon types it) or `send --now`.

The bus directory is --dir, else $ORCA_BUS_DIR, else the nearest `.orca-bus/` above the current directory.
Your own name is --from/--as, else $ORCA_BUS_NAME, else the registry entry for this terminal's
$ORCA_TERMINAL_HANDLE.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from .store import Bus, BusError


def find_root(arg):
    if arg:
        return Path(arg)
    if os.environ.get('ORCA_BUS_DIR'):
        return Path(os.environ['ORCA_BUS_DIR'])
    here = Path.cwd().resolve()
    for d in [here, *here.parents]:
        if (d / '.orca-bus').is_dir():
            return d / '.orca-bus'
    raise BusError('no bus directory: pass --dir, set ORCA_BUS_DIR, or create .orca-bus/ in the project root')


def whoami(bus, explicit):
    if explicit:
        return explicit
    if os.environ.get('ORCA_BUS_NAME'):
        return os.environ['ORCA_BUS_NAME']
    name = bus.name_for_handle(os.environ.get('ORCA_TERMINAL_HANDLE'))
    if name:
        return name
    h = os.environ.get('ORCA_TERMINAL_HANDLE')
    raise BusError('who are you? register this tab first (`register <name>`), or pass --from/--as'
                   + (f' (this terminal is {h})' if h else ''))


def live_terminals():
    try:
        from .orca import Orca
        return Orca().terminals()
    except Exception as ex:
        print(f'(orca not reachable: {ex})', file=sys.stderr)
        return None


def line_v2(m, cmd):
    prio = '' if m.get('prio', 'normal') == 'normal' else f' {m["prio"].upper()}'
    topic = f' #{m["topic"]}' if m.get('topic') else ''
    cc = f' cc={",".join(m["cc"])}' if m.get('cc') else ''
    body = f' [body: {cmd} read {m["id"]}]' if m.get('body') else ''
    re_ = f' re={m["reply_to"]}' if m.get('reply_to') else ''
    why = {'mention': ' @you', 'reply': ' reply-to-you', 'sub': ' thread-you-follow', 'watch': ' user-post',
           'all': ' @all'}.get(m.get('rcpt'), '')
    return f'[bus{prio} {m["id"]} from {m["from"]}{why}{topic}{cc}{re_}] {m["text"]}{body}'


def out(obj, as_json, text):
    print(json.dumps(obj, indent=1) if as_json else text)


def main(argv=None, root=None, default_reply_cmd=None):
    p = argparse.ArgumentParser(prog='orca-bus', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--dir', default=root)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('register', help='map a stable name to this (or a given) terminal')
    r.add_argument('name'); r.add_argument('--handle'); r.add_argument('--kind', choices=['claude', 'codex', 'shell'])
    r.add_argument('--session'); r.add_argument('--note')
    r.add_argument('--owner', help='who is alerted when this tab cannot take messages (a registered name)')
    r.add_argument('--mode', choices=['typed', 'inbox', 'file'],
                   help='typed (default): the daemon types messages in. file: only urgent is typed; run `watch` '
                        'under a Monitor. inbox: never typed (legacy)')
    u = sub.add_parser('unregister'); u.add_argument('name')
    s = sub.add_parser('send', help='send a message to one or more registered names (to a,b; --cc c,d)')
    s.add_argument('to'); s.add_argument('text'); s.add_argument('--from', dest='frm'); s.add_argument('--re', dest='reply_to')
    s.add_argument('--cc', default=''); s.add_argument('--prio', choices=['urgent', 'normal', 'fyi'], default='normal')
    s.add_argument('--topic'); s.add_argument('--body-file', help='long content: copied into bodies/<id>.md')
    s.add_argument('--now', action='store_true',
                   help='try one delivery right here (same lock and checks as the daemon); if it cannot, the daemon keeps it')
    i = sub.add_parser('inbox'); i.add_argument('--unread', action='store_true'); i.add_argument('--as', dest='me')
    i.add_argument('--json', action='store_true'); i.add_argument('--all', action='store_true', help='every message on the bus')
    i.add_argument('--follow', action='store_true',
                   help='print each new message to you as one line, forever (run it under a Monitor); with --unread, '
                        'print the unread ones first')
    wa = sub.add_parser('watch', help='for a Monitor: print each new message to you as one line (unread first)')
    wa.add_argument('--as', dest='me'); wa.add_argument('--fyi', action='store_true', help='also fyi (default: not)')
    wa.add_argument('--poll', type=float, default=2.0)
    dg = sub.add_parser('digest', help='unread messages grouped by prio and topic, one line each')
    dg.add_argument('--as', dest='me'); dg.add_argument('--mark', action='store_true', help='mark them all read')
    rd = sub.add_parser('read', help='print messages in full (body included) and mark them read')
    rd.add_argument('ids', nargs='+'); rd.add_argument('--as', dest='me')
    a = sub.add_parser('ack'); a.add_argument('ids', nargs='+'); a.add_argument('--as', dest='me')
    w = sub.add_parser('who'); w.add_argument('--json', action='store_true')
    st = sub.add_parser('status'); st.add_argument('id', nargs='?'); st.add_argument('--json', action='store_true')
    rx = sub.add_parser('react', help='react to a message (never notifies anyone)')
    rx.add_argument('id'); rx.add_argument('emoji'); rx.add_argument('--as', dest='me')
    rx.add_argument('--off', action='store_true', help='take your reaction back')
    for verb in ('sub', 'unsub', 'pin', 'unpin', 'delete'):
        x = sub.add_parser(verb); x.add_argument('id'); x.add_argument('--as', dest='me')
    ed = sub.add_parser('edit'); ed.add_argument('id'); ed.add_argument('text'); ed.add_argument('--as', dest='me')
    th = sub.add_parser('thread', help='print a thread'); th.add_argument('id'); th.add_argument('--json', action='store_true')
    d = sub.add_parser('deliver', help='run the delivery daemon (in its own plain terminal)')
    d.add_argument('--once', action='store_true'); d.add_argument('--interval', type=float, default=5)
    d.add_argument('--busy-grace', type=float, default=None,
                   help='ignored (kept for old command lines): a busy agent is never typed into')
    d.add_argument('--max-attempts', type=int, default=5); d.add_argument('--settle', type=float, default=3.0)
    d.add_argument('--alert-after', type=float, default=120,
                   help='seconds a tab may stay blocked (text the bus did not write, unreadable screen) before its '
                        'owner, the notify names and the waiting senders are told')
    d.add_argument('--draft-grace', type=float, default=1800, help='seconds before a blocked message fails')
    d.add_argument('--nudge-after', type=float, default=90,
                   help='file-mode tab: seconds of unread urgent/normal mail before one typed nudge')
    d.add_argument('--hold-grace', type=float, default=12 * 3600,
                   help='seconds a message to a closed tab waits for a successor before it fails')
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # titles carry spinner glyphs; a cp1252 console must not crash on them
        try:
            # piped (an agent's shell tool): UTF-8, so reaction emoji survive; a console keeps its own code page
            stream.reconfigure(errors='replace', **({} if stream.isatty() else {'encoding': 'utf-8'}))
        except (AttributeError, ValueError):
            pass

    try:
        bus = Bus(find_root(args.dir))
        if default_reply_cmd and not (bus.root / 'config.json').exists():
            (bus.root / 'config.json').write_text(json.dumps({'notify': [], 'reply_cmd': default_reply_cmd}, indent=1))

        if args.cmd == 'register':
            handle = args.handle or os.environ.get('ORCA_TERMINAL_HANDLE')
            kind = args.kind
            if not kind:
                terms = live_terminals() or {}
                agent = (terms.get(handle) or {}).get('agent')
                kind = agent if agent in ('claude', 'codex') else 'shell'
            old = bus.register(args.name, handle, kind, args.session, args.note, args.owner, args.mode)
            moved = f' (was {old.get("handle")})' if old and old.get('handle') != handle else ''
            mode = bus.registry()[args.name]['mode']
            print(f'registered {args.name} -> {handle} [{kind}, {mode}]{moved}')
        elif args.cmd == 'unregister':
            print('removed' if bus.unregister(args.name) else 'not registered')
        elif args.cmd == 'send':
            frm = whoami(bus, args.frm)
            body = Path(args.body_file).read_text(encoding='utf-8') if args.body_file else None
            ms = bus.send(frm, args.to, args.text, args.reply_to, cc=args.cc, prio=args.prio, topic=args.topic,
                          body=body, route=True)
            ms = ms if isinstance(ms, list) else [ms]
            for m in ms:
                why = f' ({m["rcpt"]})' if m.get('rcpt') not in (None, 'to') else ''
                print(f'{m["state"]} {m["id"]} {frm} -> {m["to"]}{why} [{m["prio"]}]'
                      + (f' thread={m["thread"]}' if m.get('thread') else '')
                      + (f' body={m["body"]}' if m.get('body') else ''))
            m = ms[0]
            if args.now and len(ms) == 1:
                from .deliver import Deliverer
                from .orca import Orca
                orca = Orca()
                Deliverer(bus, orca, holder=f'send --now by {frm}').deliver(
                    bus.messages()[m['id']], bus.registry().get(args.to), orca.terminals())
                m = bus.messages()[m['id']]
                print(f'{m["id"]} {m["state"]}: {m["history"][-1][2]}')
        elif args.cmd == 'watch':
            me = whoami(bus, args.me)
            cmd = bus.config().get('reply_cmd', 'orca-bus')
            prios = ('urgent', 'normal', 'fyi') if args.fyi else ('urgent', 'normal')
            print(f'[bus watch {me}: {len(bus.unread(me, prios))} unread; fyi in `{cmd} digest`]', flush=True)
            for m in bus.unread(me, prios):
                print(line_v2(m, cmd), flush=True)
                bus.mark_seen(me, [m['id']])
            for ev in bus.follow(me, poll=args.poll):
                ev.setdefault('prio', 'normal')
                if ev['prio'] in prios and ev['id'] not in bus.seen(me):
                    print(line_v2(ev, cmd), flush=True)
                    bus.mark_seen(me, [ev['id']])
        elif args.cmd == 'digest':
            me = whoami(bus, args.me)
            un = bus.unread(me)
            if not un:
                print(f'{me}: no unread messages')
            groups = {}
            for m in un:
                groups.setdefault((('urgent', 'normal', 'fyi').index(m['prio']), m.get('topic') or '-'), []).append(m)
            for (pi, topic), ms in sorted(groups.items()):
                print(f'== {("URGENT", "normal", "fyi")[pi]} / {topic} ({len(ms)})')
                for m in ms:
                    body = ' [body]' if m.get('body') else ''
                    print(f'  {m["id"]} {m["time"][5:16]} {m["from"]}: {m["text"][:150]}{body}')
            from . import chat
            rx, pos = chat.reactions_for(bus, me)
            if rx:
                print(f'== reactions to your messages ({len(rx)})')
                for ev in rx:
                    print(f'  {ev["id"]} {ev["time"][5:16]} {ev["by"]} {ev["emoji"]}')
            if args.mark:
                bus.mark_seen(me, [m['id'] for m in un])
                chat.mark_reactions(bus, me, pos)
                print(f'(marked {len(un)} read)')
        elif args.cmd == 'read':
            me = whoami(bus, args.me)
            mine = {m['id']: m for m in bus.inbox_file(me)}
            for mid in args.ids:
                m = mine.get(mid)
                if not m:
                    print(f'{mid}: not in the inbox of {me}')
                    continue
                rc = f' to={",".join(m.get("to_all", [m["to"]]))}' + (f' cc={",".join(m["cc"])}' if m.get('cc') else '')
                print(f'{m["id"]} {m["time"]} from={m["from"]}{rc} prio={m["prio"]} topic={m.get("topic", "-")} '
                      f're={m.get("reply_to") or "-"}')
                print(m['text'])
                if m.get('body'):
                    try:
                        print(Path(m['body']).read_text(encoding='utf-8'))
                    except OSError as ex:
                        print(f'(body unreadable: {ex})')
            bus.mark_seen(me, [i for i in args.ids if i in mine])
        elif args.cmd == 'inbox' and args.follow:
            me = whoami(bus, args.me)
            cmd = bus.config().get('reply_cmd', 'orca-bus')
            if args.unread:
                for m in bus.inbox(me, unread=True):
                    print(f'[bus {m["id"]} from {m["from"]}] {m["text"]} || reply: {cmd} send {m["from"]} --re {m["id"]} "..."', flush=True)
            for m in bus.follow(me):
                print(f'[bus {m["id"]} from {m["from"]}] {m["text"]} || reply: {cmd} send {m["from"]} --re {m["id"]} "..."', flush=True)
        elif args.cmd == 'inbox':
            if args.all:
                ms = sorted(bus.messages().values(), key=lambda m: m['time'])
            else:
                ms = bus.inbox(whoami(bus, args.me), args.unread)
            out(ms, args.json, '\n'.join(f'{m["id"]} {m["time"][11:19]} {m["from"]}->{m["to"]} [{m["state"]}] {m["text"]}'
                                          for m in ms) or '(empty)')
        elif args.cmd == 'ack':
            me = whoami(bus, args.me)
            for mid in args.ids:
                bus.ack(me, mid)
            print(f'acked {len(args.ids)}')
        elif args.cmd == 'who':
            reg, terms = bus.registry(), live_terminals()
            rows = []
            for n, e in sorted(reg.items()):
                live = None if terms is None else (e.get('handle') in terms if e.get('handle') else None)
                title = (terms or {}).get(e.get('handle'), {}).get('title', '')
                rows.append({'name': n, **e, 'live': live, 'title': title, 'mode': e.get('mode') or 'typed'})
            hb = bus.root / 'deliver.heartbeat'
            age = time.time() - hb.stat().st_mtime if hb.exists() else None
            daemon = 'daemon: ' + ('NOT RUNNING' if age is None or age > 60 else f'alive ({int(age)}s ago)')
            out({'daemon_heartbeat_age_s': age, 'names': rows}, args.json, '\n'.join(
                [daemon] + [f'{r["name"]:<16} {r["kind"]:<6} {r["mode"]:<5} {"LIVE" if r["live"] else ("DEAD" if r["live"] is False else "?"):<4} '
                            f'{(r["handle"] or "-")[:18]:<18} {r["title"][:40]}' for r in rows]))
        elif args.cmd == 'status' and args.id:
            from . import chat
            recs = chat.records(bus, args.id)
            if not recs:
                raise BusError(f'no message {args.id}')
            cache, rows = {}, []
            for r in recs:
                tick, detail = chat.receipt(bus, r, cache)
                rows.append({'id': r['id'], 'to': r['to'], 'rcpt': r.get('rcpt', 'to'), 'receipt': tick,
                             'detail': detail, 'since': r['history'][-1][0]})
            rx = chat.reactions(chat.chat_events(bus), {chat.base_id(args.id)}).get(chat.base_id(args.id), {})
            m = recs[0]
            out({'id': chat.base_id(m['id']), 'from': m['from'], 'text': m['text'], 'thread': m.get('thread'),
                 'receipts': rows, 'reactions': rx}, args.json, '\n'.join(
                [f'{chat.base_id(m["id"])} {m["time"][5:16]} {m["from"]}: {m["text"][:120]}']
                + [f'  {chat.TICKS[r["receipt"]]:<2} {r["receipt"]:<9} {r["to"]:<14} {r["rcpt"]:<7} {r["since"][11:19]}'
                   + (f'  {r["detail"][:70]}' if r['receipt'] in ('failed', 'expired', 'queued') else '') for r in rows]
                + ([' reactions: ' + '  '.join(f'{e} {",".join(w)}' for e, w in rx.items())] if rx else [])))
        elif args.cmd == 'react':
            from . import chat
            me = whoami(bus, args.me)
            ev = chat.react(bus, me, args.id, args.emoji, on=not args.off)
            print(f'{"removed" if args.off else "reacted"} {ev["emoji"]} on {ev["id"]} (nobody notified)')
        elif args.cmd in ('sub', 'unsub', 'pin', 'unpin'):
            from . import chat
            me = whoami(bus, args.me)
            fn = chat.subscribe if args.cmd.endswith('sub') else chat.pin
            ev = fn(bus, me, args.id, on=not args.cmd.startswith('un'))
            print(f'{args.cmd} {ev.get("thread") or ev.get("id")} for {me}')
        elif args.cmd in ('edit', 'delete'):
            from . import chat
            me = whoami(bus, args.me)
            ev = chat.edit(bus, me, args.id, args.text) if args.cmd == 'edit' else chat.delete(bus, me, args.id)
            print(f'{args.cmd}ed {ev["id"]} (shown in the chat; inboxes keep what was delivered)')
        elif args.cmd == 'thread':
            from . import chat
            ms = chat.thread_messages(bus, args.id)
            rx = chat.reactions(chat.chat_events(bus), {m['id'] for m in ms})
            out(ms, args.json, '\n'.join(
                f'{"  " if m["id"] != ms[0]["id"] else ""}{m["id"]} {m["time"][5:16]} {m["from"]} -> '
                f'{",".join(m["notified"]) or m["to"]}: {m["text"][:150]}'
                + (' [body]' if m.get('body') else '')
                + ''.join(f' {e}{len(w)}' for e, w in rx.get(m['id'], {}).items()) for m in ms) or '(empty)')
        elif args.cmd == 'status':
            ms = bus.messages()
            sel = [ms[args.id]] if args.id else sorted(ms.values(), key=lambda m: m['time'])[-20:]
            out(sel, args.json, '\n'.join(f'{m["id"]} {m["from"]}->{m["to"]} {m["state"]} '
                                          f'({m["history"][-1][2]}) {m["text"][:60]}' for m in sel))
        elif args.cmd == 'deliver':
            from .deliver import run
            from .orca import Orca
            run(bus, Orca(), interval=args.interval, once=args.once, busy_grace=args.busy_grace,
                max_attempts=args.max_attempts, settle=args.settle, alert_after=args.alert_after,
                draft_grace=args.draft_grace, nudge_after=args.nudge_after, hold_grace=args.hold_grace)
    except BusError as ex:
        print(f'orca-bus: {ex}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
