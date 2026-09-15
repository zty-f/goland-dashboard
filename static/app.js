const state = { token: '', pollMs: 2000, paused: false, current: null, history: [], pending: null };
const $ = (id) => document.getElementById(id);
const fmt = (n, suffix = '') => n == null ? '--' : `${Number(n).toFixed(n >= 100 ? 0 : 1)}${suffix}`;
const riskLabel = { low: '低风险', medium: '中风险', high: '高风险', critical: '禁止停止' };

async function boot() {
  const config = await fetch('/api/config').then(r => r.json());
  state.token = config.action_token;
  state.pollMs = config.poll_ms;
  bindEvents();
  await refresh();
  setInterval(() => { if (!state.paused) refresh(); }, state.pollMs);
}

function bindEvents() {
  $('pauseButton').addEventListener('click', () => {
    state.paused = !state.paused;
    $('pauseButton').textContent = state.paused ? '继续刷新' : '暂停刷新';
    $('connection').textContent = state.paused ? '已暂停' : '实时连接';
  });
  $('highOnly').addEventListener('change', renderProcesses);
  $('ackInput').addEventListener('input', () => {
    $('confirmStop').disabled = $('ackInput').value !== $('ackExpected').textContent;
  });
  $('confirmStop').addEventListener('click', performStop);
}

async function refresh() {
  try {
    const data = await fetch('/api/status', { cache: 'no-store' }).then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
    state.current = data;
    const goland = data.goland[0];
    state.history.push({ at: Date.now(), cpu: goland?.cpu || 0, rss: goland?.rss_mb || 0, tools: data.groups.reduce((a, g) => a + g.cpu, 0) });
    state.history = state.history.slice(-120);
    $('connection').className = 'status-dot online';
    $('connection').textContent = '实时连接';
    render();
  } catch (error) {
    $('connection').className = 'status-dot offline';
    $('connection').textContent = '连接中断';
    showToast(`读取失败：${error.message}`);
  }
}

function render() {
  const d = state.current;
  const g = d.goland[0];
  const toolsCpu = d.groups.reduce((a, x) => a + x.cpu, 0);
  const toolsRss = d.groups.reduce((a, x) => a + x.rss_mb, 0);
  $('golandCpu').textContent = g ? fmt(g.cpu, '%') : '未运行';
  $('golandMeta').textContent = g ? `PID ${g.pid} · ${g.threads} 线程 · ${g.elapsed}` : '没有发现 GoLand 主进程';
  $('golandRss').textContent = g ? fmt(g.rss_mb, ' MB') : '--';
  $('heapMeta').textContent = d.telemetry.available ? `JVM 堆 ${fmt(d.telemetry.heap_percent, '%')} · 遥测 ${d.telemetry.age_seconds}s 前` : 'JVM 遥测不可用';
  $('toolsCpu').textContent = fmt(toolsCpu, '%');
  $('toolsMeta').textContent = `${d.processes.length} 个子进程 · ${fmt(toolsRss, ' MB')}`;
  $('freeMemory').textContent = fmt(d.system.free_mb, ' MB');
  $('compressedMemory').textContent = `压缩内存 ${fmt(d.system.compressed_mb, ' MB')}`;
  renderAlert(g, d.telemetry, toolsCpu);
  renderGroups();
  renderJvm();
  renderProcesses();
  drawChart();
}

function renderAlert(g, telemetry, toolsCpu) {
  const alerts = [];
  if (g?.cpu >= 250) alerts.push(`GoLand CPU 已达到 ${fmt(g.cpu, '%')}`);
  if (telemetry.heap_percent >= 90) alerts.push(`JVM 堆已使用 ${fmt(telemetry.heap_percent, '%')}`);
  if (toolsCpu >= 100) alerts.push(`插件外部工具合计 ${fmt(toolsCpu, '%')}`);
  const panel = $('alertPanel');
  panel.classList.toggle('hidden', !alerts.length);
  panel.textContent = alerts.length ? `高占用警告：${alerts.join('；')}` : '';
}

