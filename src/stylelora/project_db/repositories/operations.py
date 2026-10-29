from ..types import OperationRow
from .common import get, paged, row_dto


def get_operation(db, op_id):
    return get(db, "operation_log", op_id, OperationRow)


def latest_applied(db):
    row = db.execute("SELECT * FROM operation_log WHERE status='applied' ORDER BY op_id DESC LIMIT 1").fetchone()
    return row_dto(row, OperationRow) if row else None


def list_operations(db, page):
    return paged(db, "operation_log", OperationRow, page)
