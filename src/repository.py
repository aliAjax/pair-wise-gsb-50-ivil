"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, cents


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS installment_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL UNIQUE REFERENCES records(id) ON DELETE CASCADE,
                    total_amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS installments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES installment_plans(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    due_date TEXT NOT NULL,
                    paid_amount REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open',
                    settled_at TEXT,
                    UNIQUE(plan_id, seq)
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES installment_plans(id) ON DELETE CASCADE,
                    installment_id INTEGER NOT NULL REFERENCES installments(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    amount REAL NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_installments_plan ON installments(plan_id, seq);
                CREATE INDEX IF NOT EXISTS idx_payments_plan ON payments(plan_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def create_plan(self, record_id: int, required_state: str, plan: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if row["state"] != required_state:
                connection.rollback()
                raise Conflict("复核完成后才能冻结应缴账单")
            existing = connection.execute("SELECT id FROM installment_plans WHERE record_id=?", (record_id,)).fetchone()
            if existing is not None:
                connection.rollback()
                raise Conflict("该案件已冻结应缴账单")
            cursor = connection.execute(
                "INSERT INTO installment_plans(record_id,total_amount,status,created_by,created_at) VALUES(?,?,?,?,?)",
                (record_id, plan["total_amount"], "active", actor_id, now),
            )
            plan_id = int(cursor.lastrowid)
            for item in plan["installments"]:
                connection.execute(
                    "INSERT INTO installments(plan_id,seq,amount,due_date,paid_amount,status) VALUES(?,?,?,?,0,'open')",
                    (plan_id, item["seq"], item["amount"], item["due_date"]),
                )
            details = {"summary": "复核金额已冻结为应缴账单", "total_amount": plan["total_amount"], "installments": plan["installments"]}
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "plan_created", actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_plan(record_id)

    def get_plan(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            record = connection.execute("SELECT id FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                raise NotFound("记录不存在")
            plan = connection.execute("SELECT * FROM installment_plans WHERE record_id=?", (record_id,)).fetchone()
            if plan is None:
                return None
            installments = connection.execute("SELECT * FROM installments WHERE plan_id=? ORDER BY seq", (int(plan["id"]),)).fetchall()
            payments = connection.execute(
                "SELECT p.*, i.seq AS installment_seq FROM payments p JOIN installments i ON i.id=p.installment_id "
                "WHERE p.plan_id=? ORDER BY p.id",
                (int(plan["id"]),),
            ).fetchall()
        return self._plan_row(plan, installments, payments)

    @staticmethod
    def _plan_row(plan: sqlite3.Row, installments: List[sqlite3.Row], payments: List[sqlite3.Row]) -> Dict[str, Any]:
        items = []
        for row in installments:
            amount = round(float(row["amount"]), 2)
            paid = round(float(row["paid_amount"]), 2)
            items.append({
                "id": int(row["id"]),
                "seq": int(row["seq"]),
                "amount": amount,
                "due_date": row["due_date"],
                "paid_amount": paid,
                "balance": round(amount - paid, 2),
                "status": row["status"],
                "settled_at": row["settled_at"],
            })
        total = round(float(plan["total_amount"]), 2)
        paid_total = round(sum(item["paid_amount"] for item in items), 2)
        return {
            "id": int(plan["id"]),
            "record_id": int(plan["record_id"]),
            "total_amount": total,
            "paid_total": paid_total,
            "remaining": round(total - paid_total, 2),
            "status": plan["status"],
            "created_by": plan["created_by"],
            "created_at": plan["created_at"],
            "installments": items,
            "payments": [
                {
                    "id": int(row["id"]),
                    "installment_seq": int(row["installment_seq"]),
                    "amount": round(float(row["amount"]), 2),
                    "actor_id": row["actor_id"],
                    "created_at": row["created_at"],
                }
                for row in payments
            ],
        }

    def apply_payment(self, record_id: int, allocation: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            plan = connection.execute("SELECT * FROM installment_plans WHERE id=?", (allocation["plan_id"],)).fetchone()
            if plan is None or int(plan["record_id"]) != int(record_id):
                connection.rollback()
                raise NotFound("分期计划不存在")
            if plan["status"] != "active":
                connection.rollback()
                raise Conflict("账单已结清，多出金额拒绝入账")
            installment = connection.execute("SELECT * FROM installments WHERE id=?", (allocation["installment_id"],)).fetchone()
            if installment is None or installment["status"] != "open" or cents(installment["paid_amount"]) != cents(allocation["expected_paid"]):
                connection.rollback()
                raise Conflict("付款冲突，请刷新后重试")
            installment_status = "settled" if allocation["installment_settled"] else "open"
            settled_at = now if allocation["installment_settled"] else None
            connection.execute(
                "UPDATE installments SET paid_amount=?,status=?,settled_at=? WHERE id=?",
                (allocation["new_paid"], installment_status, settled_at, allocation["installment_id"]),
            )
            connection.execute(
                "INSERT INTO payments(plan_id,installment_id,record_id,amount,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                (allocation["plan_id"], allocation["installment_id"], record_id, allocation["amount"], actor_id, now),
            )
            plan_status = "active"
            if allocation["plan_settled"]:
                plan_status = "settled"
                connection.execute("UPDATE installment_plans SET status='settled' WHERE id=?", (allocation["plan_id"],))
            details = {
                "summary": "收到第%s期缴款%s" % (allocation["seq"], allocation["amount"]),
                "amount": allocation["amount"],
                "installment_seq": allocation["seq"],
                "installment_status": installment_status,
                "balance_after": allocation["balance_after"],
                "plan_status": plan_status,
            }
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "payment_received", actor_id, int(record["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_plan(record_id)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
