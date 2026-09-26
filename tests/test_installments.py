import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


CREATE_DATA = {'taxpayer': 'Star Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60}
TO_REVIEWED = [('investigate', 'inspector', {'plan': '核对账簿'}), ('propose', 'inspector', {'proposal': '补税并处罚'}), ('review', 'reviewer', {'outcome': 'accepted', 'review_note': '证据充分'})]
TOTAL_DUE = 323700.0
PLAN_DATA = {'installments': [{'amount': 100000.0, 'due_date': '2026-10-01'}, {'amount': 100000.0, 'due_date': '2026-11-01'}, {'amount': 123700.0, 'due_date': '2026-12-01'}]}
REVIEWER = Actor('reviewer1', 'reviewer')
TAXPAYER = Actor('taxpayer1', 'taxpayer_rep')


class InstallmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _reviewed(self, reference="TAX-26001"):
        record = self.service.create(Actor("creator", "inspector"), reference, CREATE_DATA)
        for action, role, data in TO_REVIEWED:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        return record

    def _plan(self, record, data=None):
        return self.service.create_plan(REVIEWER, record["id"], data if data is not None else PLAN_DATA)

    def test_freeze_plan_and_detail(self):
        record = self._reviewed()
        self.assertIsNone(self.service.get_record(REVIEWER, record["id"])["installment_plan"])
        plan = self._plan(record)
        self.assertEqual(plan["total_amount"], TOTAL_DUE)
        self.assertEqual(plan["status"], "active")
        self.assertEqual(plan["remaining"], TOTAL_DUE)
        self.assertEqual([i["seq"] for i in plan["installments"]], [1, 2, 3])
        for item, expect in zip(plan["installments"], [100000.0, 100000.0, 123700.0]):
            self.assertEqual(item["amount"], expect)
            self.assertEqual(item["balance"], expect)
            self.assertEqual(item["status"], "open")
        detail = self.service.get_record(REVIEWER, record["id"])
        self.assertEqual(detail["installment_plan"]["total_amount"], TOTAL_DUE)
        timeline = self.service.timeline(REVIEWER, record["id"])
        self.assertEqual(timeline[-1]["action"], "plan_created")

    def test_plan_sum_must_match_bill(self):
        record = self._reviewed()
        bad = {'installments': [{'amount': 100000.0, 'due_date': '2026-10-01'}, {'amount': 100000.0, 'due_date': '2026-11-01'}]}
        with self.assertRaises(ValidationError):
            self._plan(record, bad)
        over = {'installments': [{'amount': TOTAL_DUE + 0.01, 'due_date': '2026-10-01'}]}
        with self.assertRaises(ValidationError):
            self._plan(record, over)

    def test_plan_dates_must_increase(self):
        record = self._reviewed()
        same_day = {'installments': [{'amount': 100000.0, 'due_date': '2026-10-01'}, {'amount': 223700.0, 'due_date': '2026-10-01'}]}
        with self.assertRaises(ValidationError):
            self._plan(record, same_day)
        backwards = {'installments': [{'amount': 100000.0, 'due_date': '2026-12-01'}, {'amount': 223700.0, 'due_date': '2026-10-01'}]}
        with self.assertRaises(ValidationError):
            self._plan(record, backwards)

    def test_plan_rejects_bad_input(self):
        record = self._reviewed()
        with self.assertRaises(ValidationError):
            self._plan(record, {'installments': []})
        with self.assertRaises(ValidationError):
            self._plan(record, {'installments': [{'amount': 0, 'due_date': '2026-10-01'}, {'amount': TOTAL_DUE, 'due_date': '2026-11-01'}]})
        with self.assertRaises(ValidationError):
            self._plan(record, {'installments': [{'amount': TOTAL_DUE, 'due_date': '2026-13-01'}]})
        with self.assertRaises(ValidationError):
            self._plan(record, {'installments': [{'amount': TOTAL_DUE, 'due_date': '2026-10-01'}, {'amount': 1, 'due_date': '2026-10-01'}]})

    def test_plan_requires_reviewed_state(self):
        record = self.service.create(Actor("creator", "inspector"), "TAX-26002", CREATE_DATA)
        with self.assertRaises(Conflict):
            self._plan(record)
        record = self.service.act(Actor("operator", "inspector"), record["id"], record["version"], 'investigate', {'plan': '核对账簿'})
        record = self.service.act(Actor("operator", "inspector"), record["id"], record["version"], 'propose', {'proposal': '补税并处罚'})
        with self.assertRaises(Conflict):
            self._plan(record)

    def test_plan_role_and_duplicate(self):
        record = self._reviewed()
        with self.assertRaises(PermissionDenied):
            self.service.create_plan(Actor("inspector1", "inspector"), record["id"], PLAN_DATA)
        with self.assertRaises(PermissionDenied):
            self.service.create_plan(TAXPAYER, record["id"], PLAN_DATA)
        self._plan(record)
        with self.assertRaises(Conflict):
            self._plan(record)

    def test_payment_allocates_earliest_first_and_locks_settled(self):
        record = self._reviewed()
        self._plan(record)
        plan = self.service.pay(TAXPAYER, record["id"], {'amount': 40000.0})
        first, second, third = plan["installments"]
        self.assertEqual((first["paid_amount"], first["balance"], first["status"]), (40000.0, 60000.0, "open"))
        self.assertEqual((second["paid_amount"], third["paid_amount"]), (0.0, 0.0))
        plan = self.service.pay(TAXPAYER, record["id"], {'amount': 60000.0})
        first = plan["installments"][0]
        self.assertEqual((first["paid_amount"], first["balance"], first["status"]), (100000.0, 0.0, "settled"))
        self.assertIsNotNone(first["settled_at"])
        plan = self.service.pay(TAXPAYER, record["id"], {'amount': 50000.0})
        first, second = plan["installments"][0], plan["installments"][1]
        self.assertEqual((first["paid_amount"], first["status"]), (100000.0, "settled"))
        self.assertEqual((second["paid_amount"], second["balance"], second["status"]), (50000.0, 50000.0, "open"))
        self.assertEqual(plan["paid_total"], 150000.0)
        self.assertEqual(len(plan["payments"]), 3)
        timeline = self.service.timeline(REVIEWER, record["id"])
        self.assertEqual(timeline[-1]["action"], "payment_received")

    def test_overpayment_rejected(self):
        record = self._reviewed()
        self._plan(record)
        with self.assertRaises(ValidationError):
            self.service.pay(TAXPAYER, record["id"], {'amount': 100000.01})
        plan = self.service.get_plan(REVIEWER, record["id"])
        self.assertEqual(plan["paid_total"], 0.0)
        with self.assertRaises(ValidationError):
            self.service.pay(TAXPAYER, record["id"], {'amount': 0})
        for amount in (100000.0, 100000.0, 123700.0):
            plan = self.service.pay(TAXPAYER, record["id"], {'amount': amount})
        self.assertEqual(plan["status"], "settled")
        self.assertEqual(plan["remaining"], 0.0)
        self.assertTrue(all(item["status"] == "settled" for item in plan["installments"]))
        with self.assertRaises(Conflict):
            self.service.pay(TAXPAYER, record["id"], {'amount': 0.01})

    def test_payment_requires_plan_and_role(self):
        record = self._reviewed()
        with self.assertRaises(NotFound):
            self.service.pay(TAXPAYER, record["id"], {'amount': 1.0})
        self._plan(record)
        with self.assertRaises(PermissionDenied):
            self.service.pay(Actor("inspector1", "inspector"), record["id"], {'amount': 1.0})

    def test_plan_survives_restart(self):
        record = self._reviewed()
        self._plan(record)
        self.service.pay(TAXPAYER, record["id"], {'amount': 40000.0})
        restarted = build_service(self.db)
        detail = restarted.get_record(REVIEWER, record["id"])
        plan = detail["installment_plan"]
        self.assertEqual(plan["total_amount"], TOTAL_DUE)
        self.assertEqual(plan["paid_total"], 40000.0)
        first = plan["installments"][0]
        self.assertEqual((first["balance"], first["status"]), (60000.0, "open"))
        self.assertEqual(len(plan["payments"]), 1)
        timeline = restarted.timeline(REVIEWER, record["id"])
        self.assertEqual([e["action"] for e in timeline[-2:]], ["plan_created", "payment_received"])


if __name__ == "__main__":
    unittest.main()
