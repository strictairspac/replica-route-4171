"""复制日志保留演练：带大小日志、硬容量上限、离线副本保留钉住与清理任务台账的确定性重放。

与 routing.simulate 同级的纯函数模型：不连接真实主从集群、无随机数、无外部网络。
输入非法抛 ValueError，由路由层转 422；相同输入产生相同摘要。

核心口径
--------
- 每次 write 产生一个单调整数位点的日志段，携带 size_bytes；保留段总字节不得超过硬配额。
- 副本持有追赶位点 ack：已应用到 ack，需要 ack+1 起的日志。离线副本仍钉住保留空间。
- 保护口径：日志段 position=p 被保留，当且仅当存在「追赶中」副本 ack < p（pinned 非空）。
- 清理只删除当前无任何追赶中副本钉住的段；放弃/重建状态副本不参与确认，删除走唯一通道并二次校验。
- 所需日志已被清理的副本越过清理边界，置 rebuild_required，摘除选路资格，直到快照重建。
- 清理任务跨 pending/running/interrupted/resume/completed，删除按段记账，中断恢复幂等不重复释放；
  任务完成时逐段留存当时的保留依据（钉住副本清单），随演练结果持久化，旧记录可回看。
"""
import copy
import hashlib
import json

CATCHING = 'catching_up'      # 正常追赶中，位点参与保留钉住
ABANDONED = 'abandoned'       # 已放弃追赶，钉住解除，等待重建判定
REBUILD = 'rebuild_required'  # 越过清理边界，必须重建后才能参与选路
READY = 'ready'               # 已快照重建并对齐主库，可参与选路
ROUTING_STATES = (CATCHING, READY)

POLICIES = ('block', 'abandon')
EVENT_KINDS = (
    'write', 'replica_offline', 'replica_online', 'replica_ack',
    'abandon_replica', 'rebuild_replica', 'cleanup',
)


def _snapshot_replicas(replicas):
    return [
        {
            'name': name,
            'ack_position': r['ack'],
            'online': r['online'],
            'status': r['status'],
            'routing_eligible': r['online'] and r['status'] in ROUTING_STATES,
            'rebuilds': r['rebuilds'],
        }
        for name, r in replicas.items()
    ]


