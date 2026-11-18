"""连续差额预警引擎的纯标准库单元测试。

覆盖 BUSINESS.md 全部叙事规则：最高修订号、严格 >100、连续三天、缺日断开、
迟到补齐合并、更正拆分/回落、派单沿用、全新段才通知、重试幂等、相邻不重叠不合并。
运行：python3 -m unittest -v （在本目录或加 PYTHONPATH 指向 backend）
"""
import os
import sys
import unittest
from datetime import date

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))
sys.path.insert(0, BACKEND)

from src.services import alert_engine as eng  # noqa: E402
from src.services.alert_engine import (  # noqa: E402
    LedgerEntry, HistoricalSegment,
    EVENT_OPENED, EVENT_KEPT, EVENT_MERGED, EVENT_SPLIT, EVENT_CLOSED,
    SEGMENT_OPEN, SEGMENT_CLOSED, SEGMENT_MERGED,
    REASON_GAP, REASON_CORRECTED,
)

CH = "合作渠道"


def E(day, rev, computed, recognized, channel=CH):
    return LedgerEntry(channel=channel, day=day, revision=rev,
                       computed_net=computed, recognized_net=recognized)


def over(day, rev=1, amount=180.0, channel=CH):
    """一条超阈值日账，差额 +amount。"""
    return E(day, rev, 15000.0 + amount, 15000.0, channel)


def under(day, rev=1, amount=50.0, channel=CH):
    return E(day, rev, 15000.0 + amount, 15000.0, channel)


def hist(seg_id, start, end, channel=CH):
    return HistoricalSegment(
        segment_id=seg_id, channel=channel,
        start_day=date.fromisoformat(start), end_day=date.fromisoformat(end),
        dispatch_key=eng.dispatch_key(channel, date.fromisoformat(start), date.fromisoformat(end)),
    )


def run_once(entries, historical=None, channel=CH):
    return eng.reconcile_channel(channel, entries, historical or [])


class LatestRevisionTests(unittest.TestCase):
    def test_each_day_uses_highest_revision(self):
        entries = [
            over("2026-10-01", rev=1, amount=300),
            over("2026-10-01", rev=2, amount=200),
            under("2026-10-01", rev=3, amount=40),   # 最高修订已回落到阈值内
            over("2026-10-02", rev=1, amount=150),
        ]
        points = eng.latest_per_day(entries)
        self.assertEqual(points[date(2026, 10, 1)].revision, 3)
        self.assertFalse(points[date(2026, 10, 1)].over)
        self.assertTrue(points[date(2026, 10, 2)].over)

    def test_same_revision_duplicate_is_idempotent(self):
        entries = [over("2026-10-01", rev=2), over("2026-10-01", rev=2)]
        points = eng.latest_per_day(entries)
        self.assertEqual(len(points), 1)


class ThresholdAndStreakTests(unittest.TestCase):
    def test_threshold_is_strictly_greater_than_100(self):
        # 差额恰好 100 不算超阈值；三天 100/100/200 不应成段。
        entries = [
            E("2026-10-01", 1, 15100, 15000),  # diff 100
            E("2026-10-02", 1, 15100, 15000),  # diff 100
            E("2026-10-03", 1, 15200, 15000),  # diff 200
        ]
        self.assertEqual(run_once(entries), [])

    def test_negative_diff_uses_absolute_value(self):
        entries = [
            E("2026-10-01", 1, 14800, 15000),  # -200
            E("2026-10-02", 1, 14850, 15000),  # -150
            E("2026-10-03", 1, 14900, 15000),  # -100 边界，不超
        ]
        self.assertEqual(run_once(entries), [])
        entries.append(E("2026-10-04", 1, 14700, 15000))  # -300，连续三日超（1,2,4 不连续？）
        # 1,2 超，3 不超，4 超 → 都不足三日
        self.assertEqual(run_once(entries), [])

    def test_three_consecutive_days_opens_segment(self):
        entries = [over(f"2026-10-0{i}", 1) for i in (1, 2, 3)]
        res = run_once(entries)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].event, EVENT_OPENED)
        self.assertIsNotNone(res[0].new_dispatch_key)
        self.assertEqual(res[0].start_day, date(2026, 10, 1))
        self.assertEqual(res[0].end_day, date(2026, 10, 3))

    def test_two_consecutive_days_is_not_enough(self):
        entries = [over("2026-10-01", 1), over("2026-10-02", 1)]
        self.assertEqual(run_once(entries), [])

    def test_four_days_single_segment(self):
        entries = [over(f"2026-10-0{i}", 1) for i in (1, 2, 3, 4)]
        res = run_once(entries)
        self.assertEqual(len(res), 1)
        self.assertEqual(len(res[0].days), 4)


