"""Deterministic money maths: splitting, balances, and settlement plans.

All amounts are integers in paisa (Rs 1 = 100). The LLM never does any of this.
"""

from flatmate import config


def to_paisa(rupees):
    """'1,800' or 1800 or 1800.5 -> 180000 / 180050."""
    value = round(float(str(rupees).replace(",", "")) * 100)
    if value <= 0:
        raise ValueError(f"amount must be positive, got {rupees!r}")
    return value


def fmt(paisa):
    """180000 -> 'Rs 1,800'   180050 -> 'Rs 1,800.50'"""
    sign = "-" if paisa < 0 else ""
    rupees, rest = divmod(abs(paisa), 100)
    text = f"{rupees:,}" if rest == 0 else f"{rupees:,}.{rest:02d}"
    return f"{sign}{config.CURRENCY} {text}"


def split_equal(amount, member_ids):
    """Split amount equally. Leftover paisa go one each to the first members by id,
    so the result is always the same and always adds up to the exact total."""
    if not member_ids:
        raise ValueError("cannot split an expense between zero people")
    members = sorted(member_ids)
    base, leftover = divmod(amount, len(members))
    return {m: base + (1 if i < leftover else 0) for i, m in enumerate(members)}


def net_balances(conn, flat_id):
    """Positive = the flat owes this member money. Negative = this member owes.

    Uses only approved expenses and confirmed settlements.
    """
    net = {r["member_id"]: 0 for r in conn.execute(
        "SELECT member_id FROM members WHERE flat_id = ?", (flat_id,))}

    for e in conn.execute(
            "SELECT row_id, payer_id, amount FROM expenses WHERE flat_id = ? AND status = 'approved'",
            (flat_id,)):
        net[e["payer_id"]] += e["amount"]
        for s in conn.execute("SELECT member_id, share FROM expense_shares WHERE row_id = ?", (e["row_id"],)):
            net[s["member_id"]] -= s["share"]

    for s in conn.execute(
            "SELECT from_id, to_id, amount FROM settlements WHERE flat_id = ? AND status = 'confirmed'",
            (flat_id,)):
        net[s["from_id"]] += s["amount"]   # paying back reduces what you owe
        net[s["to_id"]] -= s["amount"]

    return net


def settlement_plan(net):
    """Fewest-transfers style plan: the biggest debtor pays the biggest creditor, repeat.

    Returns a list of (from_member, to_member, amount).
    """
    debtors = sorted([(-v, m) for m, v in net.items() if v < 0], reverse=True)
    creditors = sorted([(v, m) for m, v in net.items() if v > 0], reverse=True)
    plan = []
    while debtors and creditors:
        owe, debtor = debtors.pop(0)
        due, creditor = creditors.pop(0)
        paid = min(owe, due)
        plan.append((debtor, creditor, paid))
        if owe > paid:
            debtors.insert(0, (owe - paid, debtor))
            debtors.sort(reverse=True)
        if due > paid:
            creditors.insert(0, (due - paid, creditor))
            creditors.sort(reverse=True)
    return plan
