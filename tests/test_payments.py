import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


CREATE_DATA = {'taxpayer': 'Star Ltd', 'tax_period': '2025-Q4', 'declared_tax': 500000.0, 'assessed_tax': 760000.0, 'penalty_rate': 0.2, 'evidence_count': 4, 'days_late': 90, 'appeal_deadline_day': 60}
TOTAL_DUE = 323700.0
INSTALLMENTS = [{'amount': 100000.0, 'due_date': '2026-10-01'}, {'amount': 123700.0, 'due_date': '2026-11-01'}, {'amount': 100000.0, 'due_date': '2026-12-01'}]


class PaymentTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def reviewed_record(self):
        record = self.service.create(Actor("creator", "inspector"), "TAX-26001", CREATE_DATA)
        record = self.service.act(Actor("operator", "inspector"), record["id"], record["version"], "investigate", {"plan": "核对账簿"})
        record = self.service.act(Actor("operator", "inspector"), record["id"], record["version"], "propose", {"proposal": "补税并处罚"})
        return self.service.act(Actor("operator", "reviewer"), record["id"], record["version"], "review", {"outcome": "accepted", "review_note": "证据充分"})

    def scheduled_record(self):
        record = self.reviewed_record()
        return self.service.schedule(Actor("reviewer", "reviewer"), record["id"], record["version"], {"installments": INSTALLMENTS})

    def pay(self, record, amount, role="taxpayer_rep"):
        return self.service.pay(Actor("payer", role), record["id"], record["version"], {"amount": amount})


class ScheduleTest(PaymentTestBase):
    def test_schedule_freezes_bill_and_shows_in_detail(self):
        record = self.scheduled_record()
        plan = record["payment_plan"]
        self.assertEqual(plan["bill_amount"], TOTAL_DUE)
        self.assertEqual(plan["total_amount"], TOTAL_DUE)
        self.assertEqual(plan["remaining_amount"], TOTAL_DUE)
        self.assertEqual(plan["status"], "active")
        self.assertEqual([item["seq"] for item in plan["installments"]], [1, 2, 3])
        for item, expected in zip(plan["installments"], INSTALLMENTS):
            self.assertEqual(item["balance"], expected["amount"])
            self.assertEqual(item["status"], "pending")
            self.assertEqual(item["due_date"], expected["due_date"])
        detail = self.service.get_record(Actor("creator", "inspector"), record["id"])
        self.assertEqual(detail["payment_plan"]["total_amount"], TOTAL_DUE)
        timeline = self.service.timeline(Actor("creator", "inspector"), record["id"])
        self.assertEqual(timeline[-1]["action"], "schedule")

    def test_schedule_requires_reviewer_and_reviewed_state(self):
        record = self.reviewed_record()
        with self.assertRaises(PermissionDenied):
            self.service.schedule(Actor("operator", "inspector"), record["id"], record["version"], {"installments": INSTALLMENTS})
        with self.assertRaises(PermissionDenied):
            self.service.schedule(Actor("rep", "taxpayer_rep"), record["id"], record["version"], {"installments": INSTALLMENTS})
        fresh = self.service.create(Actor("creator", "inspector"), "TAX-26002", dict(CREATE_DATA, tax_period="2026-Q1"))
        with self.assertRaises(Conflict):
            self.service.schedule(Actor("reviewer", "reviewer"), fresh["id"], fresh["version"], {"installments": INSTALLMENTS})

    def test_schedule_rejects_bad_installments(self):
        record = self.reviewed_record()
        cases = [
            [],
            [{'amount': 1.0, 'due_date': '2026-10-01'}],
            [{'amount': 200000.0, 'due_date': '2026-10-01'}, {'amount': 123700.0, 'due_date': '2026-10-01'}],
            [{'amount': 200000.0, 'due_date': '2026-12-01'}, {'amount': 123700.0, 'due_date': '2026-10-01'}],
            [{'amount': 0.0, 'due_date': '2026-10-01'}, {'amount': 323700.0, 'due_date': '2026-11-01'}],
            [{'amount': 323700.0, 'due_date': '2026-13-01'}],
            [{'amount': 323700.0, 'due_date': '2026-10-1'}],
        ]
        for installments in cases:
            with self.assertRaises(ValidationError):
                self.service.schedule(Actor("reviewer", "reviewer"), record["id"], record["version"], {"installments": installments})
        self.assertIsNone(self.service.get_record(Actor("creator", "inspector"), record["id"]).get("payment_plan"))

    def test_duplicate_schedule_rejected(self):
        record = self.scheduled_record()
        with self.assertRaises(Conflict):
            self.service.schedule(Actor("reviewer", "reviewer"), record["id"], record["version"], {"installments": INSTALLMENTS})


