from sqlalchemy import String, Double, Integer, Boolean, ForeignKey, UniqueConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class AlertBatch(Base):
    """一次并发批账的幂等单元。同 batch_id 重复提交直接忽略，重试不重复派单。"""
    __tablename__ = 'alert_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(80), unique=True)
    source: Mapped[str] = mapped_column(String(120), default='manual')
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(150), default='system')
    created_at: Mapped[float] = mapped_column(Double)


class AlertLedgerEntry(Base):
    """已获批的渠道日账。同一账日可有多修订号，查询与计算始终取最高修订号。"""
    __tablename__ = 'alert_ledger_entries'
    __table_args__ = (
        UniqueConstraint('channel', 'day', 'revision', name='uq_alert_natural_revision'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(80), index=True)
    channel: Mapped[str] = mapped_column(String(120), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)          # ISO 日期
    revision: Mapped[int] = mapped_column(Integer)
    computed_net: Mapped[float] = mapped_column(Double)
    recognized_net: Mapped[float] = mapped_column(Double)
    diff: Mapped[float] = mapped_column(Double)
    created_at: Mapped[float] = mapped_column(Double)


class AlertSegment(Base):
    """一个连续三天以上超阈值的预警段（当前态）。合并/拆分后旧段置非 open 留痕。"""
    __tablename__ = 'alert_segments'
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(120), index=True)
    start_day: Mapped[str] = mapped_column(String(10))
    end_day: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default='open', index=True)
    max_abs_diff: Mapped[float] = mapped_column(Double, default=0.0)
    dispatch_key: Mapped[str] = mapped_column(String(200), default='')
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class AlertDispatch(Base):
    """派单。dispatch_key 唯一化保证同一段（含合并/拆分沿用）绝不重复派单。"""
    __tablename__ = 'alert_dispatches'
    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey('alert_segments.id'))
    channel: Mapped[str] = mapped_column(String(120), index=True)
    start_day: Mapped[str] = mapped_column(String(10))
    end_day: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default='dispatched')
    created_at: Mapped[float] = mapped_column(Double)


class AlertLineage(Base):
    """段谱系：打开/沿用/合并/拆分/关闭，及消失账日与原因，供原派单回查。"""
    __tablename__ = 'alert_lineage'
    id: Mapped[int] = mapped_column(primary_key=True)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey('alert_segments.id'), nullable=True)
    channel: Mapped[str] = mapped_column(String(120), index=True)
    event: Mapped[str] = mapped_column(String(20))
    start_day: Mapped[str] = mapped_column(String(10))
    end_day: Mapped[str] = mapped_column(String(10))
    ancestor_ids: Mapped[str] = mapped_column(Text, default='')        # 逗号分隔
    inherited_dispatch_keys: Mapped[str] = mapped_column(Text, default='')
    new_dispatch_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    closed_days: Mapped[str] = mapped_column(Text, default='')         # 逗号分隔 ISO 日
    lost_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    at: Mapped[float] = mapped_column(Double)


class AlertInbox(Base):
    """本地收件箱通知。只有全新段（无关联派单）才写入；重试不重复。"""
    __tablename__ = 'alert_inbox'
    __table_args__ = (
        UniqueConstraint('dispatch_key', name='uq_alert_inbox_dispatch'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_key: Mapped[str] = mapped_column(String(200), index=True)
    dispatch_id: Mapped[int] = mapped_column(ForeignKey('alert_dispatches.id'))
    channel: Mapped[str] = mapped_column(String(120))
    start_day: Mapped[str] = mapped_column(String(10))
    end_day: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default='')
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Double)
