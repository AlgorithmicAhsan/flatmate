"""The ledger: members, messages, expenses, preferences, settlements and questions.

Every function takes a flat_id and every query filters by it, so one flat can never
read or change another flat's records. Permission checks live here too, in plain code.
"""

import os
import uuid

from flatmate import balances, config
from flatmate.db import log, now_ts, today

CATEGORIES = ["groceries", "meat", "dining", "electricity", "gas", "internet",
              "water", "rent", "household", "other"]


class NotAllowed(Exception):
    """Raised when a permission or isolation rule blocks an action."""


# --- Flats and members ------------------------------------------------------

def onboard(conn, data, inbox_address):
    """Create a flat, its members and starting preferences from a JSON dict.

    Member emails:
      '+sara'         -> a plus-address of the test inbox, e.g. name+sara@gmail.com
      '$BILAL_EMAIL'  -> the address in that .env setting (keeps real addresses out of sample files);
                         if the setting is empty, falls back to a plus-address like '+bilal'
    Running it twice changes nothing.
    """
    flat_id = data["flat_id"]
    created = now_ts()
    conn.execute("INSERT OR IGNORE INTO flats (flat_id, name, created_at) VALUES (?, ?, ?)",
                 (flat_id, data["name"], created))

    local, domain = inbox_address.split("@")
    for m in data["members"]:
        email = m["email"]
        if email.startswith("$"):
            email = os.getenv(email[1:], "").strip() or f"+{m['id']}"
        if email.startswith("+"):
            email = f"{local}{email}@{domain}"
        conn.execute(
            "INSERT OR IGNORE INTO members (member_id, flat_id, short_id, name, email, role, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"{flat_id}.{m['id']}", flat_id, m["id"], m["name"], email.lower(), m["role"],
             data.get("start_date", today(conn).isoformat())))

    for p in data.get("preferences", []):
        conn.execute(
            "INSERT OR IGNORE INTO preferences (flat_id, member_id, key, value, valid_from, status, "
            "note, created_by, approved_by, idem_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'current', ?, 'onboarding', 'onboarding', ?, ?)",
            (flat_id, f"{flat_id}.{p['member']}", p["key"], p["value"],
             p.get("valid_from", data.get("start_date", today(conn).isoformat())),
             p.get("note"), f"onboard:{flat_id}:{p['member']}:{p['key']}:{p['value']}", created))
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, tool="onboard", actor="user",
        output={"members": [m["id"] for m in data["members"]]})
    return flat_id


def get_flat(conn, flat_id):
    flat = conn.execute("SELECT * FROM flats WHERE flat_id = ?", (flat_id,)).fetchone()
    if not flat:
        raise NotAllowed(f"unknown flat '{flat_id}'")
    return flat


def get_member(conn, flat_id, who):
    """Find a member of THIS flat by short id ('sara') or full id ('flatA.sara')."""
    member_id = who if "." in who else f"{flat_id}.{who}"
    row = conn.execute("SELECT * FROM members WHERE member_id = ? AND flat_id = ?",
                       (member_id, flat_id)).fetchone()
    if not row:
        raise NotAllowed(f"'{who}' is not a member of {flat_id}")
    return row


def active_members(conn, flat_id):
    return conn.execute("SELECT * FROM members WHERE flat_id = ? AND active = 1 ORDER BY member_id",
                        (flat_id,)).fetchall()


def member_by_email(conn, email):
    """Used for incoming email: the sender address decides the flat, never the email text."""
    return conn.execute("SELECT * FROM members WHERE email = ? AND active = 1",
                        (email.strip().lower(),)).fetchone()


# --- Messages (source knowledge) ---------------------------------------------

def save_message(conn, flat_id, sender_id, channel, text, msg_id=None, subject=None,
                 attachment_path=None):
    """Store a raw note or email. Returns (msg_id, is_new). The same msg_id is stored once."""
    msg_id = msg_id or f"cli-{uuid.uuid4().hex[:12]}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages (msg_id, flat_id, sender_id, channel, subject, text, "
        "attachment_path, received_at, business_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (msg_id, flat_id, sender_id, channel, subject, text, attachment_path, now_ts(),
         today(conn).isoformat()))
    conn.commit()
    return msg_id, cur.rowcount == 1


def unprocessed_messages(conn, flat_id):
    return conn.execute(
        "SELECT * FROM messages WHERE flat_id = ? AND processed_at IS NULL ORDER BY received_at",
        (flat_id,)).fetchall()


