"""Read-only tools the LLM may call while deciding what to do with a message.

The tools are built for ONE flat (flat_id is fixed inside them), so the LLM has no way
to look at another flat. None of these tools change anything; all writes happen in the
agent's 'act' step after deterministic checks.
"""

from typing import Optional

from langchain_core.tools import tool

from flatmate import balances, ledger
from flatmate.balances import fmt
from flatmate.db import log, today

MEMORY_OFF = "Memory retrieval is disabled for this run."


def make_read_tools(conn, flat_id, cycle_id, memory_enabled=True):
    names = {m["member_id"]: m["short_id"] for m in conn.execute(
        "SELECT member_id, short_id FROM members WHERE flat_id = ?", (flat_id,))}

    def record_read(tool_name, args, result):
        log(conn, "memory_read", flat_id=flat_id, cycle_id=cycle_id, tool=tool_name,
            input=args, output=result[:500])
        return result

    @tool
    def search_expenses(amount: Optional[float] = None, category: Optional[str] = None,
                        keyword: Optional[str] = None, days: int = 31) -> str:
        """Search this flat's expense history (approved and pending) from the last `days` days.
        Filter by exact amount in rupees, category, and/or a keyword in the description.
        Use it to spot duplicates and to find the expense a correction or bill refers to."""
        args = {"amount": amount, "category": category, "keyword": keyword, "days": days}
        if not memory_enabled:
            return record_read("search_expenses", args, MEMORY_OFF)
        sql = ("SELECT * FROM expenses WHERE flat_id = ? AND status IN ('approved', 'pending') "
               "AND julianday(?) - julianday(expense_date) <= ?")
        params = [flat_id, today(conn).isoformat(), days]
        if amount is not None:
            sql += " AND amount = ?"
            params.append(balances.to_paisa(amount))
        if category:
            sql += " AND category = ?"
            params.append(category.lower())
        if keyword:
            sql += " AND description LIKE ?"
            params.append(f"%{keyword}%")
        rows = conn.execute(sql + " ORDER BY expense_date DESC LIMIT 15", params).fetchall()
        lines = [f"{r['expense_id']} v{r['version']} [{r['status']}] {r['expense_date']}: {r['description']} "
                 f"{fmt(r['amount'])} ({r['category']}), paid by {names[r['payer_id']]}, "
                 f"logged by {names[r['created_by']]}" for r in rows]
        return record_read("search_expenses", args, "\n".join(lines) or "No matching expenses.")

    @tool
    def get_preferences() -> str:
        """Current approved split preferences, e.g. a member excluded from a category."""
        if not memory_enabled:
            return record_read("get_preferences", {}, MEMORY_OFF)
        rows = ledger.current_preferences(conn, flat_id, "9999-12-31")
        lines = [f"{names[p['member_id']]}: {p['key']} = {p['value']} from {p['valid_from']}"
                 + (f" ({p['note']})" if p["note"] else "") for p in rows]
        return record_read("get_preferences", {}, "\n".join(lines) or "No preferences saved.")

    @tool
    def get_open_questions() -> str:
        """Questions the agent asked earlier that are still waiting for an answer,
        with the original message they are about."""
        if not memory_enabled:
            return record_read("get_open_questions", {}, MEMORY_OFF)
        lines = []
        for q in ledger.open_questions(conn, flat_id):
            about = conn.execute("SELECT text FROM messages WHERE msg_id = ?", (q["about_msg_id"],)).fetchone()
            lines.append(f"question #{q['question_id']} to {names[q['asked_to']]}: {q['text']} "
                         f"| original message: \"{about['text'] if about else '?'}\"")
        return record_read("get_open_questions", {}, "\n".join(lines) or "No open questions.")

    @tool
    def get_balances() -> str:
        """Current balances (positive = is owed money) and payment claims awaiting confirmation."""
        if not memory_enabled:
            return record_read("get_balances", {}, MEMORY_OFF)
        net = balances.net_balances(conn, flat_id)
        lines = [f"{names[m]}: {fmt(v)}" for m, v in net.items()]
        for s in ledger.open_settlements(conn, flat_id):
            lines.append(f"unconfirmed claim settle:{s['settlement_id']}: {names[s['from_id']]} says they paid "
                         f"{names[s['to_id']]} {fmt(s['amount'])}")
        return record_read("get_balances", {}, "\n".join(lines))

    @tool
    def get_recent_episodes() -> str:
        """Short summaries of this flat's last three agent cycles."""
        if not memory_enabled:
            return record_read("get_recent_episodes", {}, MEMORY_OFF)
        rows = conn.execute("SELECT business_date, summary FROM cycles WHERE flat_id = ? AND status = 'completed' "
                            "ORDER BY started_at DESC LIMIT 3", (flat_id,)).fetchall()
        lines = [f"{r['business_date']}: {r['summary']}" for r in rows]
        return record_read("get_recent_episodes", {}, "\n".join(lines) or "No earlier cycles.")

    return [search_expenses, get_preferences, get_open_questions, get_balances, get_recent_episodes]
