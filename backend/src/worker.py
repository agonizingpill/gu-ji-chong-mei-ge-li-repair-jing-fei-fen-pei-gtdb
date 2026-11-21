import time,logging
from pathlib import Path
from sqlalchemy import select
from .database import Session
from .models import RiskAssessment,Observation
from .risk_service import queue
from .algorithm import classify
from . import training_service
logging.basicConfig(level=logging.INFO)
last_sweep=0
training_service.promote_pending(initial=True)
while True:
    Path('/tmp/worker-heartbeat').touch()
    try:
        if time.monotonic()-last_sweep>15:
            last_sweep=time.monotonic();training_service.promote_pending()
        item=queue.blpop(['folio:risk','folio:train'],timeout=2)
        if not item:continue
        channel,raw=item
        if channel=='folio:train':
            training_service.process_run(int(raw));continue
        with Session() as db:
            row=db.get(RiskAssessment,int(raw))
            if not row or row.status!='queued':continue
            row.status='running';db.commit()
            try:
                obs=db.get(Observation,row.observation_id)
                row.predicted,row.metrics=classify(obs.humidity,obs.holes,obs.mold);row.status='done';row.error=''
                Path('/artifacts').mkdir(exist_ok=True)
                Path(f'/artifacts/risk-{row.id}.json').write_text(__import__('json').dumps(row.metrics,ensure_ascii=False))
            except Exception as exc:row.status='failed';row.error=str(exc)[:200]
            db.commit()
    except Exception:logging.exception('worker iteration');time.sleep(1)