def mark_processed(conn, flat_id, msg_id, outcome):
    conn.execute("UPDATE messages SET processed_at = ?, outcome = ? WHERE msg_id = ? AND flat_id = ?",
                 (now_ts(), outcome, msg_id, flat_id))
    conn.commit()


# --- Preferences -------------------------------------------------------------

def current_preferences(conn, flat_id, on_date=None):
    """Approved preferences that apply on on_date (an ISO date string; default: today)."""
    on_date = on_date or today(conn).isoformat()
    return conn.execute(
        "SELECT * FROM preferences WHERE flat_id = ? AND status = 'current' AND valid_from <= ? "
        "ORDER BY member_id", (flat_id, on_date)).fetchall()


def propose_preference(conn, flat_id, member, key, value, valid_from, proposed_by,
                       source_msg_id=None, note=None):
    """A preference change starts as 'pending'. It takes effect only after approval."""
    member = get_member(conn, flat_id, member)
    proposer = get_member(conn, flat_id, proposed_by)
    if key not in ("exclude_category", "include_category") or value not in CATEGORIES:
        raise ValueError(f"unsupported preference {key}={value}")
    if proposer["member_id"] != member["member_id"] and proposer["role"] != "admin":
        raise NotAllowed("only the member concerned or the admin can propose this change")

    idem = f"pref:{source_msg_id or uuid.uuid4().hex}:{member['member_id']}:{key}:{value}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO preferences (flat_id, member_id, key, value, valid_from, status, note, "
        "source_msg_id, created_by, idem_key, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
        (flat_id, member["member_id"], key, value, valid_from, note, source_msg_id,
         proposer["member_id"], idem, now_ts()))
    conn.commit()
    pref_id = cur.lastrowid if cur.rowcount else conn.execute(
        "SELECT pref_id FROM preferences WHERE idem_key = ?", (idem,)).fetchone()["pref_id"]
    log(conn, "memory_write", flat_id=flat_id, tool="propose_preference",
        input={"member": member["member_id"], "key": key, "value": value, "valid_from": valid_from},
        output={"pref_id": pref_id, "new": bool(cur.rowcount)})
    return pref_id


def approve_preference(conn, flat_id, pref_id, approver):
    pref = conn.execute("SELECT * FROM preferences WHERE pref_id = ? AND flat_id = ?",
                        (pref_id, flat_id)).fetchone()
    if not pref or pref["status"] != "pending":
        raise NotAllowed(f"preference {pref_id} is not pending in {flat_id}")
    approver = get_member(conn, flat_id, approver)
    if approver["member_id"] == pref["created_by"]:
        raise NotAllowed("a change must be approved by someone other than the person who proposed it")

    # Opposite rule for the same member and category is superseded (history is kept).
    opposite = "include_category" if pref["key"] == "exclude_category" else "exclude_category"
    old = conn.execute(
        "SELECT pref_id FROM preferences WHERE flat_id = ? AND member_id = ? AND value = ? "
        "AND status = 'current' AND key IN (?, ?)",
        (flat_id, pref["member_id"], pref["value"], pref["key"], opposite)).fetchall()
    for o in old:
        conn.execute("UPDATE preferences SET status = 'superseded' WHERE pref_id = ?", (o["pref_id"],))
    conn.execute("UPDATE preferences SET status = 'current', approved_by = ?, supersedes_id = ? "
                 "WHERE pref_id = ?",
                 (approver["member_id"], old[0]["pref_id"] if old else None, pref_id))
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, tool="approve_preference", actor="user",
        input={"pref_id": pref_id, "approver": approver["member_id"]},
        output={"superseded": [o["pref_id"] for o in old]})


def participants_for(conn, flat_id, category, on_date, exclude=(), use_preferences=True):
    """Who shares an expense: all active members, minus people named in the note,
    minus anyone whose current preference excludes this category on that date.
    use_preferences=False is only for the memory on/off comparison."""
    people = {m["member_id"] for m in active_members(conn, flat_id)}
    people -= {get_member(conn, flat_id, x)["member_id"] for x in exclude}
    if not use_preferences:
        return sorted(people)
    for p in current_preferences(conn, flat_id, on_date):
        if p["key"] == "exclude_category" and p["value"] == category:
            people.discard(p["member_id"])
    return sorted(people)


