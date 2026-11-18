"""持久化与端到端演化的 sqlite3 标准库集成测试。

本机无 SQLAlchemy/FastAPI（离线、无 pip），无法直接 import services/alerts.py。
本测试用与 models/alert.py 完全一致的表结构/唯一约束建 sqlite 库，复用真实的
alert_engine 做全部业务决策，按 services/alerts.py 相同的映射落库，从而验证：
  - 日账/派单/收件箱唯一约束与幂等重试；
  - 全新段才派单并发收件箱，合并/拆分沿用派单、不重复通知；
  - open→merged/closed 状态流转与谱系、消失原因；
  - 原派单 trace 能回查当时账日区间与现存区间。
SQLAlchemy/FastAPI 接线本身仅经 py_compile 验证，完整栈需按 README 用 Docker 启动。
"""
import os
import sys
import sqlite3
import unittest
from datetime import date

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))
sys.path.insert(0, BACKEND)

from src.services import alert_engine as eng  # noqa: E402
from src.services.alert_engine import (  # noqa: E402
    LedgerEntry, HistoricalSegment,
    SEGMENT_OPEN, SEGMENT_MERGED, SEGMENT_CLOSED,
    EVENT_OPENED, EVENT_CLOSED,
)

DDL = """
CREATE TABLE alert_batches(
  id INTEGER PRIMARY KEY, batch_id TEXT UNIQUE, source TEXT,
  entry_count INTEGER, created_by TEXT, created_at REAL);
CREATE TABLE alert_ledger_entries(
  id INTEGER PRIMARY KEY, batch_id TEXT, channel TEXT, day TEXT, revision INTEGER,
  computed_net REAL, recognized_net REAL, diff REAL, created_at REAL,
  UNIQUE(channel, day, revision));
CREATE TABLE alert_segments(
  id INTEGER PRIMARY KEY, channel TEXT, start_day TEXT, end_day TEXT,
  status TEXT, max_abs_diff REAL, dispatch_key TEXT, created_at REAL, updated_at REAL);
CREATE TABLE alert_dispatches(
  id INTEGER PRIMARY KEY, dispatch_key TEXT UNIQUE, segment_id INTEGER,
  channel TEXT, start_day TEXT, end_day TEXT, status TEXT, created_at REAL);
CREATE TABLE alert_lineage(
  id INTEGER PRIMARY KEY, segment_id INTEGER, channel TEXT, event TEXT,
  start_day TEXT, end_day TEXT, ancestor_ids TEXT, inherited_dispatch_keys TEXT,
  new_dispatch_key TEXT, closed_days TEXT, lost_reason TEXT, at REAL);
CREATE TABLE alert_inbox(
  id INTEGER PRIMARY KEY, dispatch_key TEXT UNIQUE, dispatch_id INTEGER,
  channel TEXT, start_day TEXT, end_day TEXT, title TEXT, detail TEXT,
  read INTEGER DEFAULT 0, created_at REAL);
"""


