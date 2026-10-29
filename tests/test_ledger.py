"""Tests for the deterministic core. Run: python -m unittest discover tests"""

import json
import tempfile
import unittest
from pathlib import Path

from flatmate import balances, db, ledger

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"
INBOX = "test.inbox@gmail.com"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        db.set_clock(self.conn, "2026-10-05")
        for name in ("flat_a.json", "flat_b.json"):
            ledger.onboard(self.conn, json.loads((SAMPLES / name).read_text()), INBOX)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, payer, rupees, category, participants, created_by=None, msg=None):
        return ledger.add_expense(self.conn, "flatA", payer, balances.to_paisa(rupees), category,
                                  f"{category} test", created_by or payer, participants, source_msg_id=msg)

    def test_split_always_adds_up(self):
        shares = balances.split_equal(100_00 + 1, ["c", "a", "b"])
        self.assertEqual(sum(shares.values()), 100_01)
        self.assertEqual(shares, {"a": 33_34, "b": 33_34, "c": 33_33})

    def test_onboarding_builds_plus_addresses_and_is_repeatable(self):
        sara = ledger.get_member(self.conn, "flatA", "sara")
        self.assertEqual(sara["email"], "test.inbox+sara@gmail.com")
        ledger.onboard(self.conn, json.loads((SAMPLES / "flat_a.json").read_text()), INBOX)
        self.assertEqual(len(ledger.active_members(self.conn, "flatA")), 3)

    def test_vegetarian_preference_excludes_ali_from_meat(self):
        people = ledger.participants_for(self.conn, "flatA", "meat", "2026-10-05")
        self.assertNotIn("flatA.ali", people)
        self.assertIn("flatA.ali", ledger.participants_for(self.conn, "flatA", "groceries", "2026-10-05"))

    def test_balances_and_settlement_plan(self):
        self.add("sara", 1800, "groceries", ["sara", "bilal", "ali"])
        self.add("bilal", 1200, "meat", ["sara", "bilal"])
        net = balances.net_balances(self.conn, "flatA")
        # Sara: +1800 -600 -600 = 600; Bilal: +1200 -600 -600 = 0; Ali: -600
        self.assertEqual(net, {"flatA.sara": 600_00, "flatA.bilal": 0, "flatA.ali": -600_00})
        self.assertEqual(balances.settlement_plan(net), [("flatA.ali", "flatA.sara", 600_00)])

    def test_large_or_third_party_expense_stays_pending(self):
        _, status = self.add("sara", 6400, "electricity", ["sara", "bilal", "ali"])
        self.assertEqual(status, "pending")
        _, status = self.add("sara", 500, "groceries", ["sara", "bilal"], created_by="bilal")
        self.assertEqual(status, "pending")
        self.assertEqual(balances.net_balances(self.conn, "flatA")["flatA.sara"], 0)

    def test_same_source_message_never_creates_two_expenses(self):
        first = self.add("sara", 300, "groceries", ["sara", "ali"], msg=None)
        msg_id, _ = ledger.save_message(self.conn, "flatA", "flatA.sara", "cli", "paid 300")
        a = self.add("sara", 300, "groceries", ["sara", "ali"], msg=msg_id)
        b = self.add("sara", 300, "groceries", ["sara", "ali"], msg=msg_id)
        self.assertEqual(a, b)
        self.assertNotEqual(first[0], a[0])

    def test_correction_supersedes_only_after_approval_and_keeps_history(self):
        eid, _ = self.add("sara", 1800, "groceries", ["sara", "bilal", "ali"])
        ledger.propose_correction(self.conn, "flatA", eid, "sara", amount=balances.to_paisa(1500))
        self.assertEqual(balances.net_balances(self.conn, "flatA")["flatA.sara"], 1200_00)  # old still counts
        with self.assertRaises(ledger.NotAllowed):
            ledger.approve_expense(self.conn, "flatA", eid, "sara")  # cannot approve own change
        ledger.approve_expense(self.conn, "flatA", eid, "bilal")
        self.assertEqual(balances.net_balances(self.conn, "flatA")["flatA.sara"], 1000_00)
        statuses = [r["status"] for r in ledger.expense_history(self.conn, "flatA", eid)]
        self.assertEqual(statuses, ["superseded", "approved"])

    def test_only_payer_or_admin_can_correct(self):
        eid, _ = self.add("bilal", 900, "dining", ["sara", "bilal", "ali"])
        with self.assertRaises(ledger.NotAllowed):
            ledger.propose_correction(self.conn, "flatA", eid, "ali", amount=100)

    def test_flats_are_isolated(self):
        eid, _ = self.add("sara", 1000, "groceries", ["sara", "bilal"])
        with self.assertRaises(ledger.NotAllowed):
            ledger.get_member(self.conn, "flatB", "sara")
        self.assertIsNone(ledger.get_expense(self.conn, "flatB", eid))
        self.assertEqual(ledger.list_expenses(self.conn, "flatB"), [])
        with self.assertRaises(ledger.NotAllowed):
            ledger.approve_expense(self.conn, "flatB", eid, "hina")

    def test_payment_claim_needs_receiver_confirmation(self):
        self.add("sara", 1800, "groceries", ["sara", "bilal", "ali"])
        sid = ledger.claim_settlement(self.conn, "flatA", "bilal", "sara", balances.to_paisa(600))
        self.assertEqual(balances.net_balances(self.conn, "flatA")["flatA.bilal"], -600_00)
        with self.assertRaises(ledger.NotAllowed):
            ledger.decide_settlement(self.conn, "flatA", sid, "bilal")  # payer cannot confirm
        ledger.decide_settlement(self.conn, "flatA", sid, "sara")
        self.assertEqual(balances.net_balances(self.conn, "flatA")["flatA.bilal"], 0)

    def test_preference_change_needs_approval_and_supersedes(self):
        pid = ledger.propose_preference(self.conn, "flatA", "ali", "include_category", "meat",
                                        "2026-11-01", "ali")
        self.assertNotIn("flatA.ali", ledger.participants_for(self.conn, "flatA", "meat", "2026-11-02"))
        ledger.approve_preference(self.conn, "flatA", pid, "sara")
        self.assertIn("flatA.ali", ledger.participants_for(self.conn, "flatA", "meat", "2026-11-02"))
        old = self.conn.execute("SELECT status FROM preferences WHERE note = 'vegetarian'").fetchone()
        self.assertEqual(old["status"], "superseded")

    def test_duplicate_search_finds_same_amount_nearby(self):
        self.add("sara", 6400, "electricity", ["sara", "bilal", "ali"])
        found = ledger.find_similar(self.conn, "flatA", balances.to_paisa(6400), "electricity")
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