# --- Expenses ----------------------------------------------------------------

def _next_expense_id(conn, flat_id):
    n = conn.execute("SELECT COUNT(DISTINCT expense_id) AS n FROM expenses WHERE flat_id = ?",
                     (flat_id,)).fetchone()["n"]
    return f"{flat_id}-E{n + 1:03d}"


def _insert_version(conn, flat_id, expense_id, version, payer_id, amount, category, description,
                    expense_date, participants, status, source_msg_id, created_by, reason, idem_key):
    cur = conn.execute(
        "INSERT INTO expenses (expense_id, version, flat_id, payer_id, amount, category, description, "
        "expense_date, status, source_msg_id, created_by, approved_by, reason, idem_key, created_at, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (expense_id, version, flat_id, payer_id, amount, category, description, expense_date, status,
         source_msg_id, created_by, "auto" if status == "approved" else None, reason, idem_key, now_ts(),
         now_ts() if status == "approved" else None))
    for member_id, share in balances.split_equal(amount, participants).items():
        conn.execute("INSERT INTO expense_shares (row_id, member_id, share) VALUES (?, ?, ?)",
                     (cur.lastrowid, member_id, share))
    if source_msg_id:
        conn.execute("INSERT OR IGNORE INTO expense_evidence (expense_id, msg_id, kind, added_at) "
                     "VALUES (?, ?, ?, ?)",
                     (expense_id, source_msg_id, "source" if version == 1 else "correction", now_ts()))


def add_expense(conn, flat_id, payer, amount, category, description, created_by, participants,
                source_msg_id=None, expense_date=None, needs_approval_reason=None):
    """Record an expense. It is auto-approved only if the rules allow it; otherwise 'pending'.

    Rules (deterministic): the person logging it is the payer, and the amount is under
    AUTO_RECORD_LIMIT, and the caller did not flag a reason to hold it.
    Returns (expense_id, status). Retrying with the same source message returns the same expense.
    """
    payer = get_member(conn, flat_id, payer)
    creator = get_member(conn, flat_id, created_by)
    if category not in CATEGORIES:
        raise ValueError(f"unknown category '{category}'")
    participants = [get_member(conn, flat_id, p)["member_id"] for p in participants]

    idem = f"expense:{source_msg_id}" if source_msg_id else f"expense:{uuid.uuid4().hex}"
    existing = conn.execute("SELECT expense_id, status FROM expenses WHERE idem_key = ?", (idem,)).fetchone()
    if existing:
        return existing["expense_id"], existing["status"]

    reasons = []
    if needs_approval_reason:
        reasons.append(needs_approval_reason)
    if payer["member_id"] != creator["member_id"]:
        reasons.append("logged by someone other than the payer")
    if amount >= config.AUTO_RECORD_LIMIT:
        reasons.append(f"amount is at or above the auto-record limit of {balances.fmt(config.AUTO_RECORD_LIMIT)}")
    status = "pending" if reasons else "approved"

    expense_id = _next_expense_id(conn, flat_id)
    _insert_version(conn, flat_id, expense_id, 1, payer["member_id"], amount, category, description,
                    (expense_date or today(conn).isoformat()), participants, status, source_msg_id,
                    creator["member_id"], "; ".join(reasons) or None, idem)
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, tool="add_expense",
        input={"payer": payer["member_id"], "amount": amount, "category": category,
               "participants": participants, "source_msg_id": source_msg_id},
        output={"expense_id": expense_id, "status": status, "reasons": reasons})
    return expense_id, status


def get_expense(conn, flat_id, expense_id, status="approved"):
    """Latest version of an expense with the given status, plus its shares."""
    row = conn.execute(
        "SELECT * FROM expenses WHERE flat_id = ? AND expense_id = ? AND status = ? "
        "ORDER BY version DESC LIMIT 1", (flat_id, expense_id, status)).fetchone()
    if not row:
        return None
    shares = {s["member_id"]: s["share"] for s in conn.execute(
        "SELECT member_id, share FROM expense_shares WHERE row_id = ?", (row["row_id"],))}
    return dict(row) | {"shares": shares}