class SqliteRepository:
    """与 services/alerts.py 同构的最小持久化编排，决策全部来自 alert_engine。"""

    def __init__(self, conn):
        self.db = conn
        self.now = 1_700_000_000.0

    def post_batch(self, batch_id, rows):
        cur = self.db.execute("SELECT 1 FROM alert_batches WHERE batch_id=?", (batch_id,))
        if cur.fetchone():
            return {"deduplicated": True, "new_dispatches": 0}
        entries = [LedgerEntry(channel=r[0], day=r[1], revision=r[2],
                               computed_net=r[3], recognized_net=r[4]) for r in rows]
        self.db.execute("INSERT INTO alert_batches VALUES(?,?,?,?,?,?)",
                        (None, batch_id, "test", len(entries), "t", self.now))
        for e in entries:
            self.db.execute(
                "INSERT INTO alert_ledger_entries VALUES(?,?,?,?,?,?,?,?,?)",
                (None, batch_id, e.channel, e.day.isoformat(), e.revision,
                 e.computed_net, e.recognized_net, e.diff, self.now))
        channels = sorted({e.channel for e in entries})
        return self._reconcile(channels)

    def _load_entries(self, channels):
        ph = ",".join("?" * len(channels))
        out = []
        for r in self.db.execute(
                f"SELECT channel,day,revision,computed_net,recognized_net,diff,batch_id "
                f"FROM alert_ledger_entries WHERE channel IN ({ph})", channels):
            out.append(LedgerEntry(channel=r[0], day=r[1], revision=r[2],
                                   computed_net=r[3], recognized_net=r[4], diff=r[5], batch_id=r[6]))
        return out

    def _historical(self, channels):
        ph = ",".join("?" * len(channels))
        out = []
        for r in self.db.execute(
                f"SELECT id,channel,start_day,end_day,dispatch_key FROM alert_segments "
                f"WHERE channel IN ({ph}) AND status='open'", channels):
            out.append(HistoricalSegment(r[0], r[1], date.fromisoformat(r[2]),
                                         date.fromisoformat(r[3]), r[4]))
        return out

    def _reconcile(self, channels):
        result = eng.reconcile(self._load_entries(channels), self._historical(channels))
        new_dispatches = 0
        for seg in result.segments:
            if seg.event == EVENT_OPENED:
                new_dispatches += self._open_new(seg)
            elif seg.event == EVENT_CLOSED:
                self.db.execute("UPDATE alert_segments SET status='closed',updated_at=? WHERE id=?",
                                (self.now, seg.segment_id))
            elif seg.event == "merged" and seg.status == SEGMENT_MERGED:
                self.db.execute("UPDATE alert_segments SET status='merged',updated_at=? WHERE id=?",
                                (self.now, seg.segment_id))
            else:
                if seg.segment_id is not None:
                    self.db.execute(
                        "UPDATE alert_segments SET start_day=?,end_day=?,max_abs_diff=?,"
                        "status='open',updated_at=? WHERE id=?",
                        (seg.start_day.isoformat(), seg.end_day.isoformat(),
                         round(seg.max_abs_diff, 4), self.now, seg.segment_id))
                else:
                    self._open_inherited(seg)
        for ev in result.events:
            self.db.execute(
                "INSERT INTO alert_lineage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (None, ev.get("segment_id"), ev["channel"], ev["event"], ev["start_day"],
                 ev["end_day"], ",".join(str(i) for i in ev.get("ancestor_ids", [])),
                 "|".join(ev.get("inherited_dispatch_keys", [])), ev.get("new_dispatch_key"),
                 ",".join(ev.get("closed_days", [])), ev.get("lost_reason"), self.now))
        return {"deduplicated": False, "new_dispatches": new_dispatches}

    def _open_new(self, seg):
        key = seg.new_dispatch_key
        exists = self.db.execute(
            "SELECT 1 FROM alert_dispatches WHERE dispatch_key=?", (key,)).fetchone()
        cur = self.db.execute(
            "INSERT INTO alert_segments VALUES(?,?,?,?,?,?,?,?,?)",
            (None, seg.channel, seg.start_day.isoformat(), seg.end_day.isoformat(),
             SEGMENT_OPEN, round(seg.max_abs_diff, 4), key, self.now, self.now))
        segment_id = cur.lastrowid
        if exists:
            return 0
        cur = self.db.execute(
            "INSERT INTO alert_dispatches VALUES(?,?,?,?,?,?,?,?)",
            (None, key, segment_id, seg.channel, seg.start_day.isoformat(),
             seg.end_day.isoformat(), "dispatched", self.now))
        self.db.execute("INSERT INTO alert_inbox VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (None, key, cur.lastrowid, seg.channel, seg.start_day.isoformat(),
                         seg.end_day.isoformat(), "title", "detail", 0, self.now))
        return 1

    def _open_inherited(self, seg):
        key = seg.inherited_dispatch_keys[0]
        self.db.execute("INSERT INTO alert_segments VALUES(?,?,?,?,?,?,?,?,?)",
                        (None, seg.channel, seg.start_day.isoformat(), seg.end_day.isoformat(),
                         SEGMENT_OPEN, round(seg.max_abs_diff, 4), key, self.now, self.now))

    def scalar(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()[0]


def row(channel, day, rev, computed, recognized):
    return (channel, day, rev, computed, recognized)


class PersistenceEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(DDL)
        self.repo = SqliteRepository(self.conn)

    def tearDown(self):
        self.conn.close()

    def _batch1(self):
        # 合作渠道：25-27 超阈值成段 A，28 缺日，29-31 超阈值成段 B（两段被断日隔开）。
        return [
            row("合作渠道", "2026-10-25", 1, 15200, 15020),
            row("合作渠道", "2026-10-26", 1, 15100, 14950),
            row("合作渠道", "2026-10-27", 1, 15300, 15080),
            row("合作渠道", "2026-10-29", 1, 15400, 15210),
            row("合作渠道", "2026-10-30", 1, 15250, 15060),
            row("合作渠道", "2026-10-31", 1, 15150, 14990),
            row("直营内容", "2026-10-26", 1, 19500, 19340),
            row("直营内容", "2026-10-27", 1, 19600, 19410),
            row("直营内容", "2026-10-28", 1, 19400, 19250),
        ]

    def test_first_batch_opens_three_new_dispatches_and_three_inbox(self):
        r = self.repo.post_batch("b1", self._batch1())
        self.assertEqual(r["new_dispatches"], 3)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_dispatches"), 3)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_inbox"), 3)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_segments WHERE status='open'"), 3)
        bounds = {r[0] for r in self.conn.execute(
            "SELECT start_day||'-'||end_day FROM alert_segments WHERE channel='合作渠道'")}
        self.assertEqual(bounds, {"2026-10-25-2026-10-27", "2026-10-29-2026-10-31"})

    def test_retry_same_batch_is_idempotent(self):
        self.repo.post_batch("b1", self._batch1())
        retry = self.repo.post_batch("b1", self._batch1())
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_batches"), 1)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_ledger_entries"), 9)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_dispatches"), 3)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_inbox"), 3)

    def test_duplicate_natural_revision_violates_constraint(self):
        self.repo.post_batch("b1", self._batch1())
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.post_batch("b2", [row("合作渠道", "2026-10-25", 1, 1, 0)])

    def test_late_fill_merges_without_new_dispatch_or_inbox(self):
        self.repo.post_batch("b1", self._batch1())
        before_dispatch = self.repo.scalar("SELECT COUNT(*) FROM alert_dispatches")
        before_inbox = self.repo.scalar("SELECT COUNT(*) FROM alert_inbox")
        # 迟到记录补齐断日 28：A(25-27) 与 B(29-31) 合并为 25-31，沿用两条历史派单。
        r = self.repo.post_batch("b2", [row("合作渠道", "2026-10-28", 1, 15050, 14920)])
        self.assertEqual(r["new_dispatches"], 0)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_dispatches"), before_dispatch)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_inbox"), before_inbox)
        merged = self.conn.execute(
            "SELECT start_day,end_day,dispatch_key,status FROM alert_segments "
            "WHERE channel='合作渠道' ORDER BY start_day").fetchall()
        open_rows = [m for m in merged if m[3] == "open"]
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0][0], "2026-10-25")
        self.assertEqual(open_rows[0][1], "2026-10-31")
        # 合并谱系：含 merged 事件，且被并入的旧段至少一条置 merged 留痕。
        self.assertGreaterEqual(self.repo.scalar(
            "SELECT COUNT(*) FROM alert_lineage WHERE event='merged'"), 1)
        self.assertGreaterEqual(self.repo.scalar(
            "SELECT COUNT(*) FROM alert_segments WHERE channel='合作渠道' AND status='merged'"), 1)

    def test_correction_closes_segment_with_corrected_reason(self):
        self.repo.post_batch("b1", self._batch1())
        # 直营 27 修订号 2 回落到阈值内：26 与 28 两个碎片均不足三日 → 段关闭。
        self.repo.post_batch("b2", [row("直营内容", "2026-10-27", 2, 19600, 19550)])
        self.assertEqual(self.repo.scalar(
            "SELECT COUNT(*) FROM alert_segments WHERE channel='直营内容' AND status='open'"), 0)
        self.assertEqual(self.repo.scalar(
            "SELECT COUNT(*) FROM alert_segments WHERE channel='直营内容' AND status='closed'"), 1)
        lineage = self.conn.execute(
            "SELECT event,lost_reason,closed_days FROM alert_lineage "
            "WHERE channel='直营内容' AND event='closed'").fetchone()
        self.assertIsNotNone(lineage)
        self.assertEqual(lineage[1], "corrected")
        self.assertIn("2026-10-27", lineage[2])
        # 关闭不产生新派单或收件箱（仍为首批的 3 条）。
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_inbox"), 3)

    def test_highest_revision_is_used_on_recorrection(self):
        self.repo.post_batch("b1", [
            row("甲", "2026-10-01", 1, 10300, 10000),
            row("甲", "2026-10-02", 1, 10300, 10000),
            row("甲", "2026-10-03", 1, 10300, 10000),
        ])
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_dispatches"), 1)
        # 高修订把三天都拉回阈值内 → 段关闭；最高修订号 2 生效。
        self.repo.post_batch("b2", [
            row("甲", "2026-10-01", 2, 10050, 10000),
            row("甲", "2026-10-02", 2, 10050, 10000),
            row("甲", "2026-10-03", 2, 10050, 10000),
        ])
        self.assertEqual(self.repo.scalar(
            "SELECT COUNT(*) FROM alert_segments WHERE channel='甲' AND status='open'"), 0)
        latest_rev = self.repo.scalar(
            "SELECT revision FROM alert_ledger_entries WHERE channel='甲' AND day='2026-10-02' "
            "ORDER BY revision DESC LIMIT 1")
        self.assertEqual(latest_rev, 2)

    def test_trace_from_original_dispatch_shows_timeline_and_current_range(self):
        self.repo.post_batch("b1", self._batch1())
        key = self.conn.execute(
            "SELECT dispatch_key FROM alert_dispatches WHERE channel='合作渠道' "
            "AND start_day='2026-10-25'").fetchone()[0]
        self.repo.post_batch("b2", [row("合作渠道", "2026-10-28", 1, 15050, 14920)])
        events = [r[0] for r in self.conn.execute(
            "SELECT event FROM alert_lineage WHERE new_dispatch_key=? "
            "OR inherited_dispatch_keys LIKE ? ORDER BY id", (key, f"%{key}%"))]
        self.assertIn("opened", events)
        self.assertIn("merged", events)
        # 现存区间用原派单键可回查，已合并延伸到 31 日。
        current = self.conn.execute(
            "SELECT start_day,end_day FROM alert_segments WHERE dispatch_key=? AND status='open'",
            (key,)).fetchall()
        self.assertEqual(current, [("2026-10-25", "2026-10-31")])

    def test_concurrent_batches_same_channel_remain_consistent(self):
        # 两个批次顺序提交（模拟并发被行锁串化后的结果），最终只应有一个合并段与一条派单。
        self.repo.post_batch("b1", [
            row("丙", "2026-11-01", 1, 10200, 10000),
            row("丙", "2026-11-02", 1, 10200, 10000),
            row("丙", "2026-11-03", 1, 10200, 10000),
        ])
        self.repo.post_batch("b2", [
            row("丙", "2026-11-04", 1, 10200, 10000),
            row("丙", "2026-11-05", 1, 10200, 10000),
        ])
        # 4、5 与 1-3 相邻不重叠 → 不合并；4-5 仅两日不成段，保持原 1-3 一段。
        self.assertEqual(self.repo.scalar(
            "SELECT COUNT(*) FROM alert_segments WHERE channel='丙' AND status='open'"), 1)
        self.assertEqual(self.repo.scalar("SELECT COUNT(*) FROM alert_dispatches"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
