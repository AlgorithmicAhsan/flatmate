"""Command-line interface. Run `python -m flatmate --help` to see all commands."""

import argparse
import json
import sys

from flatmate import balances, config, db, ledger
from flatmate.balances import fmt


def cmd_clock(conn, args):
    if args.date:
        db.set_clock(conn, args.date)
    print(f"Demo clock: {db.today(conn)}")


def cmd_onboard(conn, args):
    if not config.EMAIL_ADDRESS:
        sys.exit("Set EMAIL_ADDRESS in .env first (member plus-addresses are built from it).")
    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    flat_id = ledger.onboard(conn, data, config.EMAIL_ADDRESS)
    print(f"Onboarded {flat_id}:")
    for m in ledger.active_members(conn, flat_id):
        print(f"  {m['short_id']:<8} {m['name']:<8} {m['role']:<7} {m['email']}")


def cmd_add(conn, args):
    """Queue a note from a member. The agent handles it on the next `run`."""
    ledger.get_flat(conn, args.flat)
    member = ledger.get_member(conn, args.flat, args.who)
    msg_id, _ = ledger.save_message(conn, args.flat, member["member_id"], "cli", args.text)
    db.log(conn, "memory_write", flat_id=args.flat, tool="save_message", actor="user",
           input={"from": member["member_id"], "text": args.text}, output={"msg_id": msg_id})
    print(f"Saved note {msg_id} from {member['name']}. It will be handled on the next run.")


def cmd_status(conn, args):
    flat = ledger.get_flat(conn, args.flat)
    names = {m["member_id"]: m["name"] for m in ledger.active_members(conn, args.flat)}
    net = balances.net_balances(conn, args.flat)

    print(f"== {flat['name']} ({args.flat}) on {db.today(conn)} ==")
    print("\nBalances (positive = is owed money):")
    for member_id, value in net.items():
        print(f"  {names.get(member_id, member_id):<8} {fmt(value):>14}")

    plan = balances.settlement_plan(net)
    print("\nSuggested settlement:" if plan else "\nEveryone is settled up.")
    for debtor, creditor, amount in plan:
        print(f"  {names[debtor]} pays {names[creditor]} {fmt(amount)}")

    pending = ledger.list_expenses(conn, args.flat, statuses=("pending",))
    prefs = conn.execute("SELECT COUNT(*) FROM preferences WHERE flat_id = ? AND status = 'pending'",
                         (args.flat,)).fetchone()[0]
    print(f"\nWaiting for approval: {len(pending)} expense(s), {prefs} preference change(s), "
          f"{len(ledger.open_settlements(conn, args.flat))} payment claim(s)")
    print(f"Open questions: {len(ledger.open_questions(conn, args.flat))}")
    print(f"Notes not yet handled: {len(ledger.unprocessed_messages(conn, args.flat))}")


def cmd_review(conn, args):
    """Everything waiting for a person, with the evidence behind it."""
    ledger.get_flat(conn, args.flat)
    names = {m["member_id"]: m["name"] for m in ledger.active_members(conn, args.flat)}
    nothing = True

    for e in ledger.list_expenses(conn, args.flat, statuses=("pending",)):
        nothing = False
        exp = ledger.get_expense(conn, args.flat, e["expense_id"], status="pending")
        kind = "New expense" if exp["version"] == 1 else f"Correction (v{exp['version']})"
        print(f"[{exp['expense_id']}] {kind}: {exp['description']} - {fmt(exp['amount'])} "
              f"({exp['category']}) paid by {names[exp['payer_id']]}")
        print(f"    split: " + ", ".join(f"{names[m]} {fmt(s)}" for m, s in exp["shares"].items()))
        print(f"    why pending: {exp['reason']}")
        if exp["version"] > 1:
            old = ledger.get_expense(conn, args.flat, exp["expense_id"])
            print(f"    currently approved: {fmt(old['amount'])} ({old['category']})")
        src = conn.execute("SELECT text FROM messages WHERE msg_id = ?", (exp["source_msg_id"],)).fetchone()
        if src:
            print(f"    evidence: \"{src['text']}\"")
        print(f"    approve: python -m flatmate approve --flat {args.flat} --as <member> {exp['expense_id']}")

    for p in conn.execute("SELECT * FROM preferences WHERE flat_id = ? AND status = 'pending'", (args.flat,)):
        nothing = False
        print(f"[pref:{p['pref_id']}] {names[p['member_id']]}: {p['key']} = {p['value']} "
              f"from {p['valid_from']} (proposed by {names[p['created_by']]})")

    for s in ledger.open_settlements(conn, args.flat):
        nothing = False
        print(f"[settle:{s['settlement_id']}] {names[s['from_id']]} says they paid "
              f"{names[s['to_id']]} {fmt(s['amount'])} - only {names[s['to_id']]} can confirm")

    for q in ledger.open_questions(conn, args.flat):
        nothing = False
        print(f"[question:{q['question_id']}] to {names[q['asked_to']]}: {q['text']}")

    if nothing:
        print("Nothing is waiting for approval.")


def _decide(conn, args, approve):
    """approve / reject. The item id says what kind of record it is."""
    item = args.item
    if item.startswith("pref:"):
        if not approve:
            sys.exit("Rejecting preference changes is not supported yet.")
        ledger.approve_preference(conn, args.flat, int(item[5:]), args.who)
    elif item.startswith("settle:"):
        ledger.decide_settlement(conn, args.flat, int(item[7:]), args.who, confirm=approve)
    elif approve:
        ledger.approve_expense(conn, args.flat, item, args.who)
    else:
        ledger.reject_expense(conn, args.flat, item, args.who)
    print(f"{'Approved' if approve else 'Rejected'} {item}.")


