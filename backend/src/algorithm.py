import hashlib,json
import numpy as np
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import f1_score,confusion_matrix
LEVELS=['low','medium','high']
def classify(humidity,holes,mold):
    rng=np.random.default_rng(31)
    x=np.column_stack([rng.uniform(30,90,600),rng.integers(0,31,600),rng.uniform(0,25,600)])
    # 专家规则构造的合成训练样本，不能替代真实馆藏外部验证。
    y=np.where((x[:,0]>=72)|(x[:,2]>=10),'high',np.where((x[:,0]>=60)|(x[:,1]>=10)|(x[:,2]>=2),'medium','low'))
    train,test=train_test_split(np.arange(len(x)),test_size=.25,random_state=31,stratify=y)
    model=DecisionTreeClassifier(max_depth=5,min_samples_leaf=4,random_state=31).fit(x[train],y[train])
    prediction=model.predict(x[test])
    return str(model.predict([[humidity,holes,mold]])[0]),{'macro_f1':float(f1_score(y[test],prediction,average='macro')),'confusion_matrix':confusion_matrix(y[test],prediction,labels=LEVELS).tolist(),'labels':LEVELS,'train_count':len(train),'test_count':len(test),'seed':31,'dataset_sha256':hashlib.sha256(x.tobytes()+y.tobytes()).hexdigest(),'data_source':'专家规则合成样本；留出指标仅证明内部可复现性'}
def group_split(group_counts,seed,test_size):
    # 整册分组：同一馆藏号的全部历次测量只进入训练或留出之一。
    # 按 seed 打乱册序后贪心选取能缩小与目标差距的整册；若一册就超出目标，退而选最接近目标的单册，
    # 并始终给训练组留至少一册，保证留出非空且整册不跨组。
    ids=sorted(group_counts)
    if len(ids)<2:return {gid:'train' for gid in ids}
    order=[ids[i] for i in np.random.default_rng(seed).permutation(len(ids))]
    total=sum(group_counts.values());target=round(total*test_size)
    train=set(order);test=set();held=0
    for gid in order:
        if len(train)<=1:break
        size=group_counts[gid]
        if abs(held+size-target)>=abs(held-target):continue
        train.remove(gid);test.add(gid);held+=size
        if held>=target:break
    if not test:
        pick=min(order,key=lambda gid:abs(group_counts[gid]-target))
        train.discard(pick);test.add(pick)
    return {gid:'test' if gid in test else 'train' for gid in ids}
def snapshot_digest(rows):
    payload=[[row['assessment_id'],row['accession_id'],row['observation_id'],row['level'],float(row['humidity']),int(row['holes']),float(row['mold']),row['split']] for row in sorted(rows,key=lambda r:r['assessment_id'])]
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def training_fingerprint(rows,seed,test_size):
    payload={'seed':seed,'test_size':test_size,'samples':[[row['assessment_id'],row['accession_id'],row['observation_id'],row['level'],float(row['humidity']),int(row['holes']),float(row['mold'])] for row in sorted(rows,key=lambda r:r['assessment_id'])]}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def train_from_snapshot(rows,seed):
    train=[r for r in rows if r['split']=='train'];test=[r for r in rows if r['split']=='test']
    if not train:raise ValueError('封存样本中没有训练分组')
    x=np.array([[r['humidity'],r['holes'],r['mold']] for r in rows],dtype=float)
    y=np.array([r['level'] for r in rows])
    model=DecisionTreeClassifier(max_depth=5,min_samples_leaf=4,random_state=seed)
    model.fit(x[[r['split']=='train' for r in rows]],y[[r['split']=='train' for r in rows]])
    if test:
        prediction=model.predict(x[[r['split']=='test' for r in rows]])
        actual=y[[r['split']=='test' for r in rows]]
        macro=float(f1_score(actual,prediction,labels=LEVELS,average='macro',zero_division=0))
        matrix=confusion_matrix(actual,prediction,labels=LEVELS).tolist()
    else:
        macro=None;matrix=[[0 for _ in LEVELS] for _ in LEVELS]
    groups={r['accession_id'] for r in rows if r['split']=='train'}
    test_groups={r['accession_id'] for r in rows if r['split']=='test'}
    return model,{'macro_f1':macro,'confusion_matrix':matrix,'labels':LEVELS,'train_count':len(train),'test_count':len(test),'group_count':len(groups|test_groups),'train_group_count':len(groups),'test_group_count':len(test_groups),'seed':seed,'dataset_sha256':snapshot_digest(rows),'data_source':'主管复核样本，整册分组留出；仅作内部可复现检查，不代表外部效度'}
