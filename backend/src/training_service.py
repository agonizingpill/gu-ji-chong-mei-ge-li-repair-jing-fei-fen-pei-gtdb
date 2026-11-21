import os,json
from collections import Counter
from datetime import datetime,timezone,timedelta
from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from .database import Session
from .models import Accession,Observation,RiskAssessment,SampleWithdrawal,TrainingRun,TrainingSample,ModelVersion,VersionSample,Audit
from .algorithm import LEVELS,group_split,snapshot_digest,training_fingerprint,train_from_snapshot
from .common import get,reject,finish,audit
from .risk_service import queue
STALE_SECONDS=120
ARTIFACTS_DIR=os.getenv('ARTIFACTS_DIR','/artifacts')
def _aware(value):
    if value is None:return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
def _sample(row):
    return {'assessment_id':row.assessment_id,'accession_id':row.accession_id,'observation_id':row.observation_id,'level':row.level,'humidity':row.humidity,'holes':row.holes,'mold':row.mold,'split':row.split}
def eligible_samples(db):
    # 每位主管复核过的观测只取最新一条复核；已撤回的样本不再进入新批次。
    latest=select(func.max(RiskAssessment.id)).where(RiskAssessment.status=='reviewed',RiskAssessment.approved.in_(LEVELS)).group_by(RiskAssessment.observation_id).scalar_subquery()
    withdrawn=set(db.scalars(select(SampleWithdrawal.assessment_id)))
    rows=db.execute(select(RiskAssessment,Observation,Accession).join(Observation,Observation.id==RiskAssessment.observation_id).join(Accession,Accession.id==RiskAssessment.accession_id).where(RiskAssessment.id.in_(latest)).order_by(RiskAssessment.accession_id,RiskAssessment.observation_id)).all()
    return [{'assessment_id':a.id,'accession_id':a.accession_id,'observation_id':o.id,'level':a.approved,'humidity':o.humidity,'holes':o.holes,'mold':o.mold} for a,o,c in rows if a.id not in withdrawn]
def start(db,data,user):
    # 训练封存会读取全部候选卷册，先按册号升序锁住卷册，与撤回、版本入库共用同一取得顺序。
    # 这样并发提交同一批样本时只建一单，后到者读到既有批次后复用。
    for accession_id in db.scalars(select(Accession.id).order_by(Accession.id)):
        db.scalar(select(Accession).where(Accession.id==accession_id).with_for_update())
    samples=eligible_samples(db)
    reject(not samples,'暂无可用于训练的主管复核样本，请先完成复核')
    seed=data.seed;test_size=data.test_size
    fingerprint=training_fingerprint(samples,seed,test_size)
    existing=db.scalar(select(TrainingRun).where(TrainingRun.fingerprint==fingerprint))
    if existing:
        if existing.status=='failed':return dict(dump_run(_requeue(db,existing,user)),reused=True)
        if existing.status=='queued':queue.rpush('folio:train',existing.id)
        return dict(dump_run(existing),reused=True)
    counts=Counter(sample['accession_id'] for sample in samples)
    reject(len(counts)<2,'整册分组留出至少需要两册主管复核样本')
    splits=group_split(counts,seed,test_size)
    run=TrainingRun(fingerprint=fingerprint,seed=seed,test_size=test_size,sample_count=len(samples),group_count=len(counts),train_count=sum(1 for s in samples if splits[s['accession_id']]=='train'),test_count=sum(1 for s in samples if splits[s['accession_id']]=='test'),train_group_count=len({s['accession_id'] for s in samples if splits[s['accession_id']]=='train'}),test_group_count=len({s['accession_id'] for s in samples if splits[s['accession_id']]=='test'}))
    db.add(run)
    try:db.flush()
    except IntegrityError:
        # PostgreSQL 行锁已串行化并发封存；此分支兜底不支持行锁的部署，复用既有批次而不重复登记。
        db.rollback()
        existing=db.scalar(select(TrainingRun).where(TrainingRun.fingerprint==fingerprint))
        reject(existing is None,'训练批次登记冲突，请刷新后重试')
        return dict(dump_run(existing),reused=True)
    frozen=[]
    for sample in samples:
        row=TrainingSample(run_id=run.id,split=splits[sample['accession_id']],**sample)
        db.add(row);frozen.append(_sample(row))
    run.snapshot_sha256=snapshot_digest(frozen)
    audit(db,user,'封存训练样本',run.id);db.commit()
    queue.rpush('folio:train',run.id)
    return dict(dump_run(run),reused=False)
