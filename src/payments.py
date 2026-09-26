"""复核账单分期计划与付款冲销规则。"""
from datetime import datetime
from typing import Any, Dict, List

from .domain import Conflict, ValidationError, number


SCHEDULE_ROLES = {'reviewer'}
PAY_ROLES = {'taxpayer_rep'}
SCHEDULE_STATES = {'reviewed'}
PAY_STATES = {'reviewed', 'appealed'}
MAX_INSTALLMENTS = 60
CENT = 0.005


def _round2(value: float) -> float:
    return round(float(value), 2)


class PaymentRules:
    """复核金额冻结为应缴账单：各期日期依次靠后、合计对准账单；付款先冲最早未结清的一期。"""

    def can_schedule(self, role: str) -> bool:
        return role == "admin" or role in SCHEDULE_ROLES

    def can_pay(self, role: str) -> bool:
        return role == "admin" or role in PAY_ROLES

    def require_schedule_state(self, record: Dict[str, Any]) -> None:
        if record["state"] not in SCHEDULE_STATES:
            raise Conflict("当前状态不允许制定缴款计划")

    def require_pay_state(self, record: Dict[str, Any]) -> None:
        if record["state"] not in PAY_STATES:
            raise Conflict("当前状态不允许登记付款")

    @staticmethod
    def _due_date(value: Any, seq: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("第%s期到期日不能为空" % seq)
        value = value.strip()
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationError("第%s期到期日必须是YYYY-MM-DD格式" % seq) from exc
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValidationError("第%s期到期日必须是YYYY-MM-DD格式" % seq)
        return value

    def validate_installments(self, bill_amount: float, raw: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("installments必须是非空列表")
        if len(raw) > MAX_INSTALLMENTS:
            raise ValidationError("installments不能超过%s期" % MAX_INSTALLMENTS)
        bill = _round2(bill_amount)
        items: List[Dict[str, Any]] = []
        total = 0.0
        previous = ""
        for seq, entry in enumerate(raw, start=1):
            if not isinstance(entry, dict):
                raise ValidationError("第%s期必须是对象" % seq)
            amount = _round2(number(entry, "amount"))
            if amount <= 0:
                raise ValidationError("第%s期金额必须大于0" % seq)
            due_date = self._due_date(entry.get("due_date"), seq)
            if previous and due_date <= previous:
                raise ValidationError("各期到期日必须依次靠后")
            previous = due_date
            total = _round2(total + amount)
            items.append({"seq": seq, "amount": amount, "due_date": due_date})
        if abs(total - bill) >= CENT:
            raise ValidationError("各期金额合计%s与应缴账单%s不一致" % (total, bill))
        return items

    def apply_payment(self, plan: Dict[str, Any], installments: List[Dict[str, Any]], amount: float) -> Dict[str, Any]:
        """付款冲抵最早未结清的一期；结清后固定，超出该期余额的付款整笔拒绝入账。"""
        target = None
        for item in installments:
            if item["status"] != "settled":
                target = item
                break
        if plan.get("status") == "settled" or target is None:
            raise Conflict("账单已全部结清，付款拒绝入账")
        amount = _round2(amount)
        balance = _round2(float(target["amount"]) - float(target["paid_amount"]))
        if amount - balance >= CENT:
            raise ValidationError("付款金额%s超过第%s期未结余额%s，多出金额拒绝入账" % (amount, target["seq"], balance))
        settled = balance - amount < CENT
        paid_amount = _round2(target["amount"]) if settled else _round2(float(target["paid_amount"]) + amount)
        status = "settled" if settled else "partial"
        plan_status = "settled" if settled and all(item["status"] == "settled" for item in installments if item["id"] != target["id"]) else "active"
        return {
            "installment_id": target["id"],
            "seq": target["seq"],
            "paid_amount": paid_amount,
            "status": status,
            "plan_status": plan_status,
            "summary": "第%s期入账%s，%s" % (target["seq"], amount, "本期已结清" if settled else "本期部分结清"),
        }
