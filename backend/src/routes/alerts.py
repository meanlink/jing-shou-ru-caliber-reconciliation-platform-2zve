from fastapi import APIRouter, Depends
from sqlalchemy import select
from src.config.database import get_db
from src.middlewares.auth import current_user, admin_only
from src.models import AlertInbox
from src.schemas.alert import AlertBatchInput
from src.services import alerts
from src.services.audit import record

router = APIRouter(prefix='/alerts')


@router.post('/batches', status_code=201)
def post_batch(data: AlertBatchInput, db=Depends(get_db), u=Depends(admin_only)):
    """并发批账：日账与预警在同一事务内一起生效；重复 batch_id 幂等去重。"""
    result = alerts.post_batch(db, data, u.email)
    record(db, u.email, 'alert.batch', {'batch_id': data.batch_id,
                                        'entries': len(data.entries),
                                        'new_dispatches': result['new_dispatches']})
    db.commit()
    return result


@router.get('/ledger')
def ledger(channel: str | None = None, db=Depends(get_db), u=Depends(current_user)):
    return alerts.ledger_view(db, channel)


@router.get('/segments')
def segments(include_closed: bool = False, db=Depends(get_db), u=Depends(current_user)):
    return alerts.segments_view(db, include_closed)


@router.get('/dispatches/trace')
def trace(dispatch_key: str, db=Depends(get_db), u=Depends(current_user)):
    return alerts.trace_view(db, dispatch_key)


@router.get('/inbox')
def inbox(db=Depends(get_db), u=Depends(current_user)):
    return alerts.inbox_view(db)


@router.post('/inbox/{inbox_id}/read')
def mark_read(inbox_id: int, db=Depends(get_db), u=Depends(current_user)):
    message = db.get(AlertInbox, inbox_id)
    if message:
        message.read = True
        record(db, u.email, 'alert.inbox.read', {'id': inbox_id})
        db.commit()
    return {'id': inbox_id, 'read': bool(message and message.read)}