def _requeue(db,run,user):
    run.status='queued';run.error='';run.heartbeat_at=None;run.updated_at=datetime.now(timezone.utc)
    db.add(Audit(actor=user.username,action='续跑训练批次',detail=f'run #{run.id} / 沿用原分组'))
    db.commit()
    # 原封存样本与分组保持不变，只重新排队。
    queue.rpush('folio:train',run.id)
    return run
def retry(db,id,user):
    run=get(db,TrainingRun,id,True)
    reject(db.scalar(select(ModelVersion).where(ModelVersion.training_run_id==id)) is not None,'该批次已入库模型版本，不能重复登记；如需换样本请重新发起训练')
    if run.status=='running':
        heartbeat=_aware(run.heartbeat_at or run.updated_at or run.created_at)
        reject(heartbeat and datetime.now(timezone.utc)-heartbeat<timedelta(seconds=STALE_SECONDS),'批次仍在运算，请稍后查看')
    reject(run.status not in ('queued','running','failed','completed'),'当前批次状态不能续跑')
    return dump_run(_requeue(db,run,user))
def withdraw(db,id,data,user):
    row=get(db,RiskAssessment,id)
    reject(row.status!='reviewed' or row.approved not in LEVELS,'仅主管复核后的样本可撤回')
    reject(db.scalar(select(SampleWithdrawal).where(SampleWithdrawal.assessment_id==id)) is not None,'该样本已撤回')
    # 先锁卷册再加锁评估记录，与版本入库的卷册锁保持同一取得顺序，避免并发死锁；
    # 撤回先提交时，随后入库的版本会读到撤回记录并作废。
    get(db,Accession,row.accession_id,True)
    row=get(db,RiskAssessment,id,True)
    db.add(SampleWithdrawal(assessment_id=id,note=data.note,actor=user.username))
    return finish(db,user,'撤回训练样本',row)
def run_detail(db,id):
    run=get(db,TrainingRun,id)
    rows=[_sample(v) for v in db.scalars(select(TrainingSample).where(TrainingSample.run_id==id).order_by(TrainingSample.id))]
    return dict(dump_run(run),samples=rows)
def version_detail(db,id):
    version=get(db,ModelVersion,id)
    rows=[_sample(v) for v in db.scalars(select(VersionSample).where(VersionSample.version_id==id).order_by(VersionSample.id))]
    return dict(dump_version(version),samples=rows)
def dump_run(run):return {c.name:getattr(run,c.name) for c in run.__table__.columns}
def dump_version(row):return {c.name:getattr(row,c.name) for c in row.__table__.columns}
def write_artifact(run_id,metrics):
    try:
        os.makedirs(ARTIFACTS_DIR,exist_ok=True)
        path=os.path.join(ARTIFACTS_DIR,f'training-{run_id}.json')
        with open(path,'w',encoding='utf-8') as handle:json.dump(metrics,handle,ensure_ascii=False)
        return path
    except Exception:return ''
def claim_run(run_id):
    with Session() as db:
        run=db.scalar(select(TrainingRun).where(TrainingRun.id==run_id).with_for_update().execution_options(populate_existing=True))
        if not run:return None
        if db.scalar(select(ModelVersion).where(ModelVersion.training_run_id==run_id)) is not None:
            if run.status!='completed':run.status='completed';run.updated_at=datetime.now(timezone.utc);db.commit()
            return None
        if run.status!='queued':return None
        rows=[_sample(v) for v in db.scalars(select(TrainingSample).where(TrainingSample.run_id==run_id).order_by(TrainingSample.id))]
        run.status='running';run.attempts=(run.attempts or 0)+1;run.heartbeat_at=datetime.now(timezone.utc);run.updated_at=datetime.now(timezone.utc)
        db.commit()
        return {'id':run.id,'seed':run.seed,'samples':rows}