class PaymentTest(PaymentTestBase):
    def test_payments_offset_earliest_open_installment(self):
        record = self.scheduled_record()
        record = self.pay(record, 40000.0)
        items = record["payment_plan"]["installments"]
        self.assertEqual((items[0]["paid_amount"], items[0]["balance"], items[0]["status"]), (40000.0, 60000.0, "partial"))
        self.assertEqual((items[1]["paid_amount"], items[1]["status"]), (0.0, "pending"))
        record = self.pay(record, 60000.0)
        items = record["payment_plan"]["installments"]
        self.assertEqual((items[0]["balance"], items[0]["status"]), (0.0, "settled"))
        record = self.pay(record, 100000.0)
        items = record["payment_plan"]["installments"]
        self.assertEqual((items[1]["paid_amount"], items[1]["balance"], items[1]["status"]), (100000.0, 23700.0, "partial"))
        self.assertEqual(items[2]["status"], "pending")

    def test_excess_payment_rejected_and_nothing_changes(self):
        record = self.scheduled_record()
        with self.assertRaises(ValidationError):
            self.pay(record, 100000.01)
        plan = self.service.get_record(Actor("creator", "inspector"), record["id"])["payment_plan"]
        self.assertTrue(all(item["paid_amount"] == 0.0 for item in plan["installments"]))
        record = self.pay(record, 100000.0)
        with self.assertRaises(ValidationError):
            self.pay(record, 123700.01)
        plan = self.service.get_record(Actor("creator", "inspector"), record["id"])["payment_plan"]
        self.assertEqual(plan["installments"][1]["paid_amount"], 0.0)

    def test_full_settlement_fixes_plan(self):
        record = self.scheduled_record()
        for amount in (100000.0, 123700.0, 100000.0):
            record = self.pay(record, amount)
        plan = record["payment_plan"]
        self.assertEqual(plan["status"], "settled")
        self.assertEqual(plan["remaining_amount"], 0.0)
        self.assertTrue(all(item["status"] == "settled" for item in plan["installments"]))
        with self.assertRaises(Conflict):
            self.pay(record, 0.01)

    def test_pay_permission_state_and_version(self):
        record = self.scheduled_record()
        with self.assertRaises(PermissionDenied):
            self.pay(record, 1.0, role="inspector")
        with self.assertRaises(Conflict):
            self.service.pay(Actor("payer", "taxpayer_rep"), record["id"], record["version"] - 1, {"amount": 1.0})
        record = self.service.act(Actor("operator", "reviewer"), record["id"], record["version"], "close", {"final_decision": "维持处理"})
        with self.assertRaises(Conflict):
            self.pay(record, 1.0)

    def test_pay_without_plan_is_not_found(self):
        record = self.reviewed_record()
        with self.assertRaises(NotFound):
            self.pay(record, 1.0)

    def test_payment_allowed_during_appeal(self):
        record = self.scheduled_record()
        record = self.service.act(Actor("rep", "taxpayer_rep"), record["id"], record["version"], "appeal", {"appeal_day": 10, "appeal_reason": "现金流紧张"})
        record = self.pay(record, 100000.0)
        self.assertEqual(record["payment_plan"]["installments"][0]["status"], "settled")

    def test_plan_survives_service_restart(self):
        record = self.scheduled_record()
        record = self.pay(record, 100000.0)
        restarted = build_service(self.db_path)
        detail = restarted.get_record(Actor("creator", "inspector"), record["id"])
        plan = detail["payment_plan"]
        self.assertEqual(plan["total_amount"], TOTAL_DUE)
        self.assertEqual(plan["installments"][0]["status"], "settled")
        self.assertEqual(plan["installments"][0]["balance"], 0.0)
        self.assertEqual(plan["remaining_amount"], TOTAL_DUE - 100000.0)
        timeline = restarted.timeline(Actor("creator", "inspector"), record["id"])
        self.assertEqual([event["action"] for event in timeline[-2:]], ["schedule", "pay"])