def retain(config, events):
    """重放保留演练。config/dict 与 events/list 均为普通 JSON 数据。"""
    if not isinstance(config, dict):
        raise ValueError('保留配置格式不正确')
    quota = config.get('space_quota_bytes')
    write_size = config.get('write_size_bytes')
    policy = config.get('space_policy')
    p0 = config.get('initial_primary_position', 0)
    seg_size = config.get('initial_segment_size', write_size)
    replica_inputs = config.get('replicas', [])
    if not isinstance(quota, int) or quota <= 0:
        raise ValueError('空间硬上限 space_quota_bytes 必须为正整数')
    if not isinstance(write_size, int) or write_size <= 0:
        raise ValueError('默认日志大小 write_size_bytes 必须为正整数')
    if not isinstance(seg_size, int) or seg_size <= 0:
        raise ValueError('初始日志段大小必须为正整数')
    if policy not in POLICIES:
        raise ValueError('空间策略必须是 block 或 abandon')
    if not isinstance(p0, int) or p0 < 0:
        raise ValueError('主库初始位点必须为非负整数')
    if not 1 <= len(replica_inputs) <= 8:
        raise ValueError('需要1–8个副本')
    names = [r.get('name') for r in replica_inputs]
    if len(set(names)) != len(names):
        raise ValueError('副本名必须唯一')
    if not 1 <= len(events) <= 100:
        raise ValueError('需要1–100个时序事件')

    # 初始日志：位点 1..p0，每段 seg_size 字节
    segments = {p: seg_size for p in range(1, p0 + 1)}
    replicas = {}
    for r in replica_inputs:
        ack, online = r.get('ack_position'), r.get('online', True)
        if not isinstance(ack, int) or not 0 <= ack <= p0:
            raise ValueError('副本初始位点必须在 0 与主库初始位点之间：%s' % r.get('name'))
        replicas[r['name']] = {
            'ack': ack, 'online': bool(online), 'status': CATCHING, 'rebuilds': 0,
        }
    used_bytes = sum(segments.values())
    if used_bytes > quota:
        raise ValueError('初始日志已超过空间硬上限，请调大配额或减少初始位点')

    primary_position = p0
    active_task = None
    tasks = []
    auto_reclaims = []
    counters = {
        'writes_total': 0, 'writes_accepted': 0, 'writes_blocked': 0,
        'bytes_written': 0, 'bytes_freed': 0, 'peak_used_bytes': used_bytes,
        'abandoned': 0, 'rebuilds': 0,
    }
    trace = []
    last_time = -1

    def pinned_by(pos):
        """仍需该日志段才能追上的副本，即该段的保留依据。"""
        return [name for name, r in replicas.items()
                if r['status'] == CATCHING and r['ack'] < pos]

    def protected_bytes():
        return sum(size for pos, size in segments.items() if pinned_by(pos))

    def reclaimable_positions():
        # 可回收 = 当前仍保留、且已无任何追赶中副本钉住的段；
        # 放弃/重建状态的副本不参与确认，其所需段在放弃解除钉住前同样不可回收
        return sorted(pos for pos in segments if not pinned_by(pos))

    def delete_segment(pos, reason, ledger):
        """唯一删除通道：二次确认无保留钉住，保证并发清理不会误删受保护日志。"""
        if pos not in segments:
            return False  # 幂等：中断恢复时已释放的段不会重复释放
        pins = pinned_by(pos)
        if pins:
            raise ValueError('受保护日志不能删除：位点 %s 仍被 %s 钉住' % (pos, '、'.join(pins)))
        size = segments.pop(pos)
        ledger.append({'position': pos, 'size_bytes': size, 'reason': reason})
        counters['bytes_freed'] += size
        nonlocal used_bytes
        used_bytes -= size
        return True

    def refresh_rebuild_states():
        """越过当前清理边界（最旧保留段）的放弃副本，转为必须重建。"""
        if not segments:
            frontier = primary_position + 1
        else:
            frontier = min(segments)
        for r in replicas.values():
            if r['status'] == ABANDONED and r['ack'] < frontier:
                r['status'] = REBUILD

    def reclaim_unprotected(basis, ledger, limit=None):
        freed = deleted = 0
        for pos in reclaimable_positions():
            if limit is not None and deleted >= limit:
                break
            before = used_bytes
            if delete_segment(pos, basis, ledger):
                deleted += 1
                freed += before - used_bytes
        refresh_rebuild_states()
        return freed, deleted

    def row(index, at, kind, event, decision, reason, node=None):
        return {
            'index': index + 1, 'at_ms': at, 'kind': kind,
            'node': node or event.get('node'), 'decision': decision, 'reason': reason,
            'used_bytes': used_bytes, 'quota_bytes': quota,
            'protected_bytes': protected_bytes(),
            'reclaimable_bytes': sum(segments[p] for p in reclaimable_positions()),
            'blocked_writes': counters['writes_blocked'],
            'cleanup_task_id': active_task['id'] if active_task else None,
            'cleanup_status': active_task['status'] if active_task else None,
            'replicas': _snapshot_replicas(replicas),
        }

    for index, event in enumerate(events):
        at, kind = event.get('at_ms'), event.get('kind')
        if not isinstance(at, int) or at < 0:
            raise ValueError('事件 at_ms 必须为非负整数')
        if at < last_time:
            raise ValueError('事件必须按 at_ms 非递减排列')
        last_time = at
        if kind not in EVENT_KINDS:
            raise ValueError('未知事件类型：%s' % kind)
        node = event.get('node')
        decision, reason = 'applied', ''

        if kind == 'write':
            counters['writes_total'] += 1
            size = event.get('size_bytes', write_size)
            if not isinstance(size, int) or size <= 0:
                raise ValueError('写入日志大小必须为正整数')
            if used_bytes + size <= quota:
                primary_position += 1
                segments[primary_position] = size
                used_bytes += size
                counters['writes_accepted'] += 1
                counters['bytes_written'] += size
                reason = '日志段写入并保留，位点推进至 %s' % primary_position
            elif policy == 'block':
                counters['writes_blocked'] += 1
                decision = 'blocked'
                reason = '保留空间达硬上限 %s 字节，写入被阻塞，主库位点不推进' % quota
            else:
                # abandon：按落后程度（ack 升序、名称字典序）依次放弃离线追赶副本
                candidates = sorted(
                    (name for name, r in replicas.items()
                     if r['status'] == CATCHING and not r['online']),
                    key=lambda n: (replicas[n]['ack'], n),
                )
                given_up = []
                while used_bytes + size > quota and candidates:
                    victim_name = candidates.pop(0)
                    replicas[victim_name]['status'] = ABANDONED
                    counters['abandoned'] += 1
                    ledger = []
                    freed, deleted = reclaim_unprotected(
                        '放弃副本 %s 的追赶，解除其钉住日志' % victim_name, ledger)
                    auto_reclaims.append({
                        'at_ms': at, 'replica': victim_name,
                        'freed_bytes': freed, 'freed_count': deleted, 'deletions': ledger,
                    })
                    given_up.append(victim_name)
                if used_bytes + size <= quota:
                    primary_position += 1
                    segments[primary_position] = size
                    used_bytes += size
                    counters['writes_accepted'] += 1
                    counters['bytes_written'] += size
                    reason = '为腾出空间放弃副本 %s 的追赶，写入继续；该副本须快照重建' % '、'.join(given_up)
                else:
                    counters['writes_blocked'] += 1
                    decision = 'blocked'
                    reason = '已放弃 %s 后空间仍不足，写入阻塞' % ('、'.join(given_up) or '无可放弃的离线副本')

        elif kind in ('replica_offline', 'replica_online'):
            rep = replicas.get(node)
            if not rep:
                raise ValueError('事件引用未知副本：%s' % node)
            rep['online'] = kind == 'replica_online'
            reason = '副本离线，其追赶位点 %s 继续钉住保留空间' % rep['ack'] if not rep['online'] \
                else '副本恢复在线，可继续应用仍保留的日志'
            if not rep['online']:
                decision = 'info'

        elif kind == 'replica_ack':
            rep = replicas.get(node)
            if not rep:
                raise ValueError('事件引用未知副本：%s' % node)
            target = event.get('position')
            if not isinstance(target, int):
                raise ValueError('replica_ack 需要整数 position')
            if not rep['online']:
                raise ValueError('离线副本不能应用日志，请先 replica_online：%s' % node)
            if rep['status'] in (ABANDONED, REBUILD):
                raise ValueError('副本 %s 已越过清理边界，须先 rebuild_replica 重建才能继续追赶' % node)
            if not rep['ack'] < target <= primary_position:
                raise ValueError('追赶位点必须单调且不超过主库已提交位点 %s' % primary_position)
            missing = [p for p in range(rep['ack'] + 1, target + 1) if p not in segments]
            if missing:
                rep['status'] = REBUILD
                raise ValueError(
                    '位点 %s 所需日志已被清理，副本 %s 越过清理边界，须重建后再参与选路'
                    % (missing[0], node))
            rep['ack'] = target
            reason = '副本应用保留日志至位点 %s，保留钉住随之后移' % target

        elif kind == 'abandon_replica':
            rep = replicas.get(node)
            if not rep:
                raise ValueError('事件引用未知副本：%s' % node)
            if rep['status'] != CATCHING:
                raise ValueError('仅追赶中的副本可以放弃：%s 当前 %s' % (node, rep['status']))
            rep['status'] = ABANDONED
            counters['abandoned'] += 1
            ledger = []
            freed, deleted = reclaim_unprotected(
                '人工放弃副本 %s 的追赶，解除其钉住日志' % node, ledger)
            auto_reclaims.append({
                'at_ms': at, 'replica': node,
                'freed_bytes': freed, 'freed_count': deleted, 'deletions': ledger,
            })
            decision = 'info'
            reason = '人工放弃 %s 追赶，释放 %s 字节；恢复去路为快照重建' % (node, freed)

        elif kind == 'rebuild_replica':
            rep = replicas.get(node)
            if not rep:
                raise ValueError('事件引用未知副本：%s' % node)
            if rep['status'] not in (ABANDONED, REBUILD):
                raise ValueError('副本 %s 未越界，无需重建' % node)
            rep['ack'] = primary_position
            rep['online'] = True
            rep['status'] = READY
            rep['rebuilds'] += 1
            counters['rebuilds'] += 1
            decision = 'info'
            reason = '快照重建（重取全量）后位点对齐主库 %s，恢复选路资格' % primary_position

        elif kind == 'cleanup':
            action = event.get('action', 'start')
            limit = event.get('limit')
            if limit is not None and (not isinstance(limit, int) or limit <= 0):
                raise ValueError('cleanup 的 limit 必须为正整数')
            if action == 'start':
                if active_task:
                    raise ValueError('已有进行中的清理任务 #%s，请先中断或恢复至完成' % active_task['id'])
                active_task = {
                    'id': len(tasks) + 1, 'status': 'running', 'attempts': 1,
                    'history': [
                        {'event_index': index + 1, 'status': 'pending'},
                        {'event_index': index + 1, 'status': 'running'},
                    ],
                    'attempt_log': [], 'deletions': [], 'freed_bytes': 0, 'freed_count': 0,
                    'started_event': index + 1, 'completed_event': None,
                    'retention_basis': [],
                }
                tasks.append(active_task)
                decision, reason = 'info', '清理任务入队（pending→running），开始扫描可回收段'
            elif action == 'interrupt':
                if not active_task or active_task['status'] != 'running':
                    raise ValueError('中断清理要求存在 running 状态的任务')
                active_task['status'] = 'interrupted'
                active_task['history'].append({'event_index': index + 1, 'status': 'interrupted'})
                decision, reason = 'info', '模拟清理中断：已释放空间保持有效，删除台账落盘，等待恢复'
            elif action == 'resume':
                if not active_task or active_task['status'] not in ('running', 'interrupted'):
                    raise ValueError('恢复清理要求存在 interrupted/running 状态的任务')
                if active_task['status'] == 'interrupted':
                    active_task['attempts'] += 1
                active_task['status'] = 'running'
                active_task['history'].append({'event_index': index + 1, 'status': 'running'})
                decision, reason = 'info', '清理任务恢复，从台账位点继续，已删段跳过不重复释放'
            else:
                raise ValueError('cleanup action 必须是 start/interrupt/resume')

            if active_task and active_task['status'] == 'running' and action in ('start', 'resume'):
                before_bytes, before_count = counters['bytes_freed'], len(active_task['deletions'])
                freed, deleted = reclaim_unprotected(
                    '所有追赶中副本位点均已确认该段，清理回收',
                    active_task['deletions'], limit)
                active_task['freed_bytes'] = sum(d['size_bytes'] for d in active_task['deletions'])
                active_task['freed_count'] = len(active_task['deletions'])
                # 恢复重试时，已删除段不在 segments 中，delete_segment 幂等返回 False；
                # deleted 只统计本次新释放的段，累计释放恒等于台账唯一段大小之和（见测试断言）
                active_task['attempt_log'].append({
                    'event_index': index + 1, 'action': action,
                    'freed_delta': counters['bytes_freed'] - before_bytes,
                    'deleted_delta': len(active_task['deletions']) - before_count,
                    'ledger_size_after': len(active_task['deletions']),
                })
                remaining = reclaimable_positions()
                if not remaining:
                    active_task['status'] = 'completed'
                    active_task['completed_event'] = index + 1
                    active_task['history'].append({'event_index': index + 1, 'status': 'completed'})
                    # 留存任务完成当时的保留依据：每个仍保留段由哪些副本钉住
                    active_task['retention_basis'] = [
                        {'position': pos, 'size_bytes': segments[pos],
                         'pinned_by': pinned_by(pos)}
                        for pos in sorted(segments)
                    ]
                    reason = '清理完成，释放 %s 字节；保留依据已随任务留档' % active_task['freed_bytes']
                elif limit is not None:
                    reason = '本次达到处理上限 %s 段（新释放 %s 段、%s 字节），任务保持 running 等待恢复' % (
                        limit, deleted, freed)
                else:
                    reason = '本次处理 %s 段、释放 %s 字节，仍有 %s 个可回收段，任务保持 running' % (
                        deleted, freed, len(remaining))
                refresh_rebuild_states()

        # 先在 active_task 仍指向该任务时生成台账行，再解除当前任务引用
        counters['peak_used_bytes'] = max(counters['peak_used_bytes'], used_bytes)
        trace.append(row(index, at, kind, event, decision, reason))
        if active_task and active_task['status'] == 'completed':
            active_task = None

    if active_task:
        raise ValueError('清理任务 #%s 未完成，请补充 interrupt/resume 事件' % active_task['id'])

    final_replicas = _snapshot_replicas(replicas)
    result = {
        'mode': '受控复制日志保留模型 / 非真实复制集群',
        'policy': policy,
        'config': copy.deepcopy(config),
        'trace': trace,
        'final_segments': [{'position': p, 'size_bytes': segments[p]} for p in sorted(segments)],
        'final_replicas': final_replicas,
        'cleanup_tasks': tasks,
        'auto_reclaims': auto_reclaims,
        'summary': {
            **counters,
            'quota_bytes': quota,
            'peak_utilization_pct': round(counters['peak_used_bytes'] * 100 / quota, 2),
            'retained_bytes_final': used_bytes,
            'protected_bytes_final': protected_bytes(),
            'rebuild_required': [n for n, r in replicas.items() if r['status'] == REBUILD],
            'abandoned_open': [n for n, r in replicas.items() if r['status'] == ABANDONED],
            'routing_eligible': [r['name'] for r in final_replicas if r['routing_eligible']],
            'cleanup_task_count': len(tasks),
            # 代价一览：阻塞牺牲写入可用性，放弃/重建牺牲副本连续性与运维成本
            'cost_note': '阻塞写入次数=%s；放弃追赶副本=%s；快照重建=%s；清理释放=%s字节' % (
                counters['writes_blocked'], counters['abandoned'],
                counters['rebuilds'], counters['bytes_freed']),
        },
    }
    result['checksum'] = hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result