def register_version(run_id,metrics,artifact_path=''):
    with Session() as db:
        run=db.scalar(select(TrainingRun).where(TrainingRun.id==run_id).with_for_update().execution_options(populate_existing=True))
        if not run:raise ValueError('训练批次不存在')
        if db.scalar(select(ModelVersion).where(ModelVersion.training_run_id==run_id)) is not None:
            run.status='completed';run.updated_at=datetime.now(timezone.utc);db.commit();return None
        rows=[_sample(v) for v in db.scalars(select(TrainingSample).where(TrainingSample.run_id==run_id).order_by(TrainingSample.id))]
        if not rows:raise ValueError('封存样本为空')
        # 按卷册号升序加锁，与样本撤回的卷册锁串行化，避免撤回与入库互相穿透。
        for accession_id in sorted({row['accession_id'] for row in rows}):
            db.scalar(select(Accession).where(Accession.id==accession_id).with_for_update())
        withdrawn=sorted(db.scalars(select(SampleWithdrawal.assessment_id).where(SampleWithdrawal.assessment_id.in_([row['assessment_id'] for row in rows]))))
        stored=dict(metrics);stored['withdrawn_assessment_ids']=withdrawn
        version=ModelVersion(training_run_id=run_id,label=f'MV-{run_id:04d}',status='voided' if withdrawn else 'active',macro_f1=stored.get('macro_f1'),metrics=stored,snapshot_sha256=stored.get('dataset_sha256',''),sample_count=len(rows),train_count=stored.get('train_count',0),test_count=stored.get('test_count',0),group_count=stored.get('group_count',0),train_group_count=stored.get('train_group_count',0),test_group_count=stored.get('test_group_count',0),artifact_path=artifact_path,void_reason=('样本已撤回：'+', '.join(f'#{v}' for v in withdrawn)) if withdrawn else '')
        db.add(version);db.flush()
        # 版本保留开训时封存的原样本，供后续追查；之后撤回也不改动这份快照。
        for row in rows:db.add(VersionSample(version_id=version.id,assessment_id=row['assessment_id'],accession_id=row['accession_id'],observation_id=row['observation_id'],level=row['level'],humidity=row['humidity'],holes=row['holes'],mold=row['mold'],split=row['split']))
        run.status='completed';run.error='';run.metrics=stored;run.snapshot_sha256=stored.get('dataset_sha256','');run.heartbeat_at=None;run.updated_at=datetime.now(timezone.utc)
        db.add(Audit(actor='worker',action='训练版本作废' if withdrawn else '训练版本入库',detail=f'{version.label} / run #{run_id}'))
        db.commit()
        return version.id
def process_run(run_id):
    claimed=claim_run(run_id)
    if claimed is None:return False
    try:
        model,metrics=train_from_snapshot(claimed['samples'],claimed['seed'])
        artifact=write_artifact(run_id,{'metrics':metrics,'version_label':f'MV-{run_id:04d}'})
        register_version(run_id,metrics,artifact)
        return True
    except Exception as exc:
        with Session() as db:
            run=db.scalar(select(TrainingRun).where(TrainingRun.id==run_id).with_for_update().execution_options(populate_existing=True))
            if run and run.status=='running':
                run.status='failed';run.error=str(exc)[:300];run.heartbeat_at=None;run.updated_at=datetime.now(timezone.utc)
                db.add(Audit(actor='worker',action='训练批次失败',detail=f'run #{run_id}: {str(exc)[:200]}'))
                db.commit()
        return False
def promote_pending(initial=False):
    # worker 启动时补投所有已封存批次；之后只回收久未推进或心跳过期的批次。
    # 分组和样本始终取自 `training_samples`，这里不重算。
    now=datetime.now(timezone.utc);ids=[]
    with Session() as db:
        for run in db.scalars(select(TrainingRun).where(TrainingRun.status.in_(['queued','running'])).order_by(TrainingRun.id)):
            touched=run.updated_at or run.created_at
            age=(now-_aware(touched)).total_seconds() if touched else 0
            if run.status=='queued':
                if initial or age>=STALE_SECONDS:
                    run.updated_at=now;ids.append(run.id)
            else:
                heartbeat=_aware(run.heartbeat_at or touched)
                if initial or (heartbeat and (now-heartbeat).total_seconds()>=STALE_SECONDS):
                    run.status='queued';run.heartbeat_at=None;run.updated_at=now
                    db.add(Audit(actor='worker',action='续跑中断批次',detail=f'run #{run.id}'))
                    ids.append(run.id)
        db.commit()
    for run_id in ids:queue.rpush('folio:train',run_id)
    return ids