def propose_correction(conn, flat_id, expense_id, proposed_by, source_msg_id=None, amount=None,
                       category=None, description=None, participants=None, reason="correction"):
    """Create a new 'pending' version of an approved expense. The old version stays
    approved (and counts in balances) until the correction is approved."""
    current = get_expense(conn, flat_id, expense_id)
    if not current:
        raise NotAllowed(f"no approved expense {expense_id} in {flat_id}")
    proposer = get_member(conn, flat_id, proposed_by)
    if proposer["member_id"] != current["payer_id"] and proposer["role"] != "admin":
        raise NotAllowed("only the payer or the admin can correct an expense")

    idem = f"correction:{source_msg_id}" if source_msg_id else f"correction:{uuid.uuid4().hex}"
    existing = conn.execute("SELECT version FROM expenses WHERE idem_key = ?", (idem,)).fetchone()
    if existing:
        return existing["version"]

    category = category or current["category"]
    if category not in CATEGORIES:
        raise ValueError(f"unknown category '{category}'")
    if participants is None:
        participants = list(current["shares"])
    participants = [get_member(conn, flat_id, p)["member_id"] for p in participants]
    version = conn.execute("SELECT MAX(version) AS v FROM expenses WHERE expense_id = ? AND flat_id = ?",
                           (expense_id, flat_id)).fetchone()["v"] + 1

    _insert_version(conn, flat_id, expense_id, version, current["payer_id"], amount or current["amount"],
                    category, description or current["description"], current["expense_date"],
                    participants, "pending", source_msg_id, proposer["member_id"], reason, idem)
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, tool="propose_correction",
        input={"expense_id": expense_id, "amount": amount, "category": category,
               "participants": participants},
        output={"version": version, "status": "pending"})
    return version


def approve_expense(conn, flat_id, expense_id, approver):
    """Approve the pending version of an expense (a new expense or a correction).

    The approver must be someone other than the person who created that version, and
    must either share the expense or be the admin. An approved correction supersedes
    the previous approved version.
    """
    pending = get_expense(conn, flat_id, expense_id, status="pending")
    if not pending:
        raise NotAllowed(f"{expense_id} has nothing pending in {flat_id}")
    approver = get_member(conn, flat_id, approver)
    if approver["member_id"] == pending["created_by"]:
        raise NotAllowed("you cannot approve your own entry")
    if approver["member_id"] not in pending["shares"] and approver["role"] != "admin":
        raise NotAllowed("only someone who shares this expense, or the admin, can approve it")

    conn.execute("UPDATE expenses SET status = 'superseded', decided_at = ? "
                 "WHERE flat_id = ? AND expense_id = ? AND status = 'approved'",
                 (now_ts(), flat_id, expense_id))
    conn.execute("UPDATE expenses SET status = 'approved', approved_by = ?, decided_at = ? WHERE row_id = ?",
                 (approver["member_id"], now_ts(), pending["row_id"]))
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, tool="approve_expense", actor="user",
        input={"expense_id": expense_id, "version": pending["version"], "approver": approver["member_id"]})


def reject_expense(conn, flat_id, expense_id, approver):
    pending = get_expense(conn, flat_id, expense_id, status="pending")
    if not pending:
        raise NotAllowed(f"{expense_id} has nothing pending in {flat_id}")
    approver = get_member(conn, flat_id, approver)
    if approver["member_id"] not in pending["shares"] and approver["role"] != "admin":
        raise NotAllowed("only someone who shares this expense, or the admin, can reject it")
    conn.execute("UPDATE expenses SET status = 'rejected', approved_by = ?, decided_at = ? WHERE row_id = ?",
                 (approver["member_id"], now_ts(), pending["row_id"]))
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, tool="reject_expense", actor="user",
        input={"expense_id": expense_id, "version": pending["version"], "by": approver["member_id"]})


def list_expenses(conn, flat_id, statuses=("approved", "pending")):
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT * FROM expenses WHERE flat_id = ? AND status IN ({marks}) ORDER BY expense_id, version",
        (flat_id, *statuses)).fetchall()


def expense_history(conn, flat_id, expense_id):
    return conn.execute("SELECT * FROM expenses WHERE flat_id = ? AND expense_id = ? ORDER BY version",
                        (flat_id, expense_id)).fetchall()


def find_similar(conn, flat_id, amount, category=None, around=None, days=7):
    """History search for possible duplicates: same amount (and category if given)
    within +/- days of the given date, approved or pending."""
    around = around or today(conn).isoformat()
    sql = ("SELECT * FROM expenses WHERE flat_id = ? AND amount = ? AND status IN ('approved', 'pending') "
           "AND ABS(julianday(expense_date) - julianday(?)) <= ?")
    args = [flat_id, amount, around, days]
    if category:
        sql += " AND category = ?"
        args.append(category)
    return conn.execute(sql, args).fetchall()


