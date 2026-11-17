from typing import Literal
from pydantic import BaseModel, Field, model_validator


class RetentionReplica(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    ack_position: int = Field(ge=0, le=1000000000)
    online: bool = True


class RetentionEvent(BaseModel):
    at_ms: int = Field(ge=0, le=86400000)
    kind: Literal[
        'write', 'replica_offline', 'replica_online', 'replica_ack',
        'abandon_replica', 'rebuild_replica', 'cleanup',
    ]
    node: str | None = Field(default=None, max_length=80)
    position: int | None = Field(default=None, ge=0, le=1000000000)
    size_bytes: int | None = Field(default=None, gt=0, le=1000000000)
    action: Literal['start', 'interrupt', 'resume'] | None = None
    limit: int | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode='after')
    def valid_event(self):
        if self.kind in ('replica_offline', 'replica_online', 'replica_ack',
                         'abandon_replica', 'rebuild_replica') and not self.node:
            raise ValueError('副本事件需要 node')
        if self.kind == 'replica_ack' and self.position is None:
            raise ValueError('replica_ack 需要 position')
        if self.kind == 'cleanup' and not self.action:
            raise ValueError('cleanup 需要 action（start/interrupt/resume）')
        if self.kind != 'cleanup' and (self.action is not None or self.limit is not None):
            raise ValueError('action/limit 仅属于 cleanup 事件')
        if self.kind != 'write' and self.size_bytes is not None:
            raise ValueError('size_bytes 仅属于 write 事件')
        if self.kind != 'replica_ack' and self.position is not None:
            raise ValueError('position 仅属于 replica_ack 事件')
        return self


class RetentionTemplateInput(BaseModel):
    code: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=2, max_length=160)
    space_quota_bytes: int = Field(gt=0, le=1000000000)
    write_size_bytes: int = Field(gt=0, le=1000000000)
    initial_primary_position: int = Field(default=0, ge=0, le=1000000)
    initial_segment_size: int | None = Field(default=None, gt=0, le=1000000000)
    space_policy: Literal['block', 'abandon']
    replicas: list[RetentionReplica] = Field(min_length=1, max_length=8)
    events: list[RetentionEvent] = Field(min_length=1, max_length=100)
    note: str = Field(default='', max_length=300)

    @model_validator(mode='after')
    def chronological(self):
        if any(a.at_ms > b.at_ms for a, b in zip(self.events, self.events[1:])):
            raise ValueError('事件必须按 at_ms 非递减排列')
        if len({r.name for r in self.replicas}) != len(self.replicas):
            raise ValueError('副本名必须唯一')
        if any(r.ack_position > self.initial_primary_position for r in self.replicas):
            raise ValueError('副本初始位点不能超过主库初始位点')
        return self


class RetentionDrillInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    retention_template_id: int = Field(gt=0)