class GapTests(unittest.TestCase):
    def test_missing_day_breaks_streak(self):
        # 1,2 超；缺 3；4,5,6 超 → 前段不足三日消失，后段独立成段。
        entries = [
            over("2026-10-01", 1), over("2026-10-02", 1),
            over("2026-10-04", 1), over("2026-10-05", 1), over("2026-10-06", 1),
        ]
        res = run_once(entries)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].start_day, date(2026, 10, 4))
        self.assertEqual(res[0].end_day, date(2026, 10, 6))

    def test_under_threshold_day_breaks_streak(self):
        entries = [
            over("2026-10-01", 1), over("2026-10-02", 1),
            under("2026-10-03", 1),
            over("2026-10-04", 1), over("2026-10-05", 1),
        ]
        self.assertEqual(run_once(entries), [])


class IdempotencyTests(unittest.TestCase):
    def _batch(self):
        return [over(f"2026-10-0{i}", 1) for i in (1, 2, 3)]

    def test_repeated_reconcile_no_history_still_one_key(self):
        first = eng.reconcile(self._batch(), [])
        second = eng.reconcile(self._batch(), [])
        self.assertEqual(first.new_dispatch_keys, second.new_dispatch_keys)
        self.assertEqual(len(first.new_dispatch_keys), 1)

    def test_reconcile_against_own_history_is_kept_not_new(self):
        first = run_once(self._batch(), [])
        key = first[0].new_dispatch_key
        h = [hist(1, "2026-10-01", "2026-10-03")]
        second = run_once(self._batch(), h)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].event, EVENT_KEPT)
        self.assertIsNone(second[0].new_dispatch_key)
        self.assertIn(key, second[0].inherited_dispatch_keys)


class MergeTests(unittest.TestCase):
    def test_late_entry_fills_gap_and_merges_two_segments(self):
        # 历史两段：1-3 与 5-7（中间 4 缺日）。迟到补齐 4 后合并为 1-7。
        historical = [hist(1, "2026-10-01", "2026-10-03"),
                      hist(2, "2026-10-05", "2026-10-07")]
        entries = [over(f"2026-10-0{i}", 1) for i in range(1, 8)]  # 1..7 全超，含迟到的 4
        res = run_once(entries, historical)
        open_segs = [s for s in res if s.status == SEGMENT_OPEN]
        self.assertEqual(len(open_segs), 1)
        merged = open_segs[0]
        self.assertEqual(merged.event, EVENT_MERGED)
        self.assertEqual(merged.start_day, date(2026, 10, 1))
        self.assertEqual(merged.end_day, date(2026, 10, 7))
        # 沿用两段历史派单，不产生新派单键。
        self.assertEqual(len(merged.inherited_dispatch_keys), 2)
        self.assertIsNone(merged.new_dispatch_key)
        self.assertEqual(sorted(merged.ancestor_ids), [1, 2])
        # 被并入的旧段有 merged 留痕。
        self.assertTrue(any(s.status == SEGMENT_MERGED for s in res))

    def test_adjacent_but_non_overlapping_segments_do_not_merge(self):
        # 历史 1-3，当前 4-6：日历相邻但无日重叠，不允许跨边界拼接 → 全新段。
        historical = [hist(1, "2026-10-01", "2026-10-03")]
        entries = [over(f"2026-10-0{i}", 1) for i in (4, 5, 6)]
        res = run_once(entries, historical)
        self.assertEqual(len(res), 2)  # 一个 closed 历史段 + 一个全新段
        opened = [s for s in res if s.event == EVENT_OPENED]
        closed = [s for s in res if s.event == EVENT_CLOSED]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].start_day, date(2026, 10, 4))
        self.assertIsNotNone(opened[0].new_dispatch_key)
        self.assertEqual(len(closed), 1)


