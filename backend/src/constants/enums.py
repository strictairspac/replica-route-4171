ROLES = ('admin', 'operator', 'viewer')
RUN_STATES = ('queued', 'running', 'completed', 'failed')
RETENTION_POLICIES = ('block', 'abandon')
RETENTION_EVENT_KINDS = (
    'write', 'replica_offline', 'replica_online', 'replica_ack',
    'abandon_replica', 'rebuild_replica', 'cleanup',
)
RETENTION_REPLICA_STATES = ('catching_up', 'abandoned', 'rebuild_required', 'ready')
CLEANUP_TASK_STATES = ('pending', 'running', 'interrupted', 'completed')
