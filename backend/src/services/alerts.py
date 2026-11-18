"""连续差额预警的过账、段生命周期持久化与查询服务。

并发批账在「同一个数据库事务」内完成日账写入与预警重算，二者一起生效或一起回滚；
渠道级行锁串行化同一渠道的并发批次，批次唯一键保证重试不重复派单。
"""
from __future__ import annotations

import time
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select

from src.models import (
    AlertBatch, AlertLedgerEntry, AlertSegment, AlertDispatch, AlertLineage, AlertInbox,
)
from src.services import alert_engine as eng
from src.services.alert_engine import (
    LedgerEntry, HistoricalSegment,
    SEGMENT_OPEN, SEGMENT_MERGED, SEGMENT_CLOSED,
    EVENT_OPENED, EVENT_MERGED, EVENT_CLOSED,
    REASON_SUPERSEDED,
)


# ---------- 输入校验与批次幂等 -------------------------------------------------

def _validate_rows(rows):
    cleaned = []
    for index, row in enumerate(rows):
        try:
            channel = str(row["channel"]).strip()
            day = date.fromisoformat(str(row["day"]))
            revision = int(row["revision"])
            computed = float(row["computed_net"])
            recognized = float(row["recognized_net"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, f"第 {index + 1} 行缺少渠道/账日/修订号或净额字段") from None
        if not channel or revision < 0:
            raise HTTPException(422, f"第 {index + 1} 行渠道为空或修订号非法")
        diff = row.get("diff")
        diff = round(computed - recognized, 4) if diff is None else float(diff)
        cleaned.append(LedgerEntry(
            channel=channel, day=day, revision=revision,
            computed_net=computed, recognized_net=recognized, diff=diff,
            batch_id=row.get("batch_id"),
        ))
    return cleaned


def _lock_channels(db, channels):
    """渠道级行锁：MySQL 下 FOR UPDATE 串行化同渠道并发批次；SQLite 退化为整库写锁。"""
    if not channels:
        return
    try:
        db.execute(select(AlertSegment.id).where(AlertSegment.channel.in_(channels)).with_for_update())
    except Exception:
        # SQLite 等不支持行锁的库在事务层天然串行，忽略即可，唯一约束仍兜底幂等。
        pass


# ---------- 主过账流程 ---------------------------------------------------------

def post_batch(db, data, actor="system"):
    """写入一批已获批日账并重算预警。整方法在调用方事务内，异常即整体回滚。"""
    batch_id = str(data.batch_id).strip()
    if not batch_id:
        raise HTTPException(422, "批次标识 batch_id 不能为空")
    existing = db.scalar(select(AlertBatch).where(AlertBatch.batch_id == batch_id))
    if existing:
        # 重试：直接返回既有批次，绝不重复写日账/派单/通知。
        return {"batch_id": batch_id, "deduplicated": True, "entries": existing.entry_count,
                "new_dispatches": 0, "segments": _summarize(db, batch_id)}

    entries = _validate_rows([e.model_dump() if hasattr(e, "model_dump") else e
                              for e in data.entries])
    if not entries:
        raise HTTPException(422, "批次至少包含一条日账")
    channels = sorted({e.channel for e in entries})
    _lock_channels(db, channels)

    now = time.time()
    db.add(AlertBatch(batch_id=batch_id, source=data.source, entry_count=len(entries),
                      created_by=actor, created_at=now))
    for e in entries:
        db.add(AlertLedgerEntry(
            batch_id=batch_id, channel=e.channel, day=e.day.isoformat(), revision=e.revision,
            computed_net=e.computed_net, recognized_net=e.recognized_net, diff=e.diff,
            created_at=now,
        ))
    db.flush()  # 自然键唯一约束在此对重复 (渠道,账日,修订号) 报错

    result = _reconcile_channels(db, channels, now)
    return {"batch_id": batch_id, "deduplicated": False, "entries": len(entries),
            "new_dispatches": result["new_dispatches"], "segments": result["segments"]}


def _load_entries(db, channels):
    rows = db.scalars(select(AlertLedgerEntry).where(AlertLedgerEntry.channel.in_(channels)))
    return [LedgerEntry(channel=r.channel, day=r.day, revision=r.revision,
                        computed_net=r.computed_net, recognized_net=r.recognized_net,
                        diff=r.diff, batch_id=r.batch_id) for r in rows]


def _historical(db, channels):
    out = []
    for s in db.scalars(select(AlertSegment).where(
            AlertSegment.channel.in_(channels), AlertSegment.status == SEGMENT_OPEN)):
        out.append(HistoricalSegment(segment_id=s.id, channel=s.channel,
                                     start_day=date.fromisoformat(s.start_day),
                                     end_day=date.fromisoformat(s.end_day),
                                     dispatch_key=s.dispatch_key))
    return out


def _reconcile_channels(db, channels, now):
    entries = _load_entries(db, channels)
    historical = _historical(db, channels)
    result = eng.reconcile(entries, historical)

    # 现存段按 id 索引，便于合并/拆分/关闭时改状态留痕。
    open_segments = {s.id: s for s in db.scalars(select(AlertSegment).where(
        AlertSegment.channel.in_(channels)))}
    new_dispatches = 0

    for seg in result.segments:
        if seg.event == EVENT_OPENED:
            new_dispatches += _open_new_segment(db, seg, now)
        elif seg.event == EVENT_CLOSED:
            _close_segment(db, open_segments.get(seg.segment_id), seg, now)
        elif seg.event == EVENT_MERGED and seg.status == SEGMENT_MERGED:
            # 被并入的历史段：仅置 merged 留痕，不再按当前区间改写其边界。
            target = open_segments.get(seg.segment_id)
            if target is not None:
                target.status = SEGMENT_MERGED
                target.updated_at = now
        else:  # KEPT / 合并后的主段 / 拆分后的现存段
            if seg.segment_id is not None:
                _update_standing(db, open_segments.get(seg.segment_id), seg, now)
            else:
                # 拆分/合并派生且未锚定历史 id 的新当前段：新建段但继承历史派单，不发通知。
                _open_inherited_segment(db, seg, now)

    _write_lineage(db, result.events, now)
    return {"new_dispatches": new_dispatches, "segments": len(result.segments)}


def _open_new_segment(db, seg, now):
    key = seg.new_dispatch_key
    # 同渠道+同起止日生成的派单键恒定：若派单已存在，说明是缺口补齐后的同区间重开，
    # 沿用原派单，不再产生第二条派单或收件箱通知（唯一约束亦会兜底）。
    existing_dispatch = db.scalar(select(AlertDispatch).where(AlertDispatch.dispatch_key == key))
    segment = AlertSegment(channel=seg.channel, start_day=seg.start_day.isoformat(),
                           end_day=seg.end_day.isoformat(), status=SEGMENT_OPEN,
                           max_abs_diff=round(seg.max_abs_diff, 4),
                           dispatch_key=key, created_at=now, updated_at=now)
    db.add(segment)
    db.flush()
    if existing_dispatch is not None:
        return 0
    dispatch = AlertDispatch(dispatch_key=key, segment_id=segment.id,
                             channel=seg.channel, start_day=segment.start_day,
                             end_day=segment.end_day, created_at=now)
    db.add(dispatch)
    db.flush()
    title = f"{seg.channel} 连续{len(seg.days)}日净收入差额超阈值预警"
    detail = (f"区间 {segment.start_day} 至 {segment.end_day}，"
              f"最大绝对差额 {seg.max_abs_diff:.2f} 元（阈值严格大于 100 元，连续三天起预警）。")
    db.add(AlertInbox(dispatch_key=key, dispatch_id=dispatch.id,
                      channel=seg.channel, start_day=segment.start_day, end_day=segment.end_day,
                      title=title, detail=detail, created_at=now))
    return 1


def _open_inherited_segment(db, seg, now):
    # 继承派单键（取第一个历史派单），保证页面从原派单仍能查到新现存区间；不写收件箱。
    key = seg.inherited_dispatch_keys[0] if seg.inherited_dispatch_keys else \
        eng.dispatch_key(seg.channel, seg.start_day, seg.end_day)
    segment = AlertSegment(channel=seg.channel, start_day=seg.start_day.isoformat(),
                           end_day=seg.end_day.isoformat(), status=SEGMENT_OPEN,
                           max_abs_diff=round(seg.max_abs_diff, 4), dispatch_key=key,
                           created_at=now, updated_at=now)
    db.add(segment)
    db.flush()
    return segment


def _update_standing(db, segment, seg, now):
    if segment is None:
        return
    segment.start_day = seg.start_day.isoformat()
    segment.end_day = seg.end_day.isoformat()
    segment.max_abs_diff = round(seg.max_abs_diff, 4)
    segment.status = SEGMENT_OPEN
    segment.updated_at = now


def _close_segment(db, segment, seg, now):
    if segment is None:
        return
    segment.status = SEGMENT_CLOSED
    segment.updated_at = now


def _write_lineage(db, events, now):
    for ev in events:
        db.add(AlertLineage(
            segment_id=ev.get("segment_id"), channel=ev["channel"], event=ev["event"],
            start_day=ev["start_day"], end_day=ev["end_day"],
            ancestor_ids=",".join(str(i) for i in ev.get("ancestor_ids", [])),
            inherited_dispatch_keys="|".join(ev.get("inherited_dispatch_keys", [])),
            new_dispatch_key=ev.get("new_dispatch_key"),
            closed_days=",".join(ev.get("closed_days", [])),
            lost_reason=ev.get("lost_reason"), at=now,
        ))


def _summarize(db, batch_id):
    return db.scalar(select(AlertBatch.entry_count).where(AlertBatch.batch_id == batch_id)) or 0


# ---------- 查询：日账、现存段、派单追溯、收件箱 -------------------------------

def ledger_view(db, channel=None):
    """每个账日只返回最高修订号，并标注是否曾被更高修订替代。"""
    stmt = select(AlertLedgerEntry)
    if channel:
        stmt = stmt.where(AlertLedgerEntry.channel == channel)
    rows = list(db.scalars(stmt.order_by(AlertLedgerEntry.channel, AlertLedgerEntry.day,
                                         AlertLedgerEntry.revision)))
    latest: dict[tuple, AlertLedgerEntry] = {}
    revisions: dict[tuple, list[AlertLedgerEntry]] = {}
    for r in rows:
        key = (r.channel, r.day)
        revisions.setdefault(key, []).append(r)
        if key not in latest or r.revision > latest[key].revision:
            latest[key] = r
    out = []
    for key, r in sorted(latest.items()):
        superseded = [x for x in revisions[key] if x.revision < r.revision]
        out.append({
            "channel": r.channel, "day": r.day, "revision": r.revision,
            "computed_net": r.computed_net, "recognized_net": r.recognized_net,
            "diff": r.diff, "over_threshold": abs(r.diff) > eng.THRESHOLD,
            "batch_id": r.batch_id,
            "superseded_revisions": [x.revision for x in superseded],
            "superseded_note": (REASON_SUPERSEDED if superseded else None),
        })
    return out


def segments_view(db, include_closed=False):
    stmt = select(AlertSegment)
    if not include_closed:
        stmt = stmt.where(AlertSegment.status == SEGMENT_OPEN)
    out = []
    for s in db.scalars(stmt.order_by(AlertSegment.channel, AlertSegment.start_day)):
        dispatch = db.scalar(select(AlertDispatch).where(
            AlertDispatch.dispatch_key == s.dispatch_key))
        out.append({
            "id": s.id, "channel": s.channel, "start_day": s.start_day, "end_day": s.end_day,
            "status": s.status, "max_abs_diff": s.max_abs_diff, "dispatch_key": s.dispatch_key,
            "dispatch_id": dispatch.id if dispatch else None,
            "day_count": _span_days(s.start_day, s.end_day),
        })
    return out


def _span_days(start, end):
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def trace_view(db, dispatch_key):
    """从原派单回查：派单信息、当前现存区间、全部谱系事件与已消失账日原因。"""
    dispatch = db.scalar(select(AlertDispatch).where(AlertDispatch.dispatch_key == dispatch_key))
    if not dispatch:
        raise HTTPException(404, "派单不存在")
    lineage = list(db.scalars(select(AlertLineage).where(
        AlertLineage.channel == dispatch.channel).order_by(AlertLineage.id)))
    # 与该派单键相关（自身或继承链中包含它）的事件。
    related = [e for e in lineage if e.new_dispatch_key == dispatch_key
               or dispatch_key in (e.inherited_dispatch_keys or "")]
    current = list(db.scalars(select(AlertSegment).where(
        AlertSegment.channel == dispatch.channel,
        AlertSegment.dispatch_key == dispatch_key).order_by(AlertSegment.start_day)))
    return {
        "dispatch": {
            "id": dispatch.id, "dispatch_key": dispatch.dispatch_key,
            "channel": dispatch.channel, "start_day": dispatch.start_day,
            "end_day": dispatch.end_day, "created_at": dispatch.created_at,
            "original_segment_id": dispatch.segment_id,
        },
        "current_segments": [{
            "id": s.id, "start_day": s.start_day, "end_day": s.end_day,
            "status": s.status, "day_count": _span_days(s.start_day, s.end_day),
        } for s in current],
        "timeline": [{
            "event": e.event, "start_day": e.start_day, "end_day": e.end_day,
            "segment_id": e.segment_id, "ancestor_ids": e.ancestor_ids,
            "closed_days": e.closed_days.split(",") if e.closed_days else [],
            "lost_reason": e.lost_reason, "at": e.at,
        } for e in related],
    }


def inbox_view(db):
    return [{
        "id": m.id, "dispatch_key": m.dispatch_key, "channel": m.channel,
        "start_day": m.start_day, "end_day": m.end_day, "title": m.title, "detail": m.detail,
        "read": m.read, "created_at": m.created_at,
    } for m in db.scalars(select(AlertInbox).order_by(AlertInbox.id.desc()).limit(100))]