class SplitAndCorrectionTests(unittest.TestCase):
    def test_correction_too_short_fragments_closes_segment(self):
        # 历史 1-5 五日段；把中间 3 更正回阈值内，碎片 1-2 与 4-5 均不足三日 → 关闭。
        historical = [hist(1, "2026-10-01", "2026-10-05")]
        entries = [over(f"2026-10-0{i}", 1) for i in (1, 2, 4, 5)]
        entries.append(under("2026-10-03", rev=2))
        res = run_once(entries, historical)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].event, EVENT_CLOSED)
        self.assertEqual(res[0].status, SEGMENT_CLOSED)
        self.assertIn(date(2026, 10, 3), res[0].closed_days)
        self.assertEqual(res[0].lost_reason, REASON_CORRECTED)
        # 沿用原派单键，无新派单。
        self.assertEqual(res[0].inherited_dispatch_keys,
                         [eng.dispatch_key(CH, date(2026, 10, 1), date(2026, 10, 5))])

    def test_correction_splits_into_two_standing_segments(self):
        # 历史 1-7 七日段；更正中间 4 回阈值内，拆成 1-3 与 5-7 两个三日段。
        historical = [hist(1, "2026-10-01", "2026-10-07")]
        entries = [over(f"2026-10-0{i}", 1) for i in (1, 2, 3, 5, 6, 7)]
        entries.append(under("2026-10-04", rev=2))
        res = run_once(entries, historical)
        standing = [s for s in res if s.status == SEGMENT_OPEN]
        self.assertEqual(len(standing), 2)
        self.assertTrue(all(s.event == EVENT_SPLIT for s in standing))
        bounds = {(s.start_day, s.end_day) for s in standing}
        self.assertEqual(bounds, {(date(2026, 10, 1), date(2026, 10, 3)),
                                  (date(2026, 10, 5), date(2026, 10, 7))})
        # 两段都继承原派单，且只有主段锚定原 segment id。
        self.assertTrue(all(s.new_dispatch_key is None for s in standing))
        anchored = [s for s in standing if s.segment_id == 1]
        self.assertEqual(len(anchored), 1)

    def test_gap_close_reports_gap_reason(self):
        historical = [hist(1, "2026-10-01", "2026-10-03")]
        entries = [over("2026-10-01", 1), over("2026-10-02", 1)]  # 缺 3
        res = run_once(entries, historical)
        self.assertEqual(res[0].event, EVENT_CLOSED)
        self.assertEqual(res[0].lost_reason, REASON_GAP)
        self.assertIn(date(2026, 10, 3), res[0].closed_days)

    def test_higher_revision_correction_drives_split(self):
        # 同账日修订号 1 超阈值、修订号 2 回落，引擎取最高修订，触发拆分。
        historical = [hist(1, "2026-10-01", "2026-10-05")]
        entries = [over(f"2026-10-0{i}", 1) for i in (1, 2, 4, 5)]
        entries.append(over("2026-10-03", rev=1, amount=300))
        entries.append(under("2026-10-03", rev=2, amount=20))
        res = run_once(entries, historical)
        self.assertEqual(res[0].event, EVENT_CLOSED)
        self.assertEqual(res[0].lost_reason, REASON_CORRECTED)


class MultiChannelTests(unittest.TestCase):
    def test_channels_are_scoped_independently(self):
        entries = [
            over("2026-10-01", 1, channel="甲"), over("2026-10-02", 1, channel="甲"),
            over("2026-10-03", 1, channel="甲"),
            over("2026-10-01", 1, channel="乙"), over("2026-10-02", 1, channel="乙"),
        ]
        result = eng.reconcile(entries, [])
        keys = result.new_dispatch_keys
        self.assertEqual(len(keys), 1)
        self.assertIn("甲#", keys[0])
        by_channel = {s.channel for s in result.segments}
        self.assertEqual(by_channel, {"甲"})

    def test_distinct_channels_get_distinct_new_keys(self):
        entries = []
        for c in ("甲", "乙"):
            entries += [over(f"2026-10-0{i}", 1, channel=c) for i in (1, 2, 3)]
        result = eng.reconcile(entries, [])
        self.assertEqual(len(result.new_dispatch_keys), 2)


class DispatchKeyTests(unittest.TestCase):
    def test_key_stable_for_same_range(self):
        k1 = eng.dispatch_key(CH, date(2026, 10, 1), date(2026, 10, 3))
        k2 = eng.dispatch_key(CH, date(2026, 10, 1), date(2026, 10, 3))
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, eng.dispatch_key("其他", date(2026, 10, 1), date(2026, 10, 3)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
