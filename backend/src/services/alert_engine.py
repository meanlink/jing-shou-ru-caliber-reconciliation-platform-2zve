"""连续净收入差额预警的纯标准库领域引擎。

规则（与 BUSINESS.md「连续差额预警」一致）：

1. 按渠道汇集「已获批」日账；每个账日只取最高修订号（revision），旧修订留痕但不参与计算。
2. 账日序列必须日历连续；缺日即断开（gap），不允许跨缺口拼接。
3. 绝对差额 |diff| 严格大于 100 元记为预警日；连续三个及以上预警日才构成预警段（run）。
   |diff| 恰好 100 元不算超阈值（用严格大于，而非大于等于）。
4. 迟到记录补齐缺口后，两侧的预警段可合并为一段；更正让中间一天回到阈值内则把一段拆成两段。
5. 段合并/拆分/沿用沿用历史派单：与历史段有日重叠的新段复用其派单，不重复派单；
   只有与任何历史段都无关联（无日重叠）的全新段才生成新派单、新收件箱通知。
6. 重试幂等：派单由 dispatch_key（渠道+起止日）唯一化；同一输入重复过账不产生第二条派单或通知。

本模块只依赖 Python 标准库，便于在无第三方依赖的环境下用 unittest 直接验证。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

# ---- 业务常量 ----------------------------------------------------------------

THRESHOLD = 100.0          # 绝对差额「严格大于」该值才是预警日；等于 100 不算
MIN_STREAK = 3             # 连续预警日达到该天数才构成预警段

# 段状态
SEGMENT_OPEN = "open"          # 预警段当前仍成立
SEGMENT_MERGED = "merged"      # 被并入另一段（迟到补齐后的合并）
SEGMENT_SPLIT = "split"        # 被更正拆成了更短的段
SEGMENT_CLOSED = "closed"      # 不再有任何预警日（更正回落 / 缺口断开）

# 谱系事件
EVENT_OPENED = "opened"        # 全新段首次成立，伴随新派单
EVENT_KEPT = "kept"            # 段边界未变（或仅延伸/收缩），沿用原派单
EVENT_MERGED = "merged"        # 多段合并为一，沿用被合并段的派单
EVENT_SPLIT = "split"          # 一段拆为多段，各段沿用原派单
EVENT_CLOSED = "closed"        # 段消失
EVENT_REOPENED = "reopened"    # 缺口断开后同一区间重新成立，沿用原派单

# 消失原因（trace 页对已消失账日/区间标注原因）
REASON_GAP = "gap"             # 缺日断开
REASON_CORRECTED = "corrected"  # 更正后差额回到阈值内
REASON_SUPERSEDED = "superseded"  # 被更高修订号替代


def _as_day(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass(frozen=True)
class LedgerEntry:
    """一条已获批日账。同一 (channel, day) 可有多条不同 revision，取最高者。"""
    channel: str
    day: date
    revision: int
    computed_net: float
    recognized_net: float
    diff: float | None = None          # 不传则由 computed - recognized 推导
    batch_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "channel", str(self.channel).strip())
        object.__setattr__(self, "day", _as_day(self.day))
        object.__setattr__(self, "revision", int(self.revision))
        object.__setattr__(self, "computed_net", float(self.computed_net))
        object.__setattr__(self, "recognized_net", float(self.recognized_net))
        if self.diff is None:
            object.__setattr__(self, "diff", round(self.computed_net - self.recognized_net, 4))
        else:
            object.__setattr__(self, "diff", float(self.diff))


@dataclass
class DayPoint:
    channel: str
    day: date
    revision: int
    diff: float
    over: bool                       # |diff| 严格大于阈值


@dataclass
class Run:
    """一个候选预警段（连续 >= MIN_STREAK 个预警日，中间无缺日）。"""
    channel: str
    start_day: date
    end_day: date
    days: list[date] = field(default_factory=list)
    max_abs_diff: float = 0.0

    def overlaps(self, other: "Run") -> bool:
        return self.channel == other.channel and not (
            self.end_day < other.start_day or other.end_day < self.start_day
        )


@dataclass
class SegmentResult:
    """reconcile 对单个渠道产出的一个当前段（或对历史段的关闭结论）。"""
    channel: str
    start_day: date
    end_day: date
    status: str
    days: list[date]
    max_abs_diff: float
    segment_id: int | None = None        # 沿用的历史段 id；新段为 None
    event: str = ""
    inherited_dispatch_keys: list[str] = field(default_factory=list)
    new_dispatch_key: str | None = None   # 仅全新段有值
    closed_days: list[date] = field(default_factory=list)  # 本次消失的账日
    lost_reason: str | None = None
    ancestor_ids: list[int] = field(default_factory=list)


@dataclass
class ReconcileResult:
    segments: list[SegmentResult] = field(default_factory=list)
    # 与输入无关的稳定派单键：渠道#起止日；全新段才会出现新键
    new_dispatch_keys: list[str] = field(default_factory=list)
    # 每个渠道用于落谱系的结构化事件
    events: list[dict] = field(default_factory=list)


# ---- 纯函数：取最高修订、断点、候选段 -----------------------------------------

def latest_per_day(entries: Iterable[LedgerEntry]) -> dict[date, DayPoint]:
    """每个账日取最高修订号；同修订号重复视为同一条（幂等）。"""
    chosen: dict[date, LedgerEntry] = {}
    for e in entries:
        old = chosen.get(e.day)
        if old is None or e.revision > old.revision:
            chosen[e.day] = e
    points: dict[date, DayPoint] = {}
    for day, e in chosen.items():
        points[day] = DayPoint(
            channel=e.channel, day=e.day, revision=e.revision,
            diff=e.diff, over=abs(e.diff) > THRESHOLD,
        )
    return dict(sorted(points.items()))


def find_runs(points: dict[date, DayPoint], channel: str) -> list[Run]:
    """在「连续日历日且每日 over」的条件下找候选段；缺日或回落自然截断。"""
    runs: list[Run] = []
    streak: list[DayPoint] = []

    def flush():
        if len(streak) >= MIN_STREAK:
            days = [p.day for p in streak]
            runs.append(Run(
                channel=channel, start_day=days[0], end_day=days[-1], days=days,
                max_abs_diff=max(abs(p.diff) for p in streak),
            ))
        streak.clear()

    prev: date | None = None
    for day, p in points.items():
        if prev is not None and day != prev + timedelta(days=1):
            flush()  # 缺日断开
        if not p.over:
            flush()  # 回到阈值内断开
            prev = day
            continue
        streak.append(p)
        prev = day
    flush()
    return runs


def dispatch_key(channel: str, start_day: date, end_day: date) -> str:
    return f"{channel}#{start_day.isoformat()}#{end_day.isoformat()}"


def group_by_channel(entries: Iterable[LedgerEntry]) -> dict[str, list[LedgerEntry]]:
    out: dict[str, list[LedgerEntry]] = {}
    for e in entries:
        out.setdefault(e.channel, []).append(e)
    return out


# ---- 历史段（仅需日区间与派单键，解耦 ORM） ----------------------------------

@dataclass
class HistoricalSegment:
    segment_id: int
    channel: str
    start_day: date
    end_day: date
    dispatch_key: str


# ---- 主对账：合并 / 拆分 / 沿用 / 关闭 / 全新派单 -----------------------------

def _overlap_days(a_start: date, a_end: date, b_start: date, b_end: date) -> list[date]:
    lo, hi = max(a_start, b_start), min(a_end, b_end)
    if lo > hi:
        return []
    out, cur = [], lo
    while cur <= hi:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def reconcile_channel(channel: str, entries: list[LedgerEntry],
                      historical: list[HistoricalSegment]) -> list[SegmentResult]:
    points = latest_per_day(entries)
    runs = find_runs(points, channel)
    old = [h for h in historical if h.channel == channel]

    # 并查集：把「有日重叠」的历史段与新段连为一个连通分量
    nodes: list = [("old", h) for h in old] + [("run", r) for r in runs]
    parent = list(range(len(nodes)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            a, b = nodes[i], nodes[j]
            if a[0] == "old" and b[0] == "old":
                continue  # 历史段之间不直接相连，必须经由当前段才会合并
            a_start, a_end = (a[1].start_day, a[1].end_day)
            b_start, b_end = (b[1].start_day, b[1].end_day)
            if _overlap_days(a_start, a_end, b_start, b_end):
                union(i, j)

    components: dict[int, list] = {}
    for i, node in enumerate(nodes):
        components.setdefault(find(i), []).append(node)

    results: list[SegmentResult] = []
    for comp in components.values():
        comp_old = [n[1] for n in comp if n[0] == "old"]
        comp_runs = [n[1] for n in comp if n[0] == "run"]

        if comp_runs and not comp_old:
            # 全新段：与任何历史段都无关联 → 新派单 + 新通知
            r = comp_runs[0]
            key = dispatch_key(channel, r.start_day, r.end_day)
            results.append(SegmentResult(
                channel=channel, start_day=r.start_day, end_day=r.end_day,
                status=SEGMENT_OPEN, days=r.days, max_abs_diff=r.max_abs_diff,
                segment_id=None, event=EVENT_OPENED, new_dispatch_key=key,
            ))
            continue

        if comp_old and not comp_runs:
            # 历史段在当前数据下整体消失（更正回落或缺口断开）
            for h in comp_old:
                lost, reason = _lost_days(h, points)
                results.append(SegmentResult(
                    channel=channel, start_day=h.start_day, end_day=h.end_day,
                    status=SEGMENT_CLOSED, days=[], max_abs_diff=0.0,
                    segment_id=h.segment_id, event=EVENT_CLOSED,
                    inherited_dispatch_keys=[h.dispatch_key],
                    closed_days=lost, lost_reason=reason, ancestor_ids=[h.segment_id],
                ))
            continue

        # 既有历史段又有当前段：合并 / 拆分 / 沿用，一律复用历史派单
        inherited = sorted({h.dispatch_key for h in comp_old})
        ancestor_ids = sorted({h.segment_id for h in comp_old})
        merged = len(comp_old) >= 2
        split = len(comp_runs) >= 2
        # 以「最早开始的当前段」承接主历史段 id，保证页面从原派单仍可回查；
        # 拆分出的其余当前段没有自己的历史段 id（segment_id 留空），但携带继承派单键。
        comp_runs.sort(key=lambda r: r.start_day)
        primary_old = sorted(comp_old, key=lambda h: h.start_day)[0]

        for index, r in enumerate(comp_runs):
            if merged:
                event, status = EVENT_MERGED, SEGMENT_OPEN
            elif split:
                event, status = EVENT_SPLIT, SEGMENT_OPEN
            else:
                event, status = EVENT_KEPT, SEGMENT_OPEN
            seg_id = primary_old.segment_id if index == 0 else None
            results.append(SegmentResult(
                channel=channel, start_day=r.start_day, end_day=r.end_day,
                status=status, days=r.days, max_abs_diff=r.max_abs_diff,
                segment_id=seg_id, event=event,
                inherited_dispatch_keys=inherited, ancestor_ids=ancestor_ids,
            ))

        # 合并中除承接主段外的历史段一律置为 merged 留痕：它们已被并入新段，不再独立存在。
        if len(comp_old) > 1:
            anchored_id = primary_old.segment_id
            for h in comp_old:
                if h.segment_id == anchored_id:
                    continue
                lost, reason = _lost_days(h, points)
                results.append(SegmentResult(
                    channel=channel, start_day=h.start_day, end_day=h.end_day,
                    status=SEGMENT_MERGED, days=[], max_abs_diff=0.0,
                    segment_id=h.segment_id, event=EVENT_MERGED,
                    inherited_dispatch_keys=[h.dispatch_key],
                    closed_days=lost, lost_reason=(reason if lost else None),
                    ancestor_ids=[h.segment_id],
                ))

    results.sort(key=lambda s: (s.start_day, s.end_day, s.status))
    return results


def _lost_days(h: HistoricalSegment, points: dict[date, DayPoint]) -> tuple[list[date], str]:
    """历史段区间内今天已不构成预警的账日，并给出主因。"""
    lost: list[date] = []
    saw_gap = False
    cur = h.start_day
    while cur <= h.end_day:
        p = points.get(cur)
        if p is None:
            lost.append(cur)
            saw_gap = True
        elif not p.over:
            lost.append(cur)
        cur += timedelta(days=1)
    return lost, REASON_GAP if saw_gap else REASON_CORRECTED


def reconcile(entries: Iterable[LedgerEntry],
              historical: Iterable[HistoricalSegment]) -> ReconcileResult:
    """全渠道对账入口。输入为本次过账后的全部有效日账 + 现存 open 历史段。"""
    result = ReconcileResult()
    by_channel = group_by_channel(entries)
    hist_by_channel: dict[str, list[HistoricalSegment]] = {}
    for h in historical:
        hist_by_channel.setdefault(h.channel, []).append(h)
    for channel in sorted(by_channel):
        segments = reconcile_channel(channel, by_channel[channel], hist_by_channel.get(channel, []))
        result.segments.extend(segments)
        for s in segments:
            if s.new_dispatch_key:
                result.new_dispatch_keys.append(s.new_dispatch_key)
            result.events.append({
                "channel": channel, "event": s.event,
                "start_day": s.start_day.isoformat(), "end_day": s.end_day.isoformat(),
                "segment_id": s.segment_id, "ancestor_ids": s.ancestor_ids,
                "inherited_dispatch_keys": s.inherited_dispatch_keys,
                "new_dispatch_key": s.new_dispatch_key,
                "closed_days": [d.isoformat() for d in s.closed_days],
                "lost_reason": s.lost_reason, "status": s.status,
            })
    return result
