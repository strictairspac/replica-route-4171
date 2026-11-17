"""服务层集成测试：在导入前注入 SQLAlchemy/FastAPI 桩，纯标准库验证业务链路。

运行：python3 -I tests/test_retention_services.py
真实 MySQL/FastAPI 装配由 docker compose 环境承担，本测试只覆盖服务函数逻辑。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---- 最小第三方桩：仅实现被测模块实际引用的符号 ----
sqlalchemy = types.ModuleType('sqlalchemy')
sqlalchemy.select = lambda *a, **k: ('select', a)


def _column(*a, **k):
    return None


sqlalchemy.Column = _column


class _Callable:
    def __call__(self, *a, **k):
        return None


sqlalchemy.Integer = sqlalchemy.String = sqlalchemy.JSON = _Callable()
sqlalchemy.DateTime = sqlalchemy.Text = sqlalchemy.ForeignKey = _Callable()
sys.modules['sqlalchemy'] = sqlalchemy
sys.modules['sqlalchemy.exc'] = types.ModuleType('sqlalchemy.exc')
sys.modules['sqlalchemy.exc'].IntegrityError = type('IntegrityError', (Exception,), {})

fastapi = types.ModuleType('fastapi')


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__('%s %s' % (status_code, detail))
        self.status_code = status_code
        self.detail = detail


fastapi.HTTPException = HTTPException
fastapi.APIRouter = type('APIRouter', (), {'__init__': lambda self, *a, **k: None})
fastapi.Depends = lambda *a, **k: None
sys.modules['fastapi'] = fastapi
sys.modules['fastapi.responses'] = types.ModuleType('fastapi.responses')
sys.modules['fastapi.responses'].Response = object

pydantic = types.ModuleType('pydantic')


class BaseModel:
    def __init__(self, **data):
        for k, v in data.items():
            setattr(self, k, v)

    def model_dump(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


def Field(default=None, **k):
    return default


def model_validator(*a, **k):
    def deco(fn):
        return fn
    return deco


pydantic.BaseModel = BaseModel
pydantic.Field = Field
pydantic.model_validator = model_validator
sys.modules['pydantic'] = pydantic
typing_mod = types.ModuleType('typing')
import typing
sys.modules.setdefault('typing', typing)

# 项目内部配置桩
class _StubBase:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


config_db = types.ModuleType('src.config.database')
config_db.Base = type('Base', (_StubBase,), {
    'metadata': type('md', (), {'create_all': staticmethod(lambda *a, **k: None)})()})
config_db.engine = object()
config_db.Session = None
config_db.db_session = lambda: None
sys.modules['src.config.database'] = config_db
sys.modules['src.config.settings'] = types.ModuleType('src.config.settings')
sys.modules['src.config.settings'].DATABASE_URL = 'stub'
sys.modules['src.config.settings'].REDIS_URL = 'stub'
sys.modules['src.config.settings'].JWT_SECRET = 'stub'
security = types.ModuleType('src.utils.security')
security.current = security.writer = security.admin = lambda: None
security.password_hash = lambda v, salt=None: 'x'
security.check_password = lambda a, b: True
security.issue = lambda u: 't'
sys.modules['src.utils.security'] = security

from src.services import retention_templates as tpl_service  # noqa: E402
from src.services import retention_drills as drill_service  # noqa: E402
from src.services.retention_engine import retain  # noqa: E402
from src.schemas.retention import RetentionTemplateInput, RetentionDrillInput  # noqa: E402


class User:
    def __init__(self, email='operator@qiyuan.test'):
        self.email = email


class _Col:
    def __init__(self, name):
        self.name = name


class Template:
    def __init__(self, payload):
        self.id = 1
        for k, v in payload.items():
            setattr(self, k, v)
        self.__table__ = type('table', (), {'columns': [_Col(k) for k in payload]})()


class StubDB:
    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def add(self, obj):
        if getattr(obj, 'id', None) is None:
            obj.id = self.next_id
            self.next_id += 1
        self.rows[obj.id] = obj

    def get(self, model, ident):
        return self.rows.get(ident)

    def commit(self):
        pass

    def rollback(self):
        pass

    def scalars(self, stmt):
        class Result:
            def __init__(self, items):
                self.items = items

            def all(self_inner):
                return self_inner.items
        return Result(list(self.rows.values()))

    def scalar(self, stmt):
        return None


# 桩掉 common.save 对 Audit ORM 的依赖
def fake_save(db, obj, user, action):
    if hasattr(obj, '__table__'):
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    return obj.__dict__.copy()


tpl_service.save = fake_save
drill_service.save = fake_save


def valid_payload(**over):
    payload = {
        'code': 'RT-TEST-1', 'title': '服务层链路模板',
        'space_quota_bytes': 100, 'write_size_bytes': 30,
        'initial_primary_position': 0, 'initial_segment_size': 30,
        'space_policy': 'block',
        'replicas': [{'name': 'r1', 'ack_position': 0, 'online': True}],
        'events': [{'at_ms': i, 'kind': 'write'} for i in range(4)],
        'note': '测试',
    }
    payload.update(over)
    return payload


def test_template_create_validates_before_save():
    db = StubDB()
    data = RetentionTemplateInput(**valid_payload())
    saved = tpl_service.create(db, data, User())
    assert saved['code'] == 'RT-TEST-1'
    # 非法时序不入台账：模拟引擎报错时 HTTPException
    bad = valid_payload(events=[
        {'at_ms': 5, 'kind': 'write'}, {'at_ms': 3, 'kind': 'write'}])
    bad_db = StubDB()
    try:
        tpl_service.create(bad_db, RetentionTemplateInput(**bad), User())
        raise AssertionError('非法时序应被 422 拒绝')
    except HTTPException as e:
        assert e.status_code == 422 and '非递减' in e.detail
    assert len(bad_db.rows) == 0, '校验失败不得写入任何台账行'


def test_drill_freezes_snapshot_and_result():
    db = StubDB()
    tpl = Template(valid_payload())
    db.add(tpl)
    data = RetentionDrillInput(name='drill-night', retention_template_id=1)
    saved = drill_service.create(db, data, User())
    assert saved['status'] == 'completed'
    assert saved['input_snapshot']['template']['code'] == 'RT-TEST-1'
    assert saved['result']['summary']['writes_blocked'] == 1
    again = retain(saved['input_snapshot']['config'], saved['input_snapshot']['events'])
    assert again['checksum'] == saved['result']['checksum']


def test_drill_missing_template_404():
    try:
        drill_service.create(StubDB(),
                             RetentionDrillInput(name='x', retention_template_id=99), User())
        raise AssertionError('缺失模板应 404')
    except HTTPException as e:
        assert e.status_code == 404


def test_seed_scenarios_replay():
    two = [{'name': 'north-replica', 'ack_position': 0, 'online': True},
           {'name': 'south-replica', 'ack_position': 0, 'online': True}]
    scenarios = [
        (100, 40, 0, 40, 'block', two,
         [{'at_ms': i, 'kind': 'write'} for i in range(4)]),
        (1000, 10, 2, 10, 'block',
         [{'name': 'north-replica', 'ack_position': 0, 'online': True},
          {'name': 'south-replica', 'ack_position': 2, 'online': False}],
         [{'at_ms': i, 'kind': 'write'} for i in range(4)] + [
             {'at_ms': 10, 'kind': 'replica_online', 'node': 'south-replica'},
             {'at_ms': 11, 'kind': 'replica_ack', 'node': 'south-replica', 'position': 6},
             {'at_ms': 12, 'kind': 'replica_ack', 'node': 'north-replica', 'position': 6},
             {'at_ms': 13, 'kind': 'cleanup', 'action': 'start'}]),
        (1000, 10, 0, 10, 'abandon',
         [{'name': 'north-replica', 'ack_position': 0, 'online': True},
          {'name': 'south-replica', 'ack_position': 0, 'online': False}],
         [{'at_ms': i, 'kind': 'write'} for i in range(6)] + [
             {'at_ms': 10, 'kind': 'replica_ack', 'node': 'north-replica', 'position': 6},
             {'at_ms': 11, 'kind': 'replica_online', 'node': 'south-replica'},
             {'at_ms': 12, 'kind': 'replica_ack', 'node': 'south-replica', 'position': 6},
             {'at_ms': 13, 'kind': 'replica_offline', 'node': 'south-replica'},
             {'at_ms': 20, 'kind': 'cleanup', 'action': 'start', 'limit': 2},
             {'at_ms': 21, 'kind': 'write'},
             {'at_ms': 22, 'kind': 'cleanup', 'action': 'interrupt'},
             {'at_ms': 23, 'kind': 'cleanup', 'action': 'resume', 'limit': 2},
             {'at_ms': 24, 'kind': 'cleanup', 'action': 'resume', 'limit': 2},
             {'at_ms': 30, 'kind': 'abandon_replica', 'node': 'south-replica'},
             {'at_ms': 31, 'kind': 'rebuild_replica', 'node': 'south-replica'}]),
    ]
    for quota, wsize, p0, ssize, policy, replicas, events in scenarios:
        cfg = {'space_quota_bytes': quota, 'write_size_bytes': wsize,
               'initial_primary_position': p0, 'initial_segment_size': ssize,
               'space_policy': policy, 'replicas': replicas}
        r = retain(cfg, events)
        assert r['checksum']
        assert not r['summary']['rebuild_required'], '种子场景结束时不应遗留未重建副本'

    # 写满场景的关键代价断言：配额100、每段40，仅容纳2段，后两次写入被硬上限阻塞
    full_quota, full_size, _, full_seg, full_policy, _, full_events = scenarios[0]
    r = retain({'space_quota_bytes': full_quota, 'write_size_bytes': full_size,
                'initial_primary_position': 0, 'initial_segment_size': full_seg,
                'space_policy': full_policy,
                'replicas': [{'name': 'solo', 'ack_position': 0, 'online': True}]},
               full_events)
    assert r['summary']['writes_accepted'] == 2
    assert r['summary']['writes_blocked'] == 2
    assert r['summary']['peak_utilization_pct'] == 80.0


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in tests:
        fn()
        print('PASS', fn.__name__)
    print('\n全部 %d 项保留服务层测试通过' % len(tests))
