// 保留演练前端页面渲染冒烟测试（无浏览器依赖，Node ESM 直接运行）：
//   node tests/retention_page.test.mjs
import assert from 'node:assert';
import {retentionPage, retentionEvidencePage} from '../src/pages/retention.js';

globalThis.localStorage = {
  getItem: k => k === 'replicaroute-user'
    ? JSON.stringify({email: 'operator@qiyuan.test', role: 'operator'}) : null,
  setItem(){}, removeItem(){},
};

const templates = [{
  id: 1, code: 'RT-CLEANUP-RACE', title: '清理竞争演练', note: 'n',
  space_quota_bytes: 1000, write_size_bytes: 10, initial_primary_position: 0,
  initial_segment_size: 10, space_policy: 'abandon',
  replicas: [{name: 'south-replica', ack_position: 0, online: false}],
  events: [{at_ms: 0, kind: 'write'}, {at_ms: 1, kind: 'cleanup', action: 'start'}],
}];
const drill = {
  id: 7, name: 'drill-7', status: 'completed', created_at: new Date().toISOString(),
  retention_template_id: 1,
  result: {
    policy: 'abandon', mode: '受控模型',
    summary: {
      writes_total: 4, writes_accepted: 3, writes_blocked: 1, bytes_written: 40,
      bytes_freed: 20, peak_used_bytes: 90, quota_bytes: 100, peak_utilization_pct: 90,
      retained_bytes_final: 70, protected_bytes_final: 50, abandoned: 1, rebuilds: 1,
      cleanup_task_count: 1, rebuild_required: ['south-replica'], abandoned_open: [],
      routing_eligible: ['north-replica'],
      cost_note: '阻塞写入次数=1；放弃追赶副本=1；快照重建=1；清理释放=20字节',
    },
    trace: [
      {index: 1, at_ms: 0, kind: 'write', node: null, decision: 'applied', reason: '写入',
       used_bytes: 40, quota_bytes: 100, protected_bytes: 40, cleanup_status: null},
      {index: 2, at_ms: 1, kind: 'cleanup', node: null, decision: 'info', reason: '清理完成',
       used_bytes: 20, quota_bytes: 100, protected_bytes: 20, cleanup_status: 'completed'},
      {index: 3, at_ms: 2, kind: 'write', node: null, decision: 'blocked', reason: '硬上限',
       used_bytes: 20, quota_bytes: 100, protected_bytes: 20, cleanup_status: null},
    ],
    final_segments: [{position: 3, size_bytes: 20}],
    final_replicas: [
      {name: 'north-replica', ack_position: 3, online: true, status: 'ready',
       routing_eligible: true, rebuilds: 1},
      {name: 'south-replica', ack_position: 0, online: false, status: 'rebuild_required',
       routing_eligible: false, rebuilds: 0},
    ],
    cleanup_tasks: [{
      id: 1, status: 'completed', attempts: 2,
      history: [{status: 'pending'}, {status: 'running'}, {status: 'interrupted'},
                 {status: 'running'}, {status: 'completed'}],
      deletions: [{position: 1, size_bytes: 10, reason: '确认回收'},
                  {position: 2, size_bytes: 10, reason: '确认回收'}],
      freed_bytes: 20, freed_count: 2,
      retention_basis: [{position: 3, size_bytes: 20, pinned_by: ['south-replica']}],
      attempt_log: [],
    }],
    auto_reclaims: [], checksum: 'abc123',
  },
};

// 列表/配置页
const listHtml = retentionPage({templates, drills: [drill]});
assert(listHtml.includes('RT-CLEANUP-RACE'), '应渲染模板编号');
assert(listHtml.includes('abandon · 写满放弃最落后离线副本'), '应渲染策略选项');
assert(listHtml.includes('drill-7'), '应渲染演练记录');
assert(listHtml.includes('三条去路与代价'), '应展示取舍说明');
assert(listHtml.includes('retention-form') && listHtml.includes('drill-form'), '应包含两个表单');
assert(!listHtml.includes('disabled'), 'operator 不应被禁用按钮');

// 证据页
const evHtml = retentionEvidencePage(drill);
assert(evHtml.includes('发生 1 次写入阻塞'), '应展示阻塞代价');
assert(evHtml.includes('width:90%'), '应按峰值占用渲染容量条');
assert(evHtml.includes('阻塞写入'), '阻塞决策应有标记');
assert(evHtml.includes('待重建'), '越界副本应显示待重建状态');
assert(evHtml.includes('摘除'), '越界副本应显示选路摘除');
assert(evHtml.includes('参与选路'), '就绪副本应显示参与选路');
assert(evHtml.includes('1→10B') && evHtml.includes('2→10B'), '应渲染清理删除台账');
assert(evHtml.includes('段3: south-replica'), '应渲染完成当时的保留依据');
assert(evHtml.includes('待执行 → 执行中 → 已中断 → 执行中 → 已完成'), '应渲染完整任务状态链');
assert(evHtml.includes('abc123'), '应展示确定性摘要');
assert(evHtml.includes('retention-export'), '应提供导出按钮');

// viewer 只读
globalThis.localStorage.getItem = k => k === 'replicaroute-user'
  ? JSON.stringify({email: 'viewer@qiyuan.test', role: 'viewer'}) : null;
const viewerHtml = retentionPage({templates, drills: []});
assert(viewerHtml.includes('disabled'), 'viewer 的写按钮应禁用');

// 空态
assert(retentionEvidencePage(null).includes('还没有保留演练证据'), '空态应有提示');

console.log('PASS 保留演练前端页面渲染（列表/证据/只读/空态）');