function renderGroups() {
  const root = $('groupList');
  if (!state.current.groups.length) { root.innerHTML = '<div class="boundary-note">没有发现 GoLand 子进程。</div>'; return; }
  root.innerHTML = state.current.groups.map(group => `
    <div class="group-row">
      <div class="group-name"><strong>${escapeHtml(group.label)}</strong><small>${group.count} 个进程 · ${group.name}${group.projects.length ? ` · ${group.projects.length} 个项目` : ''}${group.sessions.length ? ` · ${group.sessions.length} 个会话` : ''}</small></div>
      <div class="number ${heat(group.cpu)}">${fmt(group.cpu, '%')}</div>
      <div class="number">${fmt(group.rss_mb, ' MB')}</div>
      ${group.can_stop ? `<button class="button small" data-stop-group="${group.name}">停止整组</button>` : '<span class="risk critical">受保护</span>'}
    </div>`).join('');
  root.querySelectorAll('[data-stop-group]').forEach(button => button.addEventListener('click', () => openGroupDialog(button.dataset.stopGroup)));
}

function renderJvm() {
  const t = state.current.telemetry;
  const signals = Object.fromEntries(state.current.signals.map(x => [x.name, x.count]));
  const items = t.available ? [
    ['堆使用率', fmt(t.heap_percent, '%'), `${fmt(t.heap_used_mb, ' MB')} / ${fmt(t.heap_committed_mb, ' MB')}`],
    ['最近一分钟 GC', `${t.gc_count || 0} 次`, `累计 ${fmt((t.gc_ms || 0) / 1000, ' 秒')}`],
    ['最近一分钟分配', fmt((t.allocated_mb || 0), ' MB'), '对象分配吞吐'],
    ['JVM 线程', t.jvm_threads || '--', `进程内存 ${fmt(t.process_memory_mb, ' MB')}`],
    ['CodeBuddy 日志', signals.codebuddy || 0, '最近 5 分钟'],
    ['MarsCode 日志', signals.marscode || 0, '最近 5 分钟'],
    ['CC GUI 日志', signals.ccgui || 0, '最近 5 分钟'],
    ['低内存信号', signals.low_memory || 0, '最近 5 分钟'],
  ] : [['遥测不可用', '--', '等待 GoLand 生成 OpenTelemetry 数据']];
  $('jvmPanel').innerHTML = items.map(([label, value, note]) => `<div class="jvm-item"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join('');
}

function renderProcesses() {
  if (!state.current) return;
  const highOnly = $('highOnly').checked;
  const rows = state.current.processes.filter(p => !highOnly || p.cpu >= state.current.limits.high_cpu || p.rss_mb >= state.current.limits.high_rss_mb);
  $('processTable').innerHTML = rows.length ? rows.map(proc => `
    <tr>
      <td><div class="process-name"><strong>${escapeHtml(proc.group_label)} · ${escapeHtml(proc.role)}</strong><div class="process-purpose">${escapeHtml(proc.purpose)}</div>${identityHtml(proc)}<div class="command" title="${escapeHtml(proc.command)}">${escapeHtml(proc.command)}</div></div></td>
      <td class="number">${proc.pid}</td>
      <td class="number ${heat(proc.cpu)}">${fmt(proc.cpu, '%')}</td>
      <td class="number">${fmt(proc.rss_mb, ' MB')}</td>
      <td class="number">${proc.elapsed}</td>
      <td><span class="risk ${proc.risk}" title="${escapeHtml(proc.risk_text)}">${riskLabel[proc.risk]}</span></td>
      <td>${proc.can_stop ? `<button class="button small" data-stop-pid="${proc.pid}">停止</button>` : '<span class="risk critical">受保护</span>'}</td>
    </tr>`).join('') : '<tr><td colspan="7" class="boundary-note">当前筛选下没有进程。</td></tr>';
  $('processTable').querySelectorAll('[data-stop-pid]').forEach(button => button.addEventListener('click', () => openPidDialog(Number(button.dataset.stopPid))));
}

function openPidDialog(pid) {
  const proc = state.current.processes.find(p => p.pid === pid);
  if (!proc) return;
  state.pending = { scope: 'pid', id: pid, fingerprint: proc.fingerprint };
  const identity = [proc.purpose, proc.project_label ? `项目：${proc.project_label}` : '', proc.session_id ? `会话：${proc.session_title || proc.session_short_id} (${proc.session_id})` : '', proc.command].filter(Boolean).join('\n');
  openDialog(`停止 ${proc.group_label}`, proc.risk_text, identity, `STOP PID ${pid}`, proc.risk);
}

function openGroupDialog(name) {
  const group = state.current.groups.find(g => g.name === name);
  if (!group) return;
  state.pending = { scope: 'group', id: name };
  openDialog(`停止整组 ${group.label}`, `${group.risk_text} 本次将影响 ${group.count} 个进程。`, `PIDs: ${group.pids.join(', ')}`, `STOP GROUP ${name}`, group.risk);
}

function openDialog(title, risk, command, ack, level) {
  $('dialogTitle').textContent = title;
  $('dialogRisk').innerHTML = `<span class="risk ${level}">${riskLabel[level]}</span> ${escapeHtml(risk)}`;
  $('dialogCommand').textContent = command;
  $('ackExpected').textContent = ack;
  $('ackInput').value = '';
  $('confirmStop').disabled = true;
  $('stopDialog').showModal();
  setTimeout(() => $('ackInput').focus(), 80);
}

async function performStop() {
  if (!state.pending) return;
  $('confirmStop').disabled = true;
  try {
    const response = await fetch('/api/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Action-Token': state.token },
      body: JSON.stringify({ ...state.pending, ack: $('ackInput').value }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    $('stopDialog').close();
    showToast(`已向 ${result.stopped.length} 个进程发送 TERM。${result.note}`);
    setTimeout(refresh, 700);
    setTimeout(refresh, 3500);
  } catch (error) {
    showToast(`停止失败：${error.message}`);
    $('confirmStop').disabled = false;
  }
}

function drawChart() {
  const canvas = $('trendChart');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  const w = rect.width, h = rect.height, pad = 22;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = '#242c3b'; ctx.lineWidth = 1;
  for (let i = 0; i < 5; i++) { const y = pad + (h - pad * 2) * i / 4; ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(w - pad, y); ctx.stroke(); }
  const max = Math.max(100, ...state.history.flatMap(x => [x.cpu, x.tools, x.rss / 100]));
  plot(ctx, state.history.map(x => x.cpu), '#5aa7ff', max, w, h, pad);
  plot(ctx, state.history.map(x => x.tools), '#ffae57', max, w, h, pad);
  plot(ctx, state.history.map(x => x.rss / 100), '#9d7cff', max, w, h, pad);
}

function plot(ctx, values, color, max, w, h, pad) {
  if (values.length < 2) return;
  ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = 'round';
  values.forEach((value, i) => { const x = pad + (w - pad * 2) * i / Math.max(values.length - 1, 1); const y = h - pad - (h - pad * 2) * value / max; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();
}

function heat(cpu) { return cpu >= 100 ? 'critical-hot' : cpu >= 25 ? 'hot' : ''; }
function identityHtml(proc) {
  const chips = [];
  if (proc.project_label) chips.push(`<span class="identity-chip" title="${escapeHtml(proc.project || proc.project_label)}">项目 ${escapeHtml(proc.project_label)}</span>`);
  if (proc.session_id) chips.push(`<span class="identity-chip session" title="完整 Session ID：${escapeHtml(proc.session_id)}">会话 ${escapeHtml(proc.session_title || proc.session_short_id)}</span>`);
  else if (proc.group === 'ccgui' && (proc.role === 'Claude Agent' || proc.role === 'AI bridge')) chips.push('<span class="identity-chip session">会话 ID 未暴露</span>');
  if (proc.instance) chips.push(`<span class="identity-chip instance">实例 ${escapeHtml(proc.instance)}</span>`);
  return chips.length ? `<div class="identity-line">${chips.join('')}</div>` : '';
}
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function showToast(message) { const toast = $('toast'); toast.textContent = message; toast.classList.remove('hidden'); clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => toast.classList.add('hidden'), 6000); }
boot().catch(error => { $('connection').className = 'status-dot offline'; $('connection').textContent = '启动失败'; showToast(error.message); });
