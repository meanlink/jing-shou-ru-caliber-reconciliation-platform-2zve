import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, AlertBatch
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import alerts
from src.schemas.alert import AlertBatchInput, AlertEntryInput
from src.utils.security import hash_password
from src.utils.serialization import checksum


def _entry(channel, day, revision, computed, recognized, batch_id):
    return AlertEntryInput(channel=channel, day=day, revision=revision,
                           computed_net=computed, recognized_net=recognized,
                           diff=round(computed - recognized, 4), batch_id=batch_id)


def seed_alerts(db):
    """构造可解释的预警演化：全新段派单 → 迟到补齐合并 → 更正拆分/回落。"""
    if db.scalar(select(AlertBatch).where(AlertBatch.batch_id == 'seed-alert-1')):
        return
    # 第一批：合作渠道 25-27 与 29-31 各连续三日超阈值，28 缺日断开（两个独立段、两条派单）；
    # 直营内容 26-28 连续三日超阈值成一段。
    batch1 = AlertBatchInput(batch_id='seed-alert-1', source='seed-demo', entries=[
        _entry('合作渠道', '2026-10-25', 1, 15200, 15020, 'seed-alert-1'),  # +180
        _entry('合作渠道', '2026-10-26', 1, 15100, 14950, 'seed-alert-1'),  # +150
        _entry('合作渠道', '2026-10-27', 1, 15300, 15080, 'seed-alert-1'),  # +220
        _entry('合作渠道', '2026-10-29', 1, 15400, 15210, 'seed-alert-1'),  # +190
        _entry('合作渠道', '2026-10-30', 1, 15250, 15060, 'seed-alert-1'),  # +190
        _entry('合作渠道', '2026-10-31', 1, 15150, 14990, 'seed-alert-1'),  # +160
        _entry('直营内容', '2026-10-26', 1, 19500, 19340, 'seed-alert-1'),  # +160
        _entry('直营内容', '2026-10-27', 1, 19600, 19410, 'seed-alert-1'),  # +190
        _entry('直营内容', '2026-10-28', 1, 19400, 19250, 'seed-alert-1'),  # +150
    ])
    alerts.post_batch(db, batch1, 'seed')

    # 第二批：合作渠道迟到补齐 10-28，断日闭合，25-27 与 29-31 合并为 25-31，沿用两条原派单、不发新通知；
    # 直营 10-27 修订号 2 回落到阈值内（+50），26 与 28 两个碎片均不足三日，原段关闭并标明 corrected 原因。
    batch2 = AlertBatchInput(batch_id='seed-alert-2', source='seed-demo', entries=[
        _entry('合作渠道', '2026-10-28', 1, 15050, 14920, 'seed-alert-2'),  # +130 迟到补齐
        _entry('直营内容', '2026-10-27', 2, 19600, 19550, 'seed-alert-2'),  # +50 更正回落
    ])
    alerts.post_batch(db, batch2, 'seed')
    db.add(Audit(actor='seed', action='demo.alerts', detail={'batches': ['seed-alert-1', 'seed-alert-2']}, at=time.time()))


def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        seed_alerts(db)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,AlertBatch]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