def cmd_history(conn, args):
    names = {m["member_id"]: m["name"] for m in conn.execute(
        "SELECT member_id, name FROM members WHERE flat_id = ?", (args.flat,))}
    rows = ledger.expense_history(conn, args.flat, args.expense_id)
    if not rows:
        sys.exit(f"No expense {args.expense_id} in {args.flat}.")
    for r in rows:
        print(f"v{r['version']} {r['status']:<10} {fmt(r['amount']):>12} {r['category']:<11} "
              f"{r['description']} (by {names.get(r['created_by'], r['created_by'])}, "
              f"approved by {r['approved_by'] or '-'})")


def cmd_run(conn, args):
    """Run one agent cycle for a flat (or resume an interrupted one)."""
    from flatmate import agent  # imported here so the other commands start fast

    print(f"Running cycle for {args.flat} on {db.today(conn)} "
          f"(memory {'on' if config.MEMORY_ENABLED else 'OFF'}, email {'off' if args.no_email else 'on'})")
    try:
        state = agent.run_cycle(conn, args.flat, resume=args.resume,
                                memory_enabled=config.MEMORY_ENABLED, use_email=not args.no_email,
                                simulate_email_failure=args.simulate_email_failure,
                                settle=args.settle, crash_after=args.crash_after)
    except agent.SimulatedCrash as e:
        sys.exit(f"Stopped: {e}. Run again with --resume to continue from the checkpoint.")
    print(f"\nCycle {state['cycle_id']}:")
    for line in state["outcomes"] or ["nothing new to handle"]:
        print(f"  - {line}")


def cmd_export(conn, args):
    from flatmate.export import export_flat
    print(f"Wrote {export_flat(conn, args.flat)} (a read-only view; the database is the real record)")


def cmd_outbox(conn, args):
    for r in conn.execute("SELECT * FROM outbox WHERE flat_id = ? ORDER BY outbox_id", (args.flat,)):
        print(f"#{r['outbox_id']:<3} {r['status']:<12} {r['kind']:<8} to {r['recipient_id']:<12} "
              f"tries={r['attempts']} {r['subject']}" + (f"  ERROR: {r['last_error']}" if r["last_error"] else ""))


def cmd_log(conn, args):
    rows = conn.execute("SELECT * FROM action_log WHERE flat_id = ? ORDER BY log_id DESC LIMIT ?",
                        (args.flat, args.limit)).fetchall()
    for r in reversed(rows):
        line = f"{r['ts']} {r['actor']:<6} {r['event']:<13} {r['tool'] or '':<20} {r['status']}"
        if r["error"]:
            line += f"  ERROR: {r['error']}"
        print(line)
        if args.verbose:
            print(f"    in:  {r['input']}\n    out: {r['output']}")


def build_parser():
    p = argparse.ArgumentParser(prog="python -m flatmate", description="FlatMate shared-flat expense agent")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("clock", help="show or set the demo clock, e.g. clock 2026-10-05")
    s.add_argument("date", nargs="?")
    s.set_defaults(func=cmd_clock)

    s = sub.add_parser("onboard", help="create a flat from a JSON file")
    s.add_argument("file")
    s.set_defaults(func=cmd_onboard)

    s = sub.add_parser("add", help="add a note from a member")
    s.add_argument("--flat", required=True)
    s.add_argument("--as", dest="who", required=True, help="member short id, e.g. sara")
    s.add_argument("text")
    s.set_defaults(func=cmd_add)

    for name, func, help_text in [("status", cmd_status, "balances and what is waiting"),
                                  ("review", cmd_review, "items waiting for approval, with evidence")]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--flat", required=True)
        s.set_defaults(func=func)

    for name, approve in [("approve", True), ("reject", False)]:
        s = sub.add_parser(name, help=f"{name} an expense id, pref:<id> or settle:<id>")
        s.add_argument("--flat", required=True)
        s.add_argument("--as", dest="who", required=True)
        s.add_argument("item")
        s.set_defaults(func=lambda conn, args, approve=approve: _decide(conn, args, approve))

    s = sub.add_parser("history", help="all versions of one expense")
    s.add_argument("--flat", required=True)
    s.add_argument("expense_id")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("run", help="run one agent cycle for a flat")
    s.add_argument("--flat", required=True)
    s.add_argument("--settle", action="store_true", help="also propose a settlement and send reminders")
    s.add_argument("--no-email", action="store_true", help="do not read or send email (drafts stay UNSENT)")
    s.add_argument("--simulate-email-failure", action="store_true", help="make every send fail (retry test)")
    s.add_argument("--crash-after", type=int, help="stop after N messages (restart/resume test)")
    s.add_argument("--resume", action="store_true", help="continue an interrupted cycle from its checkpoint")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("outbox", help="all queued, sent and failed emails")
    s.add_argument("--flat", required=True)
    s.set_defaults(func=cmd_outbox)

    s = sub.add_parser("export", help="write an Excel review view of a flat")
    s.add_argument("--flat", required=True)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("log", help="recent action log entries")
    s.add_argument("--flat", required=True)
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_log)
    return p


def main():
    args = build_parser().parse_args()
    conn = db.connect()
    try:
        args.func(conn, args)
    except (ledger.NotAllowed, ValueError) as e:
        db.log(conn, "error", flat_id=getattr(args, "flat", None), tool=args.command,
               actor="user", status="refused", error=str(e))
        sys.exit(f"Refused: {e}")
    finally:
        conn.close()
