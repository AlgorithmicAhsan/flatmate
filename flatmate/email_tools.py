"""Email tools: read new emails over IMAP, and send queued emails over SMTP.

Every outgoing email goes through the outbox table first. Its msg_key is unique, so
queuing the same email twice does nothing, and a sent email is never sent again.
"""

import email
import imaplib
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr

from flatmate import config, ledger
from flatmate.db import log, now_ts, today


# --- Reading -----------------------------------------------------------------

def _plain_text(msg):
    """Return the text/plain body of an email, without the quoted earlier message."""
    body = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            break
    lines = []
    for line in body.splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:\s*$", line):
            break  # everything below is the quoted earlier email
        lines.append(line)
    return "\n".join(lines).strip()


def _get_last_uid(conn, flat_id):
    row = conn.execute("SELECT last_email_uid FROM working_state WHERE flat_id = ?", (flat_id,)).fetchone()
    return (row["last_email_uid"] or 0) if row else 0


def _set_last_uid(conn, flat_id, uid):
    conn.execute(
        "INSERT INTO working_state (flat_id, last_email_uid, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(flat_id) DO UPDATE SET last_email_uid = excluded.last_email_uid, "
        "updated_at = excluded.updated_at", (flat_id, uid, now_ts()))
    conn.commit()


def read_new_emails(conn, flat_id, cycle_id=None):
    """email_read tool. Fetches new emails sent FROM this flat's member addresses and stores
    them as messages. The sender address decides the flat; the email text never does.

    Returns the list of new msg_ids.
    """
    members = {m["email"]: m for m in ledger.active_members(conn, flat_id)}
    flat = ledger.get_flat(conn, flat_id)
    since = datetime.fromisoformat(flat["created_at"]).strftime("%d-%b-%Y")
    last_uid = _get_last_uid(conn, flat_id)

    imap = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
    try:
        imap.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)  # read-only: we never change the inbox
        uids = set()
        for address in members:
            _, data = imap.uid("search", None, "SINCE", since, "FROM", f'"{address}"')
            uids.update(int(u) for u in data[0].split())

        new_ids, skipped = [], 0
        for uid in sorted(u for u in uids if u > last_uid):
            _, data = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            msg = email.message_from_bytes(data[0][1])
            sender = parseaddr(msg.get("From", ""))[1].lower()
            member = members.get(sender)   # Gmail search is loose, so check the exact address
            if member:
                msg_id, is_new = ledger.save_message(
                    conn, flat_id, member["member_id"], "email", _plain_text(msg),
                    msg_id=(msg.get("Message-ID") or f"<uid-{uid}>").strip(), subject=msg.get("Subject"))
                if is_new:
                    new_ids.append(msg_id)
            else:
                skipped += 1
            _set_last_uid(conn, flat_id, uid)
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    log(conn, "tool_call", flat_id=flat_id, cycle_id=cycle_id, tool="email_read",
        input={"after_uid": last_uid}, output={"new_messages": new_ids, "skipped_non_members": skipped})
    return new_ids


# --- Sending -----------------------------------------------------------------

def queue_email(conn, flat_id, cycle_id, kind, recipient, subject, body, msg_key,
                debt_ref=None, approval="auto"):
    """Put an email in the outbox as UNSENT_DRAFT. Returns True if it was newly queued."""
    member = ledger.get_member(conn, flat_id, recipient)
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox (msg_key, flat_id, cycle_id, kind, recipient_id, recipient_email, "
        "subject, body, debt_ref, approval, status, business_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNSENT_DRAFT', ?, ?)",
        (msg_key, flat_id, cycle_id, kind, member["member_id"], member["email"],
         f"[FlatMate {flat_id}] {subject}", body, debt_ref, approval, today(conn).isoformat(), now_ts()))
    conn.commit()
    if cur.rowcount:
        log(conn, "memory_write", flat_id=flat_id, cycle_id=cycle_id, tool="queue_email",
            input={"kind": kind, "to": member["member_id"], "msg_key": msg_key})
    return cur.rowcount == 1


def cancel_unsent(conn, flat_id, outbox_id, reason, cycle_id=None):
    """Withdraw an email that has not been sent yet (e.g. the question was already answered)."""
    conn.execute("UPDATE outbox SET status = 'cancelled', last_error = ? WHERE outbox_id = ? AND flat_id = ? "
                 "AND status IN ('UNSENT_DRAFT', 'failed')", (reason, outbox_id, flat_id))
    conn.commit()
    log(conn, "memory_write", flat_id=flat_id, cycle_id=cycle_id, tool="cancel_email",
        input={"outbox_id": outbox_id, "reason": reason})


def send_outbox(conn, flat_id, cycle_id=None, simulate_failure=False, max_attempts=3):
    """email_send tool. Sends approved drafts and retries failed ones (up to max_attempts).

    A failure is recorded on the row and in the log; the draft stays in the outbox, so the
    next run retries it. Rows already 'sent' are never picked up again.
    """
    rows = conn.execute(
        "SELECT * FROM outbox WHERE flat_id = ? AND status IN ('UNSENT_DRAFT', 'failed') "
        "AND approval IN ('auto', 'approved') AND attempts < ? ORDER BY outbox_id",
        (flat_id, max_attempts)).fetchall()
    sent = failed = 0
    smtp = None
    for row in rows:
        try:
            if simulate_failure:
                raise ConnectionError("simulated SMTP failure")
            if smtp is None:
                smtp = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ssl.create_default_context())
                smtp.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
            msg = EmailMessage()
            msg["From"] = config.EMAIL_ADDRESS
            msg["To"] = row["recipient_email"]
            msg["Subject"] = row["subject"]
            msg["X-FlatMate-Key"] = row["msg_key"]
            msg.set_content(row["body"])
            smtp.send_message(msg)
            conn.execute("UPDATE outbox SET status = 'sent', attempts = attempts + 1, sent_at = ?, "
                         "last_error = NULL WHERE outbox_id = ?", (now_ts(), row["outbox_id"]))
            conn.commit()
            sent += 1
            log(conn, "tool_call", flat_id=flat_id, cycle_id=cycle_id, tool="email_send",
                input={"msg_key": row["msg_key"], "to": row["recipient_id"]}, output={"status": "sent"})
        except Exception as e:
            conn.execute("UPDATE outbox SET status = 'failed', attempts = attempts + 1, last_error = ? "
                         "WHERE outbox_id = ?", (str(e), row["outbox_id"]))
            conn.commit()
            failed += 1
            log(conn, "error", flat_id=flat_id, cycle_id=cycle_id, tool="email_send",
                input={"msg_key": row["msg_key"], "to": row["recipient_id"]}, status="failed", error=str(e))
    if smtp:
        smtp.quit()
    return sent, failed
