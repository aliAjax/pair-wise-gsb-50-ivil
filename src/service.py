"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, number, text
from .payments import PaymentRules
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, payment_rules: PaymentRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.payment_rules = payment_rules or PaymentRules()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._with_plan(self.repository.get(record_id))

    def _with_plan(self, record: Dict[str, Any]) -> Dict[str, Any]:
        plan = self.repository.get_plan(record["id"])
        if plan is not None:
            record["payment_plan"] = plan
        return record

    @staticmethod
    def _data(data: Any) -> Dict[str, Any]:
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValidationError("data必须是对象")
        return data

    def schedule(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.payment_rules.can_schedule(actor.role):
            raise PermissionDenied("角色无权制定缴款计划")
        record = self.repository.get(record_id)
        self.payment_rules.require_schedule_state(record)
        bill = round(float(record["payload"].get("total_due", 0.0)), 2)
        if bill <= 0:
            raise ValidationError("应缴账单金额为零，无需制定缴款计划")
        installments = self.payment_rules.validate_installments(bill, self._data(data).get("installments"))
        record = self.repository.create_plan(
            record_id=record_id,
            expected_version=int(expected_version),
            bill_amount=bill,
            installments=installments,
            actor_id=actor.user_id,
            details={"summary": "复核金额已冻结为应缴账单并制定分期计划", "bill_amount": bill, "installments": installments},
        )
        return self._with_plan(record)

    def pay(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.payment_rules.can_pay(actor.role):
            raise PermissionDenied("角色无权登记付款")
        record = self.repository.get(record_id)
        self.payment_rules.require_pay_state(record)
        amount = round(number(self._data(data), "amount"), 2)
        if amount <= 0:
            raise ValidationError("amount必须大于0")
        record, payment = self.repository.apply_payment(
            record_id=record_id,
            expected_version=int(expected_version),
            amount=amount,
            actor_id=actor.user_id,
            apply_fn=self.payment_rules.apply_payment,
        )
        result = self._with_plan(record)
        result["payment"] = payment
        return result

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
