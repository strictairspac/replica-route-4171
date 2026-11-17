import {table} from '../components/common/table.js';
import {badge} from '../components/common/badge.js';
import {esc,date} from '../utils/html.js';
import {session} from '../hooks/session.js';
import {RETENTION_POLICIES, RETENTION_REPLICA_STATES, CLEANUP_STATES} from '../constants/enums.js';

const sampleEvents=[
  {at_ms:0,kind:'write'},
  {at_ms:10,kind:'replica_offline',node:'south-replica'},
  {at_ms:20,kind:'write'},
  {at_ms:30,kind:'replica_online',node:'south-replica'},
  {at_ms:40,kind:'replica_ack',node:'south-replica',position:3},
  {at_ms:50,kind:'cleanup',action:'start'},
];
const sampleReplicas=[
  {name:'north-replica',ack_position:0,online:true},
  {name:'south-replica',ack_position:0,online:true},
];

export function retentionPage(store){
  const templates=store.templates,drills=store.drills;
  const readonly=session.user.role==='viewer';
  return `<div class="split"><section>
    <div class="section-top"><h2>保留模板与演练记录</h2><span>QUOTA / PIN / CLEANUP LEDGER</span></div>
    <h3 class="sub-h">保留模板（配置 + 时序）</h3>
    ${table(['模板 / 策略','硬上限 / 段大小','副本 / 事件','操作'],templates.map(t=>[
      `<strong>${esc(t.code)}</strong><small>${esc(t.title)}</small><small>${esc(t.note)}</small>`,
      `${t.space_quota_bytes}B<small>每段 ${t.write_size_bytes}B · 初始位点 ${t.initial_primary_position}</small>`,
      `${t.replicas.length} 副本<small>${t.events.length} 事件</small>`,
      `<button class="small edit-retention" data-id="${t.id}" ${readonly?'disabled':''}>编辑</button>`,
    ]))}
    <h3 class="sub-h">演练记录（冻结快照与清理台账）</h3>
    ${table(['演练 / 时间','模板','状态 / 代价','证据'],drills.map(d=>{
      const tpl=templates.find(t=>t.id===d.retention_template_id);
      const cost=d.result?.summary?.cost_note||'—';
      return [`<strong>${esc(d.name)}</strong><small>${date(d.created_at)}</small>`,
        esc(tpl?.code||'# '+d.retention_template_id),
        `${badge(d.status)}<small>${esc(cost)}</small>`,
        `<a href="#retention_evidence/${d.id}">查看 →</a>`];
    }))}
    <div class="protocol"><h3>三条去路与代价</h3><ol>
      <li><b>block 阻塞写入</b>：保留全部钉住日志，写满硬上限即拒绝新写入，主库位点不推进——保住副本恢复能力，牺牲写入可用性。</li>
      <li><b>abandon 放弃追赶</b>：写满时按落后程度放弃离线副本、解除其钉住空间，写入继续；副本所需日志一旦清理即越过边界。</li>
      <li><b>rebuild 快照重建</b>：越界副本摘除选路资格，重取全量对齐主库后才能恢复——代价是重建窗口与运维成本。</li>
    </ol></div>
    <div class="protocol"><h3>清理任务台账口径</h3><p>
      清理仅删除无任何追赶中副本钉住的段，删除走唯一通道并二次校验保护状态；任务 pending→running→（interrupted→resume）→completed，
      中断恢复按删除台账幂等续跑，已释放空间不重复计入；任务完成时逐段留存当时的保留依据（钉住副本清单），旧演练记录可随时回看。
    </p></div>
  </section>
  <section class="form-panel">
    <span class="eyebrow">RETENTION TEMPLATE</span>
    <h2 id="retention-heading">配置保留演练模板</h2>
    <form id="retention-form">
      <input type="hidden" name="id">
      <label>模板编号<input name="code" value="RT-20261117" required></label>
      <label>场景标题<input name="title" value="离线副本保留钉住演练" required></label>
      <div class="fields">
        <label>空间硬上限B<input name="space_quota_bytes" type="number" min="1" value="1000" required></label>
        <label>默认段大小B<input name="write_size_bytes" type="number" min="1" value="10" required></label>
      </div>
      <div class="fields">
        <label>主库初始位点<input name="initial_primary_position" type="number" min="0" value="0" required></label>
        <label>初始段大小B<input name="initial_segment_size" type="number" min="1" value="10"></label>
      </div>
      <label>空间策略<select name="space_policy">
        <option value="block">block · 写满阻塞写入</option>
        <option value="abandon">abandon · 写满放弃最落后离线副本</option>
      </select></label>
      <label>副本JSON（name / ack_position / online）<textarea name="replicas" rows="5" required>${esc(JSON.stringify(sampleReplicas,null,2))}</textarea></label>
      <label>时序事件JSON<textarea name="events" rows="12" required>${esc(JSON.stringify(sampleEvents,null,2))}</textarea></label>
      <label>备注<input name="note" value="保留 / 阻塞 / 放弃取舍演练"></label>
      <button class="primary" ${readonly?'disabled':''}>保存模板并预演校验 +</button>
    </form>
    <p class="help">保存时后端同步预演，非法时序或越界操作直接 422，不产生台账。事件类型见左侧口径。</p>
    <span class="eyebrow" style="margin-top:24px">RUN DRILL</span>
    <form id="drill-form">
      <label>演练名称<input name="name" value="retention-shift-20261117" required></label>
      <label>选择模板<select name="retention_template_id">
        ${templates.map(t=>`<option value="${t.id}">${esc(t.code)} · ${esc(t.title)}</option>`).join('')}
      </select></label>
      <button class="primary" ${readonly||!templates.length?'disabled':''}>冻结快照并执行演练 ↗</button>
    </form>
  </section></div>`;
}

