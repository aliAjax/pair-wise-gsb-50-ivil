"""税务稽查案件与复议流程领域规则与状态转换。"""
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, cents, choice, integer, number, text, text_list


INITIAL_STATE = "opened"
CREATE_ROLES = {'inspector'}
ACTION_ROLES = {'investigate': {'inspector'}, 'propose': {'inspector'}, 'review': {'reviewer'}, 'appeal': {'taxpayer_rep'}, 'close': {'reviewer'}}
TRANSITIONS = {'investigate': {'opened': 'investigating'}, 'propose': {'investigating': 'proposed'}, 'review': {'proposed': 'reviewed'}, 'appeal': {'reviewed': 'appealed'}, 'close': {'reviewed': 'closed', 'appealed': 'closed'}}
PLAN_STATE = "reviewed"
PLAN_ROLES = {'reviewer'}
PAYMENT_ROLES = {'taxpayer_rep'}
MAX_INSTALLMENTS = 60


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    PLAN_STATE = PLAN_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | PLAN_ROLES | PAYMENT_ROLES
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_plan(self, role: str) -> bool:
        return role == "admin" or role in PLAN_ROLES

    def role_can_pay(self, role: str) -> bool:
        return role == "admin" or role in PAYMENT_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "taxpayer")
        text(p, "tax_period")
        number(p, "declared_tax", 0)
        number(p, "assessed_tax", 0)
        number(p, "penalty_rate", 0, 1)
        integer(p, "evidence_count", 0)
        integer(p, "days_late", 0)
        integer(p, "appeal_deadline_day", 1)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        difference = max(0.0, float(p["assessed_tax"]) - float(p["declared_tax"]))
        interest = difference * 0.0005 * int(p["days_late"])
        penalty = difference * float(p["penalty_rate"])
        p["tax_difference"] = round(difference, 2)
        p["interest"] = round(interest, 2)
        p["penalty"] = round(penalty, 2)
        p["total_due"] = round(difference + interest + penalty, 2)
        p["refund_due"] = round(max(0.0, float(p["declared_tax"]) - float(p["assessed_tax"])), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed"} and item["payload"].get("taxpayer") == payload.get("taxpayer") and item["payload"].get("tax_period") == payload.get("tax_period"):
                raise Conflict("同一纳税人同一税期已有未结稽查案件")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "investigate":
            changes["investigation_plan"] = text(data, "plan")
            summary = "进入稽查调查"
        elif action == "propose":
            if int(p["evidence_count"]) <= 0:
                raise ValidationError("没有证据不能提出处理建议")
            changes["proposal"] = text(data, "proposal")
            changes["proposed_amount"] = float(p["total_due"])
            summary = "已提出补税和处罚建议"
        elif action == "review":
            outcome = choice(data, "outcome", ["accepted", "reduced", "remanded"])
            changes["review_outcome"] = outcome
            changes["review_note"] = text(data, "review_note")
            if outcome == "reduced":
                changes["total_due"] = round(float(p["total_due"]) * float(data.get("reduction_pct", 0.5)), 2)
            summary = "复核完成"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["appeal_deadline_day"]):
                raise ValidationError("复议申请超过期限")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "复议申请已受理"
        elif action == "close":
            changes["final_decision"] = text(data, "final_decision")
            summary = "案件已结案"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def prepare_plan(self, record: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """复核金额冻结为应缴账单：一次提交各期，日期依次靠后，合计对准账单。"""
        if record["state"] != PLAN_STATE:
            raise Conflict("复核完成后才能冻结应缴账单")
        total = round(float(record["payload"].get("total_due", 0.0)), 2)
        if cents(total) <= 0:
            raise ValidationError("复核应缴金额为零，无需冻结账单")
        raw = (data or {}).get("installments")
        if not isinstance(raw, list) or not raw:
            raise ValidationError("installments至少需要一期")
        if len(raw) > MAX_INSTALLMENTS:
            raise ValidationError("installments不能超过%s期" % MAX_INSTALLMENTS)
        installments: List[Dict[str, Any]] = []
        previous_due = None
        sum_cents = 0
        for seq, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                raise ValidationError("第%s期必须是对象" % seq)
            amount = round(number(item, "amount"), 2)
            if cents(amount) <= 0:
                raise ValidationError("第%s期金额必须大于0" % seq)
            due = self._due_date(item.get("due_date"), seq)
            if previous_due is not None and due <= previous_due:
                raise ValidationError("各期到期日必须依次靠后")
            previous_due = due
            sum_cents += cents(amount)
            installments.append({"seq": seq, "amount": amount, "due_date": due.isoformat()})
        if sum_cents != cents(total):
            raise ValidationError("各期金额合计%s与应缴账单%s不一致" % (sum_cents / 100, total))
        return {"total_amount": total, "installments": installments}

    @staticmethod
    def _due_date(value: Any, seq: int) -> date:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("第%s期due_date不能为空" % seq)
        value = value.strip()
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValidationError("第%s期due_date必须是YYYY-MM-DD格式" % seq) from exc
        if parsed.isoformat() != value:
            raise ValidationError("第%s期due_date必须是YYYY-MM-DD格式" % seq)
        return parsed

    def apply_payment(self, plan: Dict[str, Any], amount: Any) -> Dict[str, Any]:
        """缴款先冲最早未结清的一期，结清后固定，多出金额拒绝入账。"""
        if plan["status"] != "active":
            raise Conflict("账单已结清，多出金额拒绝入账")
        value = round(number({"amount": amount}, "amount"), 2)
        if cents(value) <= 0:
            raise ValidationError("付款金额必须大于0")
        target = None
        for item in plan["installments"]:
            if item["status"] != "settled":
                target = item
                break
        if target is None:
            raise Conflict("账单已结清，多出金额拒绝入账")
        remaining_cents = cents(target["amount"]) - cents(target["paid_amount"])
        if cents(value) > remaining_cents:
            raise ValidationError("付款金额%s超出第%s期余额%s，多出金额拒绝入账" % (value, target["seq"], remaining_cents / 100))
        new_paid_cents = cents(target["paid_amount"]) + cents(value)
        settled = new_paid_cents == cents(target["amount"])
        plan_settled = settled and all(item["status"] == "settled" for item in plan["installments"] if item["id"] != target["id"])
        return {
            "plan_id": plan["id"],
            "installment_id": target["id"],
            "seq": target["seq"],
            "amount": value,
            "expected_paid": round(float(target["paid_amount"]), 2),
            "new_paid": new_paid_cents / 100,
            "installment_settled": settled,
            "plan_settled": plan_settled,
            "balance_after": (cents(target["amount"]) - new_paid_cents) / 100,
        }
