from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import RetentionTemplate
from src.utils.serialization import record
from src.services.retention_engine import retain
from .common import save


def listing(db):
    return [record(x) for x in db.scalars(
        select(RetentionTemplate).order_by(RetentionTemplate.id))]


def _normalize(payload):
    # initial_segment_size 为空时回退默认段大小；实体列为非空，落库前必须兜底
    payload = dict(payload)
    payload['initial_segment_size'] = (
        payload['initial_segment_size'] or payload['write_size_bytes'])
    return payload


def create(db, data, user):
    payload = _normalize(data.model_dump())
    # 入队前预演：非法时序/配置直接 422，不产生台账
    try:
        retain(_config_from_payload(payload), payload['events'])
    except ValueError as e:
        raise HTTPException(422, str(e))
    return save(db, RetentionTemplate(**payload), user, '创建保留演练模板 ' + data.code)


def update(db, ident, data, user):
    obj = db.get(RetentionTemplate, ident)
    if not obj:
        raise HTTPException(404, '保留模板不存在')
    payload = _normalize(data.model_dump())
    try:
        retain(_config_from_payload(payload), payload['events'])
    except ValueError as e:
        raise HTTPException(422, str(e))
    for k, v in payload.items():
        setattr(obj, k, v)
    return save(db, obj, user, '更新保留演练模板 ' + data.code)


def _config_from_payload(payload):
    return {
        'space_quota_bytes': payload['space_quota_bytes'],
        'write_size_bytes': payload['write_size_bytes'],
        'initial_primary_position': payload['initial_primary_position'],
        'initial_segment_size': payload['initial_segment_size'],
        'space_policy': payload['space_policy'],
        'replicas': payload['replicas'],
    }


def config_of(obj):
    return _config_from_payload({
        'space_quota_bytes': obj.space_quota_bytes,
        'write_size_bytes': obj.write_size_bytes,
        'initial_primary_position': obj.initial_primary_position,
        'initial_segment_size': obj.initial_segment_size,
        'space_policy': obj.space_policy,
        'replicas': obj.replicas,
    })