export function retentionEvidencePage(drill){
  if(!drill)return '<div class="empty">还没有保留演练证据。先在「日志保留」页保存模板并执行一次演练。</div>';
  const r=drill.result;
  return `<div class="evidence-top"><div>
      <span class="eyebrow">RETENTION EVIDENCE / #${drill.id}</span>
      <h2>${esc(drill.name)} ${badge(drill.status)}</h2>
      <p>${date(drill.created_at)} · 配额与事件快照已冻结</p>
    </div>
    <button id="retention-export" data-id="${drill.id}">导出 JSON ↓</button></div>
  ${drill.error?`<div class="error">${esc(drill.error)}</div>`:''}
  ${r?renderResult(r):'<div class="notice">演练处理中。</div>'}`;
}

function renderResult(r){
  const s=r.summary;
  const util=Math.min(100,s.peak_utilization_pct);
  return `<div class="verdict"><div class="verdict-icon">${s.writes_blocked===0&&!s.rebuild_required.length?'✓':'!'}</div>
    <div><span class="eyebrow">RETENTION TRADE-OFF</span>
    <h2>${s.writes_blocked?`发生 ${s.writes_blocked} 次写入阻塞`:'写入未被阻塞'}${s.rebuild_required.length?` · ${s.rebuild_required.length} 副本待重建`:''}</h2>
    <p>${esc(s.cost_note)}</p></div>
    <div class="equation">${s.peak_used_bytes}<span> / ${s.quota_bytes} B</span></div></div>
  <div class="capacity-bar"><div style="width:${util}%"></div><small>峰值占用 ${s.peak_utilization_pct}% · 最终保留 ${s.retained_bytes_final}B · 其中受保护 ${s.protected_bytes_final}B</small></div>
  <div class="policy-strip">策略 ${RETENTION_POLICIES[r.policy]||r.policy} <span>／</span> 接受写入 ${s.writes_accepted}/${s.writes_total} <span>／</span> 清理任务 ${s.cleanup_task_count} <span>／</span> 释放 ${s.bytes_freed}B <span>／</span> 放弃 ${s.abandoned} · 重建 ${s.rebuilds}</div>

  <div class="section-top"><h2>逐事件保留证据</h2><span>每行记录用量、保护量与副本状态</span></div>
  ${table(['时间 / 事件','节点','决策','用量 / 受保护B','理由'],r.trace.map(e=>[
    `${e.at_ms}ms<small>${esc(e.kind)}${e.action?' · '+e.action:''}${e.limit?' · limit '+e.limit:''}</small>`,
    esc(e.node||'—'),
    e.decision==='blocked'?'<span class="rejected">阻塞写入</span>':esc(e.decision),
    `${e.used_bytes} / ${e.protected_bytes}<small>${e.cleanup_status?CLEANUP_STATES[e.cleanup_status]||e.cleanup_status:''}</small>`,
    esc(e.reason),
  ]))}

  <div class="section-top"><h2>副本追赶与选路资格</h2><span>越过清理边界须重建</span></div>
  ${table(['副本','追赶位点','在线','状态','选路资格','重建次数'],r.final_replicas.map(x=>[
    esc(x.name),x.ack_position,x.online?'在线':'离线',
    RETENTION_REPLICA_STATES[x.status]||x.status,
    x.routing_eligible?'<span class="ok">参与选路</span>':'<span class="rejected">摘除</span>',x.rebuilds,
  ]))}

  <div class="section-top"><h2>清理任务台账（中断恢复幂等）</h2><span>旧记录随演练持久化</span></div>
  ${r.cleanup_tasks.length?table(['任务','状态链 / 尝试次数','删除台账（位点→大小B→理由）','完成当时保留依据'],r.cleanup_tasks.map(t=>[
    `#${t.id}<small>释放 ${t.freed_bytes}B / ${t.freed_count}段</small>`,
    t.history.map(h=>CLEANUP_STATES[h.status]||h.status).join(' → ')+`<small>${t.attempts} 次尝试</small>`,
    t.deletions.map(d=>`<span class="attribute">${d.position}→${d.size_bytes}B</span>`).join(' ')||'—',
    t.retention_basis.length?t.retention_basis.map(b=>`<span class="attribute">段${b.position}: ${b.pinned_by.join('、')||'无钉住'}</span>`).join(' '):'任务未完成',
  ])):'<div class="empty">本演练没有清理任务。</div>'}

  <div class="evidence-notes"><section><h3>保留与清理口径</h3>
    <p>日志段 p 被保留当且仅当存在追赶中副本 ack &lt; p；离线副本的钉住同样生效。清理唯一删除通道在删除前二次校验钉住关系，并发写入的新日志不会被误删。</p>
    <p>${esc(r.mode)}</p></section>
  <section><h3>确定性摘要</h3>
    <p>相同输入产生相同结果；删除台账按段唯一，中断恢复累计释放恒等于台账大小之和。</p>
    <code>${esc(r.checksum)}</code></section></div>`;
}