def link_evidence(conn, flat_id, expense_id, msg_id, kind):
    if not conn.execute("SELECT 1 FROM expenses WHERE flat_id = ? AND expense_id = ?",
                        (flat_id, expense_id)).fetchone():
        raise NotAllowed(f"no expense {expense_id} in {flat_id}")
    conn.execute("INSERT OR IGNORE INTO expense_evidence (expense_id, msg_id, kind, added_at) "
                 "VALUES (?, ?, ?, ?)", (expense_id, msg_id, kind, now_ts()))
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, tool="link_evidence",
        input={"expense_id": expense_id, "msg_id": msg_id, "kind": kind})


# --- Settlements -------------------------------------------------------------

def claim_settlement(conn, flat_id, payer, receiver, amount, source_msg_id=None):
    """Someone says they paid. It stays 'claimed' and does not change balances
    until the receiver confirms."""
    payer = get_member(conn, flat_id, payer)
    receiver = get_member(conn, flat_id, receiver)
    idem = f"settle:{source_msg_id}" if source_msg_id else f"settle:{uuid.uuid4().hex}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO settlements (flat_id, from_id, to_id, amount, status, claim_msg_id, "
        "idem_key, created_at) VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?)",
        (flat_id, payer["member_id"], receiver["member_id"], amount, source_msg_id, idem, now_ts()))
    conn.commit()
    sid = conn.execute("SELECT settlement_id FROM settlements WHERE idem_key = ?", (idem,)).fetchone()[0]
    log(conn, "memory_write", flat_id=flat_id, tool="claim_settlement",
        input={"from": payer["member_id"], "to": receiver["member_id"], "amount": amount},
        output={"settlement_id": sid, "new": bool(cur.rowcount)})
    return sid


def decide_settlement(conn, flat_id, settlement_id, receiver, confirm=True):
    s = conn.execute("SELECT * FROM settlements WHERE settlement_id = ? AND flat_id = ?",
                     (settlement_id, flat_id)).fetchone()
    if not s or s["status"] != "claimed":
        raise NotAllowed(f"settlement {settlement_id} is not awaiting confirmation in {flat_id}")
    receiver = get_member(conn, flat_id, receiver)
    if receiver["member_id"] != s["to_id"]:
        raise NotAllowed("only the person who received the money can confirm it")
    status = "confirmed" if confirm else "rejected"
    conn.execute("UPDATE settlements SET status = ?, confirmed_by = ?, decided_at = ? WHERE settlement_id = ?",
                 (status, receiver["member_id"], now_ts(), settlement_id))
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, tool="decide_settlement", actor="user",
        input={"settlement_id": settlement_id, "status": status})


def open_settlements(conn, flat_id):
    return conn.execute("SELECT * FROM settlements WHERE flat_id = ? AND status = 'claimed'",
                        (flat_id,)).fetchall()


# --- Questions the agent is waiting on ----------------------------------------

def ask_question(conn, flat_id, asked_to, text, about_msg_id=None, cycle_id=None):
    member = get_member(conn, flat_id, asked_to)
    idem = f"q:{about_msg_id}:{member['member_id']}" if about_msg_id else f"q:{uuid.uuid4().hex}"
    conn.execute(
        "INSERT OR IGNORE INTO questions (flat_id, cycle_id, about_msg_id, asked_to, text, status, "
        "idem_key, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
        (flat_id, cycle_id, about_msg_id, member["member_id"], text, idem, now_ts()))
    conn.commit()
    return conn.execute("SELECT question_id FROM questions WHERE idem_key = ?", (idem,)).fetchone()[0]


def open_questions(conn, flat_id):
    return conn.execute("SELECT * FROM questions WHERE flat_id = ? AND status = 'open' ORDER BY question_id",
                        (flat_id,)).fetchall()


def answer_question(conn, flat_id, question_id, answer_msg_id):
    conn.execute("UPDATE questions SET status = 'answered', answer_msg_id = ?, answered_at = ? "
                 "WHERE question_id = ? AND flat_id = ?", (answer_msg_id, now_ts(), question_id, flat_id))
    conn.commit()
