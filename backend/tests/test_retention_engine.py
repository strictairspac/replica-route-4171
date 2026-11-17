"""复制日志保留演练引擎的纯标准库断言测试。

运行（在 backend 目录）：python3 -I tests/test_retention_engine.py
不依赖 pytest/MySQL/Redis，直接验证确定性重放模型的全部关键性质。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.retention_engine import retain, CATCHING, READY, REBUILD


def check(cond, message):
    if not cond:
        raise AssertionError(message)


def base(**over):
    cfg = {
        'space_quota_bytes': 1000, 'write_size_bytes': 10,
        'initial_primary_position': 0, 'space_policy': 'block',
        'replicas': [
            {'name': 'r1', 'ack_position': 0, 'online': True},
            {'name': 'r2', 'ack_position': 0, 'online': True},
        ],
    }
    cfg.update(over)
    return cfg


def test_write_full_blocks():
    """写满硬上限：block 策略下写入被阻塞，主库位点不推进，代价是阻塞次数。"""
    cfg = base(space_quota_bytes=100, write_size_bytes=30,
               replicas=[{'name': 'r1', 'ack_position': 0, 'online': True}])
    r = retain(cfg, [{'at_ms': i * 10, 'kind': 'write'} for i in range(4)])
    s = r['summary']
    check(s['writes_accepted'] == 3 and s['writes_blocked'] == 1, '应接受3次阻塞1次')
    check(len(r['final_segments']) == 3, '主库位点应停在3')
    check(s['peak_used_bytes'] == 90 and s['peak_utilization_pct'] == 90.0, '峰值用量计算错误')
    check(r['trace'][-1]['decision'] == 'blocked', '最后一次写入应标记 blocked')
    check('硬上限' in r['trace'][-1]['reason'], '阻塞理由应引用硬上限')


def test_offline_replica_pins_retention():
    """离线副本仍占用保留空间：它未确认的日志不能被清理回收。"""
    cfg = base(initial_primary_position=2, initial_segment_size=10, replicas=[
        {'name': 'r1', 'ack_position': 0, 'online': True},
        {'name': 'r2', 'ack_position': 2, 'online': False},
    ])
    events = [{'at_ms': i, 'kind': 'write'} for i in range(5)]
    events += [{'at_ms': 10, 'kind': 'replica_ack', 'node': 'r1', 'position': 7},
               {'at_ms': 20, 'kind': 'cleanup', 'action': 'start'}]
    r = retain(cfg, events)
    task = r['cleanup_tasks'][0]
    check([d['position'] for d in task['deletions']] == [1, 2], '只能清理两副本都确认的1、2段')
    check(sorted(x['position'] for x in r['final_segments']) == [3, 4, 5, 6, 7], 'r2钉住的段必须保留')
    check(r['summary']['protected_bytes_final'] == 50, '受保护字节应为50')
    basis = {b['position']: b['pinned_by'] for b in task['retention_basis']}
    check(basis == {3: ['r2'], 4: ['r2'], 5: ['r2'], 6: ['r2'], 7: ['r2']}, '保留依据应逐段记录钉住副本')
    offline_row = next(x for x in r['trace'] if x['kind'] == 'write')
    check(offline_row['replicas'][1]['online'] is False, '离线状态应进入每行快照')


def test_cleanup_concurrent_with_write_and_resume():
    """清理与写入并发：清理中新写入且被钉住的日志不能误删；中断恢复不重复释放。"""
    cfg = base()
    events = [{'at_ms': i, 'kind': 'write'} for i in range(6)]
    events += [
        {'at_ms': 10, 'kind': 'replica_ack', 'node': 'r1', 'position': 6},
        {'at_ms': 11, 'kind': 'replica_ack', 'node': 'r2', 'position': 6},
        {'at_ms': 20, 'kind': 'cleanup', 'action': 'start', 'limit': 2},
        {'at_ms': 21, 'kind': 'write'},  # 清理进行中并发写入，段7被两副本钉住
        {'at_ms': 22, 'kind': 'cleanup', 'action': 'interrupt'},
        {'at_ms': 23, 'kind': 'cleanup', 'action': 'resume', 'limit': 2},
        {'at_ms': 24, 'kind': 'cleanup', 'action': 'resume'},
        {'at_ms': 25, 'kind': 'replica_ack', 'node': 'r1', 'position': 7},
        {'at_ms': 26, 'kind': 'replica_ack', 'node': 'r2', 'position': 7},
    ]
    r = retain(cfg, events)
    task = r['cleanup_tasks'][0]
    # 台账唯一性 + 累计释放恒等：中断恢复没有重复释放任何空间
    positions = [d['position'] for d in task['deletions']]
    check(len(positions) == len(set(positions)) == 6, '清理台账应含6个唯一段')
    check(r['summary']['bytes_freed'] == 60, '累计释放应等于6段×10字节')
    check(sum(d['size_bytes'] for d in task['deletions']) == 60, '台账大小之和与释放量一致')
    deltas = [a['deleted_delta'] for a in task['attempt_log']]
    check(deltas == [2, 2, 2], '三次尝试删除增量应为2/2/2，重试不重复释放')
    check(task['attempts'] == 2, '中断后恢复应累计为2次尝试')
    check([h['status'] for h in task['history']]
          == ['pending', 'running', 'interrupted', 'running', 'running', 'completed'],
          '任务应经历完整 pending→completed 状态链')
    # 并发写入的段7始终受保护，清理边界没有越过它
    check([x['position'] for x in r['final_segments']] == [7], '清理中新写入的段7不能被误删')
    # 保留依据是完成当时的快照，事后位点推进也不改变旧记录
    basis = {b['position']: b['pinned_by'] for b in task['retention_basis']}
    check(basis == {7: ['r1', 'r2']}, '完成当时段7由两副本钉住')
    concurrent_write = next(x for x in r['trace'] if x['kind'] == 'write' and x['at_ms'] == 21)
    check(concurrent_write['cleanup_task_id'] == 1
          and concurrent_write['cleanup_status'] == 'running',
          '并发写入行应携带进行中的清理任务上下文')


def test_crossed_replica_rebuild_and_routing():
    """越过清理边界的副本：摘除选路资格，重建对齐后才能重新参与。"""
    cfg = base(replicas=[
        {'name': 'r1', 'ack_position': 0, 'online': True},
        {'name': 'r2', 'ack_position': 0, 'online': False},
    ])
    events = [{'at_ms': i, 'kind': 'write'} for i in range(4)]
    events += [
        {'at_ms': 10, 'kind': 'replica_ack', 'node': 'r1', 'position': 4},
        {'at_ms': 20, 'kind': 'abandon_replica', 'node': 'r2'},
    ]
    r = retain(cfg, events)
    r2 = next(x for x in r['final_replicas'] if x['name'] == 'r2')
    check(r2['status'] == REBUILD, '所需日志清理后应转为 rebuild_required')
    check(r2['routing_eligible'] is False, '越界副本必须摘除选路资格')
    check(r['summary']['rebuild_required'] == ['r2'], '汇总应列出待重建副本')
    # 未重建直接追赶必须被拒绝
    try:
        retain(cfg, events + [
            {'at_ms': 21, 'kind': 'replica_online', 'node': 'r2'},
            {'at_ms': 22, 'kind': 'replica_ack', 'node': 'r2', 'position': 4},
        ])
        raise AssertionError('越界副本未重建就追赶应当报错')
    except ValueError as e:
        check('重建' in str(e), '报错应明确指引重建')
    # 快照重建后位点对齐主库、恢复选路
    r2_done = retain(cfg, events + [
        {'at_ms': 30, 'kind': 'rebuild_replica', 'node': 'r2'},
    ])
    r2 = next(x for x in r2_done['final_replicas'] if x['name'] == 'r2')
    check(r2['status'] == READY and r2['ack_position'] == 4 and r2['routing_eligible'] is True,
          '重建后应对齐主库并恢复选路')
    check(r2_done['summary']['rebuilds'] == 1 and r2['rebuilds'] == 1, '重建次数应记账')


def test_abandon_policy_frees_space_but_costs_rebuild():
    """abandon 策略：写满时自动放弃最落后离线副本换取写入继续，代价是重建。"""
    cfg = base(space_quota_bytes=100, write_size_bytes=40,
               initial_primary_position=2, initial_segment_size=40,
               space_policy='abandon',
               replicas=[
                   {'name': 'r1', 'ack_position': 2, 'online': True},
                   {'name': 'r2', 'ack_position': 0, 'online': False},
               ])
    r = retain(cfg, [
        {'at_ms': 0, 'kind': 'write'},          # 120>100 → 放弃r2，释放80，写入段3
        {'at_ms': 1, 'kind': 'write'},          # 80，段4
        {'at_ms': 2, 'kind': 'write'},          # 再次超上限，已无离线副本可放弃 → 阻塞
        {'at_ms': 3, 'kind': 'rebuild_replica', 'node': 'r2'},
    ])
    s = r['summary']
    check(s['writes_accepted'] == 2 and s['writes_blocked'] == 1, '两次接受一次阻塞')
    check(s['abandoned'] == 1 and s['bytes_freed'] == 80, '应放弃r2并释放其钉住的80字节')
    check(len(r['auto_reclaims']) == 1 and r['auto_reclaims'][0]['replica'] == 'r2',
          '自动放弃应单独留账')
    check(sorted(x['position'] for x in r['final_segments']) == [3, 4], '旧段应已回收')
    check('r2' in s['routing_eligible'], '重建后应回到选路集合')
    check('放弃' in r['trace'][0]['reason'], '写入行应说明取舍与代价')


def test_offline_catchup_keeps_and_releases():
    """离线恢复后可应用仍保留的日志完成追赶，钉住随之后移再清理。"""
    cfg = base(initial_primary_position=1, initial_segment_size=10, replicas=[
        {'name': 'r1', 'ack_position': 0, 'online': True},
        {'name': 'r2', 'ack_position': 1, 'online': False},
    ])
    events = [{'at_ms': i, 'kind': 'write'} for i in range(3)]
    events += [
        {'at_ms': 10, 'kind': 'replica_online', 'node': 'r2'},
        {'at_ms': 11, 'kind': 'replica_ack', 'node': 'r2', 'position': 4},
        {'at_ms': 12, 'kind': 'replica_ack', 'node': 'r1', 'position': 4},
        {'at_ms': 13, 'kind': 'cleanup', 'action': 'start'},
    ]
    r = retain(cfg, events)
    check(r['summary']['bytes_freed'] == 40 and not r['final_segments'], '追平后可全部回收')
    check(r['cleanup_tasks'][0]['status'] == 'completed', '清理任务应完成')


def test_validation_errors():
    """非法输入在入队前拒绝，给出中文理由。"""
    def expect(cfg, events, part):
        try:
            retain(cfg, events)
            raise AssertionError('应拒绝：' + part)
        except ValueError as e:
            check(part in str(e), '错误信息应包含 %s，实际：%s' % (part, e))

    expect(base(space_policy='wait'), [{'at_ms': 0, 'kind': 'write'}], 'block')
    expect(base(space_quota_bytes=0), [{'at_ms': 0, 'kind': 'write'}], '硬上限')
    expect(base(), [{'at_ms': 5, 'kind': 'write'}, {'at_ms': 3, 'kind': 'write'}], '非递减')
    expect(base(), [{'at_ms': 0, 'kind': 'replica_ack', 'node': 'rx', 'position': 1}], '未知副本')
    expect(base(), [{'at_ms': 0, 'kind': 'replica_ack', 'node': 'r1', 'position': 9}], '主库已提交位点')
    expect(base(replicas=[
        {'name': 'r1', 'ack_position': 0, 'online': True},
        {'name': 'r1', 'ack_position': 0, 'online': True}]),
        [{'at_ms': 0, 'kind': 'write'}], '唯一')
    # 有可回收段时任务保持 running，重复 start / 无任务中断 / 收尾缺失都应被拒绝
    long_events = [{'at_ms': i, 'kind': 'write'} for i in range(4)]
    long_events += [{'at_ms': 9, 'kind': 'replica_ack', 'node': 'r1', 'position': 4},
                    {'at_ms': 10, 'kind': 'replica_ack', 'node': 'r2', 'position': 4},
                    {'at_ms': 11, 'kind': 'cleanup', 'action': 'start', 'limit': 1}]
    expect(base(), long_events + [{'at_ms': 12, 'kind': 'cleanup', 'action': 'start'}], '进行中')
    expect(base(), [{'at_ms': 0, 'kind': 'cleanup', 'action': 'interrupt'}], 'running')
    expect(base(), long_events, '未完成')


def test_determinism():
    """相同输入产生相同结果与摘要，不依赖随机数或外部状态。"""
    cfg = base()
    events = [{'at_ms': i, 'kind': 'write'} for i in range(4)]
    events += [{'at_ms': 9, 'kind': 'cleanup', 'action': 'start'}]
    a, b = retain(cfg, events), retain(cfg, events)
    check(a['checksum'] == b['checksum'], '摘要应一致')
    check(json.dumps(a, sort_keys=True, ensure_ascii=False)
          == json.dumps(b, sort_keys=True, ensure_ascii=False), '完整结果应一致')


def test_protected_segment_defense():
    """删除通道二次校验：任何被钉住的段都不会被清理理由删除。"""
    cfg = base(replicas=[{'name': 'r1', 'ack_position': 0, 'online': False}])
    r = retain(cfg, [
        {'at_ms': 0, 'kind': 'write'},
        {'at_ms': 1, 'kind': 'cleanup', 'action': 'start'},
    ])
    check(r['summary']['bytes_freed'] == 0, '离线副本钉住时清理不得释放任何空间')
    check(len(r['final_segments']) == 1, '受保护段必须仍在')


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in tests:
        fn()
        print('PASS', fn.__name__)
    print('\n全部 %d 项保留演练引擎测试通过' % len(tests))
