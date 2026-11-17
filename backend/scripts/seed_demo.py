from sqlalchemy import select,func
from src.config.database import Session,Base,engine
from src.models.entities import User,Node,RequestSet,Trial,RetentionTemplate
from src.utils.security import password_hash

def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin','operator','viewer'):
            email=role+'@qiyuan.test'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,password=password_hash('Demo2026!'),role=role))
        for name,role,pos,delay,note in [('harbor-primary','primary',105,0,'栖原主站 · 写入航道'),('north-replica','replica',104,150,'北站副本 · 延迟偏高'),('south-replica','replica',105,20,'南站副本 · 常用读航道')]:
            if not db.scalar(select(Node).where(Node.name==name)):db.add(Node(name=name,role=role,status='healthy',lineage='qiyuan-log-01',position=pos,delay_ms=delay,note=note))
        mixed=[{'at_ms':0,'kind':'write','session':'lab-A'},{'at_ms':100,'kind':'read','session':'lab-A'},{'at_ms':600,'kind':'replicate','node':'south-replica','position':106,'delay_ms':20},{'at_ms':700,'kind':'read','session':'lab-A'},{'at_ms':800,'kind':'node_down','node':'south-replica'},{'at_ms':900,'kind':'read','session':'lab-A'},{'at_ms':1000,'kind':'node_down','node':'harbor-primary'},{'at_ms':1100,'kind':'read','session':'lab-A'}]
        cases=[('QY-1117-A','交接班写后读与双节点故障',mixed),('QY-1117-B','延迟超标回退主库',[{'at_ms':0,'kind':'node_down','node':'south-replica'},{'at_ms':10,'kind':'read','session':'visitor'}]),('QY-1117-C','双副本不可读时拒绝',[{'at_ms':0,'kind':'write','session':'lab-B'},{'at_ms':1000,'kind':'node_down','node':'harbor-primary'},{'at_ms':1100,'kind':'read','session':'lab-B'}])]
        for code,title,events in cases:
            if not db.scalar(select(RequestSet).where(RequestSet.code==code)):db.add(RequestSet(code=code,title=title,events=events,note='栖原站点值班交接 · 09:15 请求轨迹'))
        seed_retention_templates(db)
        db.commit();print({cls.__tablename__:db.scalar(select(func.count()).select_from(cls)) for cls in (User,Node,RequestSet,Trial,RetentionTemplate)})

def seed_retention_templates(db):
    two_replicas=[{'name':'north-replica','ack_position':0,'online':True},{'name':'south-replica','ack_position':0,'online':True}]
    retention_cases=[
        ('RT-WRITE-FULL','写满硬上限与写入阻塞',100,40,0,40,'block',two_replicas,
         [{'at_ms':i,'kind':'write'} for i in range(4)],
         '配额100字节、每段40字节：第三次写入后达80%，第四次触发硬上限阻塞，展示 block 策略牺牲写入可用性的代价'),
        ('RT-OFFLINE-CATCHUP','离线副本钉住与恢复追赶',1000,10,2,10,'block',
         [{'name':'north-replica','ack_position':0,'online':True},{'name':'south-replica','ack_position':2,'online':False}],
         [{'at_ms':i,'kind':'write'} for i in range(4)]+[
             {'at_ms':10,'kind':'replica_online','node':'south-replica'},
             {'at_ms':11,'kind':'replica_ack','node':'south-replica','position':6},
             {'at_ms':12,'kind':'replica_ack','node':'north-replica','position':6},
             {'at_ms':13,'kind':'cleanup','action':'start'},
         ],
         '南站离线期间位点2持续钉住保留空间；恢复后应用仍保留的日志追平，清理边界才得以推进'),
        ('RT-CLEANUP-RACE','清理竞争、中断恢复与越界重建',1000,10,0,10,'abandon',
         [{'name':'north-replica','ack_position':0,'online':True},{'name':'south-replica','ack_position':0,'online':False}],
         [{'at_ms':i,'kind':'write'} for i in range(6)]+[
             {'at_ms':10,'kind':'replica_ack','node':'north-replica','position':6},
             {'at_ms':11,'kind':'replica_online','node':'south-replica'},
             {'at_ms':12,'kind':'replica_ack','node':'south-replica','position':6},
             {'at_ms':13,'kind':'replica_offline','node':'south-replica'},
             {'at_ms':20,'kind':'cleanup','action':'start','limit':2},
             {'at_ms':21,'kind':'write'},
             {'at_ms':22,'kind':'cleanup','action':'interrupt'},
             {'at_ms':23,'kind':'cleanup','action':'resume','limit':2},
             {'at_ms':24,'kind':'cleanup','action':'resume','limit':2},
             {'at_ms':30,'kind':'abandon_replica','node':'south-replica'},
             {'at_ms':31,'kind':'rebuild_replica','node':'south-replica'},
         ],
         '清理与写入并发：受保护段不误删；中断恢复按台账幂等续跑；放弃越界副本后须快照重建才恢复选路'),
    ]
    for code,title,quota,write_size,p0,seg_size,policy,replicas,events,note in retention_cases:
        if not db.scalar(select(RetentionTemplate).where(RetentionTemplate.code==code)):
            db.add(RetentionTemplate(code=code,title=title,space_quota_bytes=quota,
                write_size_bytes=write_size,initial_primary_position=p0,
                initial_segment_size=seg_size,space_policy=policy,replicas=replicas,
                events=events,note=note))
if __name__=='__main__':seed()
