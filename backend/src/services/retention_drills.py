from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import RetentionTemplate, RetentionDrill
from src.utils.serialization import record
from src.services.retention_engine import retain
from src.services.retention_templates import config_of
from .common import save


def listing(db):
    return [record(x) for x in db.scalars(
        select(RetentionDrill).order_by(RetentionDrill.id.desc()))]


def detail(db, ident):
    obj = db.get(RetentionDrill, ident)
    if not obj:
        raise HTTPException(404, '保留演练不存在')
    return record(obj)


def create(db, data, user):
    tpl = db.get(RetentionTemplate, data.retention_template_id)
    if not tpl:
        raise HTTPException(404, '保留模板不存在')
    config = config_of(tpl)
    snapshot = {'config': config, 'events': tpl.events, 'template': record(tpl)}
    # 同步确定性重放：纯函数、无外部依赖；模板保存时已预演，这里再次冻结结果
    try:
        result = retain(config, tpl.events)
    except ValueError as e:
        raise HTTPException(422, str(e))
    obj = RetentionDrill(
        name=data.name, retention_template_id=tpl.id,
        status='completed', input_snapshot=snapshot, result=result,
    )
    return save(db, obj, user, '复制日志保留演练 ' + data.name)
