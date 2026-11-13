/* SpriteMaker Web UI —— 原生 JS，无构建步骤 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  manifest: null, step: 'reference', job: null, logCount: 0,
  selection: new Set(), playFrames: [], playIndex: 0, playing: false,
  timer: null, pollTimer: null, images: {}, renderToken: 0,
  videoWorkflows: [], groupFilter: null, sourcePreviewGroup: null,
  previewVideoUrl: '', projectCount: 0,
};

const VIEWS = { front: '正面', back: '背面', left: '左侧', right: '右侧' };
const VIEW_DIRS = ['front', 'back', 'left', 'right'];
// 老项目里只有一个「侧面」（旧提示词是 90 度朝右），按右侧显示，不凭空多出一张
const normDir = d => (d === 'side' ? 'right' : d);
const STAGES = ['reference', 'keyframe', 'video', 'frames', 'select', 'sprites', 'export'];

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// 帧的唯一标识：相对 04_frames/ 的路径。多段视频会有同名 frame_XXXX.png，
// 只用文件名会把不同动作 / 方向的帧混在一起。
const frameKey = f => f.rel || f.name;

async function api(path, opts = {}) {
  // /api/jobs/<id> 的响应里带 error 字段表示「任务自身失败」，不是接口错误；
  // 这类接口要用 allowErrorField，否则会被误判成轮询失败。
  const { allowErrorField, ...fetchOpts } = opts;
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...fetchOpts,
    body: fetchOpts.body ? JSON.stringify(fetchOpts.body) : undefined,
  });
  let data;
  try { data = await res.json(); } catch { data = { error: '响应不是 JSON' }; }
  if (!res.ok || (data.error && !allowErrorField)) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function logLine(text) {
  const el = $('#log');
  el.textContent += text + '\n';
  el.scrollTop = el.scrollHeight;
}

/* 轻量吐司提示：用于「设为切帧源」这类瞬时的状态变更反馈 */
let toastTimer = null;
function toast(text, kind = 'ok') {
  const el = $('#toast');
  if (!el) return;
  el.textContent = text;
  el.className = `toast ${kind}`;
  el.hidden = false;
  // 重启动画：先移除再加回，连续点击也能看到反馈
  el.classList.remove('show');
  void el.offsetWidth;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => { if (!el.classList.contains('show')) el.hidden = true; }, 220);
  }, 2400);
}

/* ------------------------------------------------- 运行日志高度（手动可调） */
const LOG_DEFAULT = 148, LOG_MIN = 64, LOG_STEP = 40;
function logMaxHeight() { return Math.max(LOG_MIN + 40, window.innerHeight - 220); }
function currentLogHeight() { return $('#logbar').getBoundingClientRect().height || LOG_DEFAULT; }
function setLogHeight(px, persist = true) {
  const h = Math.max(LOG_MIN, Math.min(logMaxHeight(), Math.round(px)));
  document.documentElement.style.setProperty('--log-h', h + 'px');
  if (persist) { try { localStorage.setItem('sm_log_height', String(h)); } catch (_) {} }
  return h;
}
function bindLogResize() {
  const handle = $('#logResizer');
  let startY = 0, startH = 0, dragging = false;
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.classList.remove('log-dragging');
    try { localStorage.setItem('sm_log_height', String(Math.round(currentLogHeight()))); } catch (_) {}
  };
  handle.addEventListener('pointerdown', e => {
    dragging = true; startY = e.clientY; startH = currentLogHeight();
    handle.classList.add('dragging');
    document.body.classList.add('log-dragging');
    try { handle.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();
  });
  handle.addEventListener('pointermove', e => {
    if (dragging) setLogHeight(startH + (startY - e.clientY), false);
  });
  handle.addEventListener('pointerup', stop);
  handle.addEventListener('pointercancel', stop);
  handle.addEventListener('dblclick', () => setLogHeight(LOG_DEFAULT));
  $('#btnLogShrink').onclick = () => setLogHeight(currentLogHeight() - LOG_STEP);
  $('#btnLogGrow').onclick = () => setLogHeight(currentLogHeight() + LOG_STEP);
  $('#btnLogReset').onclick = () => setLogHeight(LOG_DEFAULT);
  try {
    const saved = Number(localStorage.getItem('sm_log_height'));
    if (saved > 0) setLogHeight(saved, false);
  } catch (_) {}
  window.addEventListener('resize', () => { setLogHeight(currentLogHeight(), false); fitCanvas(); });
}

function fileUrl(stage, name) {
  const f = (state.manifest?.files?.[stage] || []).find(x => x.name === name);
  return f ? f.url : '';
}

function fileOf(stage, pred) {
  return (state.manifest?.files?.[stage] || []).find(pred);
}

/* ------------------------------------------------- 背景色 / 可选底图 */
const BG_SWATCHES = [
  ['#00FF00', '绿幕'], ['#FF00FF', '品红'], ['#FFFFFF', '纯白'], ['#000000', '纯黑'],
  ['#808080', '中灰'], ['#00FFFF', '青'], ['#FFFF00', '黄'], ['#0000FF', '蓝'],
];

function markActiveSwatch() {
  const cur = ($('[data-param="background_color"]')?.value || '').trim().toUpperCase();
  $$('#bgSwatches .swatch').forEach(b => b.classList.toggle('active', b.dataset.color === cur));
  if (/^#[0-9A-F]{6}$/.test(cur)) $('#bgPicker').value = cur.toLowerCase();
}

function initBackgroundControls() {
  $('#bgSwatches').innerHTML = BG_SWATCHES.map(([c, t]) =>
    `<button type="button" class="swatch" data-color="${c}" title="${t} ${c}" style="background:${c}"></button>`).join('');
  $$('#bgSwatches .swatch').forEach(b => {
    b.onclick = () => {
      const input = $('[data-param="background_color"]');
      input.value = b.dataset.color;
      if (state.manifest) assign(state.manifest.params, 'background_color', b.dataset.color);
      markActiveSwatch();
      saveParams();
      logLine(`背景色已设为 ${b.dataset.color}`);
    };
  });
  $('#bgPicker').onchange = () => {
    const c = $('#bgPicker').value.toUpperCase();
    $('[data-param="background_color"]').value = c;
    if (state.manifest) assign(state.manifest.params, 'background_color', c);
    markActiveSwatch();
    saveParams();
  };
  // 文本输入由 bind() 的通用 change/blur 负责保存，这里只同步取色器与色块
  $('[data-param="background_color"]').addEventListener('change', markActiveSwatch);
}

async function uploadBaseImage() {
  if (!state.manifest) { alert('请先新建或选择项目'); return; }
  const file = $('#baseFile').files?.[0];
  if (!file) { alert('请先选择一张底图'); return; }
  const dataUrl = await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error('读取文件失败'));
    r.readAsDataURL(file);
  });
  try {
    const m = await api(`/api/projects/${encodeURIComponent(state.manifest.name)}/reference-image`,
      { method: 'POST', body: { data: dataUrl } });
    state.manifest = m;
    $('#baseFile').value = '';
    fillParams();
    renderAll();
    logLine(`底图已上传（${file.name}），参考图将改用图生图`);
  } catch (e) { alert('上传底图失败：' + e.message); }
}

async function clearBaseImage() {
  if (!state.manifest) return;
  try {
    const m = await api(`/api/projects/${encodeURIComponent(state.manifest.name)}/reference-image`,
      { method: 'POST', body: { clear: true } });
    state.manifest = m;
    fillParams();
    renderAll();
    logLine('已移除底图，参考图回到纯文生图');
  } catch (e) { alert('移除底图失败：' + e.message); }
}

function refreshGif() {
  const imgs = $$('#exportBox img.gif-preview');
  if (!imgs.length) { logLine('还没有 GIF 预览，请先执行导出'); return; }
  imgs.forEach((img, i) => {
    const url = new URL(img.src, location.href);
    url.searchParams.set('_', String(Date.now()));
    setTimeout(() => { img.src = url.toString(); }, i * 60);   // 逐张错开，避免同时解码卡一下
  });
  logLine(`已重新加载 ${imgs.length} 个 GIF 动图预览`);
}

/* ---------------------------------------------------------------- 弹窗 */
function modal(title, html, onOk) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = html;
  $('#modal').hidden = false;
  $('#modalOk').onclick = async () => {
    try { await onOk(); $('#modal').hidden = true; }
    catch (e) { alert(e.message); }
  };
  $('#modalCancel').onclick = () => { $('#modal').hidden = true; };
}

/* ---------------------------------------------------------------- 项目 */
async function loadHealth() {
  const badge = $('#health');
  try {
    const h = await api('/api/health');
    const ok = h.comfy === 'ok' && h.ffmpeg === 'ok' && Object.keys(h.workflows).length === 3;
    badge.textContent = ok ? `ComfyUI v${h.comfy_version || '?'} · 已连接` : '服务异常';
    badge.className = 'pill ' + (ok ? 'ok' : 'bad');
    badge.title = `${h.comfy_url}\n${h.ffmpeg_path || ''}\n${h.errors.join('\n')}`;
    if (!ok) logLine('[自检] ' + (h.errors.join('; ') || '依赖不完整'));
  } catch (e) {
    badge.textContent = '服务异常'; badge.className = 'pill bad';
    logLine('[自检] ' + e.message);
  }
}

async function loadProjects(selectName, { autoSelect = true } = {}) {
  const data = await api('/api/projects');
  state.projectCount = data.projects.length;
  const sel = $('#projectSelect');
  sel.innerHTML = '<option value="">— 选择项目 —</option>' + data.projects.map(p =>
    `<option value="${encodeURIComponent(p.name)}">${p.name}</option>`).join('');
  const wanted = selectName || state.manifest?.name;
  if (wanted && data.projects.some(p => p.name === wanted)) {
    sel.value = encodeURIComponent(wanted);
    await loadManifest(wanted);
  } else if (data.projects.length && !wanted && autoSelect) {
    sel.value = encodeURIComponent(data.projects[data.projects.length - 1].name);
    await loadManifest(decodeURIComponent(sel.value));
  } else {
    sel.value = '';
    state.manifest = null; renderEmpty();
  }
}

async function loadManifest(name, { resume = false } = {}) {
  // 项目内的帧都叫 frame_XXXX.png，切换项目必须丢弃所有图片与播放器状态，
  // 否则新项目会显示上一个项目缓存的帧图（同名帧复用）。
  const switched = state.manifest?.name !== name;
  if (switched) {
    if (state.job) logLine('已切换项目：后台任务仍在运行，切回该项目会自动恢复监控');
    stopJobPolling();
    setPlay(false);
    state.frames = [];
    state.playFrames = [];
    state.playIndex = 0;
    state.selection.clear();
    state.groupFilter = null;
    state.previewVideoUrl = '';
  }
  state.images = {};                 // 每次刷新 manifest 都重新取图，避免重跑后仍显示旧产物
  state.renderToken++;               // 让上一次 renderAll 的异步回调失效
  state.sourcePreviewGroup = null;   // 切帧完成后回到真实帧组，不再显示「尚未切帧」空态
  state.manifest = await api('/api/projects/' + encodeURIComponent(name));
  state.selection = new Set(state.manifest.selection || []);
  $('#empty').hidden = true;
  setStepsEnabled(true);
  $$('.panel').forEach(p => p.hidden = true);
  populateSelectors();               // 必须先填好下拉选项，fillParams() 才能正确回填
  showStep(state.step);              // 内部会调用 renderAll
  if (resume) resumeJob(name);       // 刷新页面 / 切回项目时接管仍在运行的任务
}

function renderEmpty() {
  const has = (state.projectCount || 0) > 0;
  const title = $('#emptyTitle'), text = $('#emptyText');
  if (title) title.textContent = has ? '未选择项目' : '还没有项目';
  if (text) {
    text.textContent = has
      ? '从右上角的下拉里选一个项目继续，或新建一个。'
      : '新建一个项目，填好角色描述，然后按左侧流水线依次生成。';
  }
  $('#empty').hidden = false;
  // 没选项目就没有可打开的面板：左侧步骤整排禁用，避免点开上一个项目的残留内容
  setStepsEnabled(false);
  $$('.panel').forEach(p => p.hidden = true);
}

function setStepsEnabled(on) {
  $$('.step').forEach(b => {
    b.disabled = !on;
    if (!on) b.classList.remove('active');
  });
}

/* ---------------------------------------------------------------- 删除：按组 / 按段 / 整个项目 */
function fmtSize(bytes) {
  const b = Number(bytes) || 0;
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b >= 1024) return Math.round(b / 1024) + ' KB';
  return b ? b + ' B' : '0 KB';
}

async function clearGroup(stage, group, label) {
  const m = state.manifest;
  if (!m || !group) return;
  const msg = {
    frames: `删除「${label}」这一组切出来的帧？\n\n只动这一段视频：它的精灵帧与导出也会一并清掉，其它组不受影响。`,
    sprites: `删除「${label}」这一组的精灵帧？\n\n只动这一段视频：它导出的 spritesheet / GIF / ZIP 也会一起清掉，其它组不受影响。`,
    export: `删除「${label}」这一组的导出文件？\n\n只删这一段视频的 spritesheet / GIF / ZIP，精灵帧与其它组都不动。`,
  }[stage] || `删除「${label}」这一组的产物？`;
  if (!confirm(msg)) return;
  const what = { frames: '帧组', sprites: '精灵帧', export: '导出文件' }[stage] || '产物';
  try {
    const man = await api(`/api/projects/${encodeURIComponent(m.name)}/clear`,
      { method: 'POST', body: { stage, group } });
    const info = man.cleared || {};
    await loadManifest(m.name);
    logLine(`已删除「${label}」的${what}：${info.files || 0} 个文件 / ${fmtSize(info.bytes)}`);
    toast('已删除该组产物');
  } catch (e) { alert('删除失败：' + e.message); }
}

function deleteProject() {
  const m = state.manifest;
  if (!m) { toast('请先选择一个项目', 'bad'); return; }
  const media = Object.values(m.files || {}).flat();
  const bytes = media.reduce((s, f) => s + (f.size || 0), 0);
  const name = m.name;
  modal('删除项目', `
    <p>将删除项目目录 <code>projects/${esc(name)}/</code>：全部产物、帧选择、提示词缓存与运行日志都会被清掉。</p>
    <p class="muted">当前 ${media.length} 个媒体文件 · ${fmtSize(bytes)}。此操作不可撤销。</p>
    <label>输入项目名 <b>${esc(name)}</b> 以确认<input id="mDelName" autocomplete="off" placeholder="${esc(name)}"></label>`,
    async () => {
      const typed = ($('#mDelName').value || '').trim();
      if (typed !== name) throw new Error('项目名不一致，已取消');
      await api(`/api/projects/${encodeURIComponent(name)}`, { method: 'DELETE' });
      logLine(`已删除项目：${name}`);
      stopJobPolling();
      state.manifest = null;
      state.selection.clear();
      state.frames = [];
      state.playFrames = [];
      toast('项目已删除');
      await loadProjects(undefined, { autoSelect: false });
    });
}

function newProject() {
  modal('新建项目', `
    <label>项目名称（英文/数字，作为目录名）<input id="mName" placeholder="slime_knight"></label>
    <label>角色描述（中文）<textarea id="mChar" rows="3" placeholder="一只蓝色小史莱姆骑士，头戴金色头盔，手拿圆形木盾"></textarea></label>
    <label>画风（可留空用默认）<input id="mStyle" placeholder="干净的 2D 游戏精灵，粗描边，赛璐璐上色"></label>
    <div class="hint">创建后可在第 ① 步修改全部参数。</div>`, async () => {
    const name = $('#mName').value.trim();
    if (!name) throw new Error('请填写项目名称');
    const params = {};
    if ($('#mChar').value.trim()) params.character = $('#mChar').value.trim();
    if ($('#mStyle').value.trim()) params.style = $('#mStyle').value.trim();
    const m = await api('/api/projects', { method: 'POST', body: { name, params } });
    logLine(`项目已创建：${m.name}`);
    await loadProjects(name);
    showStep('reference');
  });
}

function settings() {
  api('/api/config').then(cfg => {
    modal('设置', `
      <div class="sub-block">
        <div class="sub-title">ComfyUI 连接</div>
        <label>地址<input id="cUrl" value="${cfg.comfy_url}"></label>
        <div class="row">
          <label>用户名<input id="cUser" value="${cfg.comfy_user || ''}"></label>
          <label>密码<input id="cPass" type="password" value="${cfg.comfy_password}"></label>
        </div>
        <div class="row">
          <label>上传子目录<input id="cSub" value="${cfg.comfy_subfolder}"></label>
          <label>Web 端口<input id="cPort" type="number" value="${cfg.port}"></label>
        </div>
        <div class="row">
          <label>网络重试次数<input id="cRetries" type="number" min="1" max="10" value="${cfg.request_retries ?? 3}"></label>
          <label>轮询失败上限（0=不限）<input id="cPollMax" type="number" min="0" value="${cfg.poll_max_errors ?? 0}"></label>
        </div>
        <div class="hint">超时 / 502 自动重试；改端口需重启服务。</div>
      </div>
      <div class="sub-block">
        <div class="sub-title">提示词优化（DeepSeek）</div>
        <label class="check"><input type="checkbox" id="cOpt" ${cfg.prompt_optimize ? 'checked' : ''}> 生成前用 DeepSeek 改写提示词</label>
        <label>API Key<input id="cDsKey" type="password" value="${cfg.deepseek_api_key || ''}"></label>
        <label>模型<input id="cDsModel" value="${cfg.deepseek_model || 'deepseek-chat'}"></label>
        <div class="hint">只改写写法，硬性约束保留；失败退回原模板。</div>
      </div>`, async () => {
      await api('/api/config', { method: 'POST', body: {
        comfy_url: $('#cUrl').value.trim(), comfy_user: $('#cUser').value.trim(),
        comfy_password: $('#cPass').value, comfy_subfolder: $('#cSub').value.trim(),
        port: Number($('#cPort').value),
        request_retries: Number($('#cRetries').value),
        poll_max_errors: Number($('#cPollMax').value),
        prompt_optimize: $('#cOpt').checked,
        deepseek_api_key: $('#cDsKey').value.trim(),
        deepseek_model: $('#cDsModel').value.trim() || 'deepseek-chat',
      }});
      logLine('配置已保存'); await loadHealth();
    });
  }).catch(e => alert(e.message));
}

/* ---------------------------------------------------------------- 参数 */
function parseValue(el) {
  if (el.type === 'checkbox') return el.checked;
  if (el.type === 'number') return Number(el.value);
  return el.value;
}

function assign(params, key, value) {
  if (key.includes('.')) {
    const [a, b] = key.split('.', 2);
    params[a] = params[a] || {};
    params[a][b] = value;
  } else params[key] = value;
}

// 旧项目可能没有这些后加的参数，界面用同样的默认值兜底，避免下拉框空白
const PARAM_DEFAULTS = {
  background_mode: 'solid', background_color: '#00FF00',
  sprite_method: 'redraw', chroma_tolerance: 60, reference_use_image: false,
  sprite_scale_mode: 'global',
  video_pad_left: 20, video_pad_top: 20, video_pad_right: 20, video_pad_bottom: 20,
  video_mp_follow: true, video_silent_audio: true, video_seed: 0,
  weapon_hand: 'auto',
};

function fillParams() {
  const p = state.manifest?.params || {};
  $$('[data-param]').forEach(el => {
    const key = el.dataset.param;
    let v = key.includes('.') ? (p[key.split('.')[0]] || {})[key.split('.')[1]] : p[key];
    // 首帧下拉没有空选项：参数为空时保留 populateSelectors 选中的第一张
    if (key === 'first_frame' && !v) return;
    if (v === undefined || v === null || v === '') v = PARAM_DEFAULTS[key] ?? '';
    if (el.type === 'checkbox') el.checked = !!v;
    else el.value = v;
  });
}

function collectParams() {
  const patch = {};
  $$('[data-param]').forEach(el => assign(patch, el.dataset.param, parseValue(el)));
  return patch;
}

async function saveParams() {
  if (!state.manifest) return;
  try {
    const patch = collectParams();
    const name = state.manifest.name;
    const m = await api(`/api/projects/${encodeURIComponent(name)}/params`, { method: 'POST', body: { params: patch } });
    state.manifest.params = m.params;
    logLine('参数已保存');
  } catch (e) { alert('保存参数失败：' + e.message); }
}

/* ---------------------------------------------------------------- 动作预设 */
async function loadPresets() {
  let list;
  try { list = (await api('/api/presets')).actions || []; }
  catch (e) {
    $('#motionPresets').innerHTML = '<span class="muted">预设加载失败，可直接在上方输入中文动作描述</span>';
    return;
  }
  state.actions = list;
  $('#motionPresets').innerHTML = list.map(a =>
    `<button type="button" class="preset-btn" data-key="${a.key}" title="${a.motion}">${a.label}</button>`).join('');
  $$('#motionPresets .preset-btn').forEach(btn => {
    btn.onclick = async () => {
      const item = list.find(x => x.key === btn.dataset.key);
      if (!item) return;
      const el = $('[data-param="video_motion"]');
      if (!el) return;
      el.value = item.motion;
      if (state.manifest) assign(state.manifest.params, 'video_motion', item.motion);
      markActivePreset();
      await saveParams();
      logLine(`动作描述已设为「${item.label}」`);
    };
  });
  markActivePreset();
}

function markActivePreset() {
  const cur = ($('[data-param="video_motion"]')?.value || '').trim();
  $$('#motionPresets .preset-btn').forEach(b => {
    const item = (state.actions || []).find(x => x.key === b.dataset.key);
    b.classList.toggle('active', !!item && cur === item.motion);
  });
}

/* ---------------------------------------------------------------- 视频段 / 工作流 */
async function loadWorkflows() {
  try { state.videoWorkflows = (await api('/api/workflows')).videos || []; }
  catch (_) { state.videoWorkflows = []; }
}

function populateSelectors() {
  const m = state.manifest;
  if (!m) return;
  const list = state.videoWorkflows || [];
  const def = list.find(x => x.default);
  const wf = $('#videoWorkflow');
  wf.innerHTML = `<option value="">默认${def ? '（' + esc(def.name) + '）' : ''}</option>` +
    list.filter(x => !x.default).map(x =>
      `<option value="${esc(x.file)}" ${x.compatible ? '' : 'disabled'} ` +
      `title="${esc(x.compatible ? x.file : '不兼容：' + x.reason)}">${esc(x.name)}${x.compatible ? '' : '（不兼容）'}</option>`).join('');
  const wantWf = m.params.video_workflow || '';
  if (wantWf && !list.some(x => x.file === wantWf)) {
    wf.insertAdjacentHTML('beforeend', `<option value="${esc(wantWf)}">${esc(wantWf)}（文件已移除）</option>`);
  }
  wf.value = wantWf;
  if (wf.value !== wantWf) wf.value = '';

  // 第 ③ 步：首帧 / 尾帧都从第 ② 步的单帧库里选
  const keys = (m.keys && m.keys.keys) || [];
  const fsel = $('#firstKeySelect');
  const lsel = $('#lastKeySelect');
  fsel.innerHTML = keys.length
    ? keys.map(k => `<option value="${esc(k.id)}">${esc(k.label)}</option>`).join('')
    : '<option value="">— 先在 ② 生成 —</option>';
  const wantFirst = keys.some(k => k.id === m.params.first_frame)
    ? m.params.first_frame : (keys[0]?.id || '');
  fsel.value = wantFirst;
  // 尾帧下拉排除首帧自己：同图就是循环，那由「同首帧」这个空选项表达
  lsel.innerHTML = '<option value="">同首帧（无缝循环）</option>' +
    keys.filter(k => k.id !== wantFirst)
      .map(k => `<option value="${esc(k.id)}">${esc(k.label)}（过渡到尾帧）</option>`).join('');
  const wantLast = keys.some(k => k.id === m.params.last_frame && k.id !== wantFirst)
    ? m.params.last_frame : '';
  lsel.value = wantLast;
  updateKeyPicked();

  const vids = (m.videos || []).slice().reverse();   // 下拉里最新一段排最前
  const latest = vids[0];
  const fs = $('#frameSource');
  fs.innerHTML = `<option value="">最新一段${latest ? '（' + esc(latest.label || latest.file) + '）' : ''}</option>` +
    vids.map(v => `<option value="${esc(v.file)}">${esc(v.label || v.file)}</option>`).join('');
  fs.value = m.params.frame_source || '';
  if (fs.value !== (m.params.frame_source || '')) fs.value = '';
  updateFrameSourceInfo();
  populateScopeSelects();            // ⑥⑦ 能处理 / 能导出的组，随产物自动更新
}

/* --------------------------------------- ⑥⑦ 处理范围（可以只跑一段视频的产物） */
// 帧组 id = 视频段 id；旧版平铺组 id 为 ''，只能随「全部」一起处理，故不单独列出
function scopeOptions(groups, allLabel) {
  return `<option value="">${esc(allLabel)}</option>` + groups.filter(g => g.id).map(g => {
    const name = g.label || videoLabel(g.id) || g.id;
    return `<option value="${esc(g.id)}">${esc(name)}${g.count ? `（${g.count} 帧）` : ''}</option>`;
  }).join('');
}

function populateScopeSelects() {
  const m = state.manifest;
  if (!m) return;
  // ⑥ 可选 = 有帧的帧组；⑦ 可选 = 已经出过精灵帧的组（没出过就没东西可导）
  const frameGs = frameGroups().map(g => ({ id: g.id, label: g.label || videoLabel(g.id), count: g.count }));
  const spriteGs = (m.sprite_groups || []).map(g => ({ id: g.id, label: g.label || videoLabel(g.id), count: g.count }));
  [['#spriteScope', 'sprite_group', frameGs, '全部帧组（已选帧一次处理完）'],
   ['#exportScope', 'export_group', spriteGs.length ? spriteGs : frameGs, '全部帧组（每组一套文件）'],
  ].forEach(([sel, key, groups, allLabel]) => {
    const el = $(sel);
    if (!el) return;
    const cur = String(m.params[key] ?? '');
    el.innerHTML = scopeOptions(groups, allLabel);
    // 参数指向的帧组已经没了（视频段被删）：保留一个可见项，别静默变空白
    if (cur && !groups.some(g => g.id === cur)) {
      el.insertAdjacentHTML('beforeend',
        `<option value="${esc(cur)}">${esc(videoLabel(cur) || cur)}（已不存在）</option>`);
    }
    el.value = cur;
  });
}

// 点上「只重绘 / 只导出这组」：把范围切到该组、存盘、直接开跑
async function runScoped(selectId, groupId, stage) {
  const el = $(selectId);
  if (el) el.value = groupId;
  await saveParams();
  runStage(stage);
}

/* ------------------------------------------------- 单帧库（第②③步） */
function keyList() { return (state.manifest?.keys?.keys) || []; }

function keyOf(id) { return keyList().find(k => k.id === id); }

function keyImgUrl(file) { return file ? fileUrl('keyframe', file) : ''; }

function firstKey() {
  const sel = $('#firstKeySelect');
  return keyOf(sel?.value) || keyList()[0];
}

function lastKey() {
  const first = firstKey();
  return keyOf($('#lastKeySelect')?.value) || first;
}

function updateKeyPicked() {
  const box = $('#keyPicked');
  if (!box) return;
  const first = firstKey();
  if (!first) {
    box.className = 'pair-picked muted';
    box.textContent = '还没有单帧，请先在第 ② 步生成';
    return;
  }
  const last = lastKey();
  const loop = last.id === first.id;
  box.className = 'pair-picked';
  box.innerHTML = `<div class="pp-thumbs">
      <figure><img src="${keyImgUrl(first.file)}" alt="首帧" data-zoom data-caption="首帧 · ${esc(first.label)}"><figcaption>首帧 · ${esc(first.label)}</figcaption></figure>
      <figure><img src="${keyImgUrl(last.file)}" alt="尾帧" data-zoom data-caption="${loop ? '尾帧 = 首帧' : '尾帧 · ' + esc(last.label)}"><figcaption>${loop ? '尾帧 = 首帧' : '尾帧 · ' + esc(last.label)}</figcaption></figure>
    </div>
    <span class="pp-meta">${loop ? '无缝循环：尾帧不选，就用首帧当尾帧' : '首尾过渡：从「' + esc(first.label) + '」动到「' + esc(last.label) + '」'}</span>`;
  bindZoom();
}

function renderKeys() {
  const m = state.manifest;
  if (!m) return;
  const keys = keyList();
  const box = $('#keyList');
  const stats = $('#keyStats');
  if (!keys.length) {
    box.innerHTML = '<div class="muted">还没有单帧，先点上面的「生成 / 更新单帧」。</div>';
    stats.textContent = '';
    stats.hidden = true;               // 空着一根破折号看着像残留，直接收起来
    return;
  }
  stats.hidden = false;
  stats.classList.remove('muted');
  const cur = $('#firstKeySelect')?.value || m.params.first_frame || keys[0].id;
  stats.textContent = `共 ${keys.length} 张单帧`;
  box.innerHTML = keys.map(k => `
    <div class="key-card ${k.id === cur ? 'is-first' : ''}" data-key="${esc(k.id)}">
      <figure class="key-thumb">
        <img src="${keyImgUrl(k.file)}" alt="${esc(k.label)}" data-zoom data-caption="${esc(k.label)} · ${esc(k.file)}">
        <figcaption>
          <b>${esc(k.label)}</b>
          ${k.id === cur ? '<span class="pill ok">首帧</span>' : `<span class="muted">${esc(k.id)}</span>`}
        </figcaption>
      </figure>
      <div class="key-main">
        <div class="key-editrow">
          <input class="key-prompt" placeholder="追加细节，例如：手里加一把发光的长剑" value="${esc(k.edit_prompt || '')}">
          <button class="btn tiny" data-key-act="edit">重绘</button>
          <button class="btn tiny ghost" data-key-act="first">设为首帧</button>
          <button class="btn ghost tiny danger" data-key-act="delete">删除</button>
        </div>
        ${k.edit_prompt ? `<div class="pair-note">上次编辑：${esc(k.edit_prompt)}</div>` : ''}
        ${k.prompt ? `<details class="key-promptview">
          <summary>提取提示词</summary>
          <pre>${esc(k.prompt)}</pre>
          <button class="btn ghost tiny" data-key-act="copyprompt">复制</button>
        </details>` : ''}
      </div>
    </div>`).join('');
  bindKeyActions();
  bindZoom();
}

async function selectFirstKey(id) {
  const m = state.manifest;
  if (!m || !keyOf(id)) return;
  try {
    const patch = { first_frame: id };
    // 尾帧和新的首帧撞成同一张就退回「同首帧」，否则界面写着循环、参数里还留着一张尾帧
    if (m.params.last_frame === id) patch.last_frame = '';
    const r = await api(`/api/projects/${encodeURIComponent(m.name)}/params`,
      { method: 'POST', body: { params: patch } });
    state.manifest.params = r.params;
    populateSelectors();               // 尾帧下拉要排除刚选中的这张
    renderKeys();
    logLine(`第③步首帧设为「${keyOf(id)?.label || id}」`);
  } catch (e) { alert('设置首帧失败：' + e.message); }
}

/* ---------------------------------------------------------------- 图片放大 */
// 单帧只有 1024 级，靠缩略图看不清描边和手指，所以给所有 data-zoom 的图配一个灯箱。
const LB = { scale: 1, fit: 1, x: 0, y: 0, drag: null, moved: false, open: null };

function ensureLightbox() {
  let box = $('#lightbox');
  if (box) return box;
  box = document.createElement('div');
  box.id = 'lightbox';
  box.className = 'lightbox';
  box.hidden = true;
  box.innerHTML = `
    <div class="lb-bar">
      <span class="lb-caption"></span>
      <span class="lb-hint">滚轮缩放 · 拖动平移 · 双击复位</span>
      <button class="btn tiny ghost" data-lb="fit">适应窗口</button>
      <button class="btn tiny ghost" data-lb="one">1:1</button>
      <button class="btn tiny ghost" data-lb="close">关闭 ✕</button>
    </div>
    <div class="lb-stage"><img alt=""></div>`;
  document.body.appendChild(box);
  const stage = $('.lb-stage', box);
  const img = $('img', box);

  const apply = () => {
    img.style.transform = `translate(${LB.x}px, ${LB.y}px) scale(${LB.scale})`;
  };

  const fitToStage = () => {
    const r = stage.getBoundingClientRect();
    const nw = img.naturalWidth || 1;
    const nh = img.naturalHeight || 1;
    LB.fit = Math.min((r.width - 28) / nw, (r.height - 28) / nh, 1);
    LB.scale = LB.fit > 0.02 ? LB.fit : 1;
    LB.x = LB.y = 0;
    apply();
  };

  const zoomAt = (cx, cy, factor) => {
    const r = stage.getBoundingClientRect();
    const next = Math.min(8, Math.max(0.05, LB.scale * factor));
    const k = next / LB.scale;
    // 以光标为锚点：光标下那一点在缩放前后保持不动
    const ox = cx - r.left - r.width / 2 - LB.x;
    const oy = cy - r.top - r.height / 2 - LB.y;
    LB.x -= ox * (k - 1);
    LB.y -= oy * (k - 1);
    LB.scale = next;
    apply();
  };

  const close = () => {
    box.hidden = true;
    document.body.classList.remove('lb-open');
    img.removeAttribute('src');
  };

  LB.open = (url, caption) => {
    $('.lb-caption', box).textContent = caption || '';
    box.hidden = false;
    document.body.classList.add('lb-open');
    if (img.dataset.src === url && img.complete && img.naturalWidth) {
      fitToStage();                     // 同一张再打开：缓存命中不会触发 load
    } else {
      img.dataset.src = url;
      img.src = url;
    }
  };

  img.addEventListener('load', fitToStage);
  stage.addEventListener('wheel', e => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0014));
  }, { passive: false });
  stage.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    LB.drag = { x0: e.clientX, y0: e.clientY, ox: LB.x, oy: LB.y };
    LB.moved = false;
    stage.classList.add('dragging');
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener('pointermove', e => {
    if (!LB.drag) return;
    const dx = e.clientX - LB.drag.x0;
    const dy = e.clientY - LB.drag.y0;
    if (Math.abs(dx) + Math.abs(dy) > 3) LB.moved = true;
    LB.x = LB.drag.ox + dx;
    LB.y = LB.drag.oy + dy;
    apply();
  });
  stage.addEventListener('pointerup', e => {
    LB.drag = null;
    stage.classList.remove('dragging');
    try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
    // 拖动结束时不要误判成「点空白关闭」
    if (!LB.moved && e.target === stage) close();
  });
  stage.addEventListener('dblclick', fitToStage);
  $$('[data-lb]', box).forEach(b => {
    b.onclick = () => {
      if (b.dataset.lb === 'close') return close();
      if (b.dataset.lb === 'fit') return fitToStage();
      LB.scale = 1;
      LB.x = LB.y = 0;
      apply();
    };
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !box.hidden) close();
  });
  return box;
}

function openLightbox(url, caption) {
  ensureLightbox();
  LB.open(url, caption);
}

// 事件委托只装一次：所有 data-zoom 的图（① 正面立绘 / ② 单帧 / ③ 首尾帧缩略图）都能点开
function bindZoom() {
  if (bindZoom.done) return;
  bindZoom.done = true;
  document.addEventListener('click', e => {
    const img = e.target.closest('img[data-zoom]');
    if (!img) return;
    openLightbox(img.dataset.full || img.currentSrc || img.src,
      img.dataset.caption || img.alt || '');
  });
}


/* ------------------------------------------------- 第②步：方向 / 追加方向 */
function fillDirs() {
  const dirs = (state.manifest?.params?.frame_directions || VIEW_DIRS).map(normDir);
  $$('#dirPicks input[data-dir]').forEach(i => { i.checked = dirs.includes(i.dataset.dir); });
}

async function saveDirs() {
  if (!state.manifest) return;
  const dirs = $$('#dirPicks input[data-dir]').filter(i => i.checked).map(i => i.dataset.dir);
  if (!dirs.length) return;                   // 至少留一个，调用方已阻止全不选
  try {
    const r = await api(`/api/projects/${encodeURIComponent(state.manifest.name)}/params`,
      { method: 'POST', body: { params: { frame_directions: dirs } } });
    state.manifest.params = r.params;
    logLine(`第②步方向：${dirs.map(d => VIEWS[d] || d).join(' / ')}`);
  } catch (e) { alert('保存方向失败：' + e.message); }
}

function extraViews() {
  const list = state.manifest?.params?.frame_extra_views;
  return Array.isArray(list) ? list : [];
}

async function saveExtraViews(list) {
  if (!state.manifest) return;
  const r = await api(`/api/projects/${encodeURIComponent(state.manifest.name)}/params`,
    { method: 'POST', body: { params: { frame_extra_views: list } } });
  state.manifest.params = r.params;
  renderExtraViews();
}

function renderExtraViews() {
  const box = $('#extraViews');
  if (!box) return;
  const list = extraViews();
  if (!list.length) {
    box.className = 'extra-views muted';
    box.textContent = '无';
    return;
  }
  box.className = 'extra-views';
  box.innerHTML = list.map((v, i) =>
    `<span class="view-chip" title="${esc(v.prompt || v.from || '')}">${esc(v.label)}
      <button type="button" data-view-del="${i}" title="移除这个方向">×</button></span>`).join('');
  $$('#extraViews [data-view-del]').forEach(b => {
    b.onclick = () => {
      const next = extraViews().slice();
      next.splice(Number(b.dataset.viewDel), 1);
      saveExtraViews(next).catch(e => alert('移除失败：' + e.message));
    };
  });
}

function addExtraView() {
  const froms = [['front', '正面'], ['back', '背面'], ['left', '左侧'], ['right', '右侧']];
  modal('追加一个方向', `
    <label>方向名称<input id="vLabel" placeholder="例如：左前 45°"></label>
    <label>视角描述（会写进图生图提示词）<textarea id="vPrompt" rows="3"
      placeholder="左前方 45 度视角，身体朝向画面左前方，能看到侧脸与胸口"></textarea></label>
    <label>参考哪个视图<select id="vFrom">
      ${froms.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}
    </select></label>
    <div class="hint">以参考视图为底图转出这个角度。</div>`, async () => {
    const label = $('#vLabel').value.trim();
    if (!label) throw new Error('请填写方向名称');
    const list = extraViews().slice();
    list.push({ label, prompt: $('#vPrompt').value.trim(), from: $('#vFrom').value });
    await saveExtraViews(list);
    logLine(`已追加方向「${label}」，点「生成 / 更新单帧」后生效`);
    toast(`已追加方向：${label}`);
  });
}


function bindKeyActions() {
  const m = state.manifest;
  if (!m) return;
  $$('#keyList .key-card').forEach(card => {
    const id = card.dataset.key;
    const input = $('.key-prompt', card);
    $$('[data-key-act]', card).forEach(btn => {
      btn.onclick = async () => {
        if (btn.dataset.keyAct === 'delete') {
          if (!confirm('删除这张单帧？\n只删这一张图，已生成的视频和帧组不受影响。')) return;
          try {
            const meta = await api(`/api/projects/${encodeURIComponent(m.name)}/keys/delete`,
              { method: 'POST', body: { id } });
            state.manifest = await api('/api/projects/' + encodeURIComponent(m.name));
            state.manifest.keys = meta;
            populateSelectors();
            renderKeys();
            toast('已删除该单帧');
            logLine(`已删除单帧「${id}」`);
          } catch (e) { alert('删除失败：' + e.message); }
          return;
        }
        if (btn.dataset.keyAct === 'first') { selectFirstKey(id); return; }
        if (btn.dataset.keyAct === 'copyprompt') {
          const text = $('.key-promptview pre', card)?.textContent || '';
          try {
            await navigator.clipboard.writeText(text);
            toast('提示词已复制');
          } catch (e) { alert('复制失败，可直接选中文本复制'); }
          return;
        }
        const prompt = (input?.value || '').trim();
        if (!prompt) { alert('请先填写要增加的细节，例如：手里加一把发光的长剑'); input?.focus(); return; }
        startJob({ stage: 'keyframe', op: { key: id, prompt } }, `单帧重绘（${id}）`);
      };
    });
  });
}

function updateFrameSourceInfo() {
  const m = state.manifest;
  if (!m) return;
  const vids = m.videos || [];
  const chosen = $('#frameSource').value;
  const r = chosen ? vids.find(v => v.file === chosen) : vids[vids.length - 1];
  const box = $('#frameSourceInfo');
  const sel = $('#frameSource');
  // 固定 / 跟随最新，两种模式在控件上就要能看出来
  if (sel) {
    sel.classList.toggle('pinned', !!chosen);
    sel.title = chosen ? '已固定切帧源' : '跟随最新一段视频';
  }
  if (!r) { box.textContent = '还没有视频段，请先在第 ③ 步生成'; box.classList.add('muted'); return; }
  const probe = r.probe || {};
  box.className = 'stats' + (chosen ? ' pinned' : '');
  box.textContent = `${chosen ? '✓ 已固定切帧源' : '跟随最新'}：${r.label || r.file}｜${r.direction || '—'}｜` +
    `${r.workflow_label || r.workflow || '默认工作流'}` +
    (probe.width ? `｜${probe.width}×${probe.height} · ${probe.duration}s` : '') +
    `｜${((r.size || 0) / 1024 / 1024).toFixed(1)} MB`;
}

function renderVideoList() {
  const m = state.manifest;
  if (!m) return;
  const vids = m.videos || [];
  $('#videoCount').textContent = `${vids.length} 段`;
  const box = $('#videoList');
  if (!vids.length) {
    box.innerHTML = '尚未生成视频'; box.classList.add('muted');
    $('#videoBox').textContent = '尚未生成视频';
    $('#videoBox').classList.add('muted');
    return;
  }
  box.classList.remove('muted');
  // 切帧源：未显式指定时跟随最新一段，显式指定则固定
  const pinned = !!m.params.frame_source;
  const srcFile = m.params.frame_source || (vids[vids.length - 1] || {}).file || '';
  const rows = vids.slice().reverse();
  box.innerHTML = rows.map(r => {
    const isSrc = r.file === srcFile;
    const isPreview = state.previewVideoUrl === r.url;
    return `
    <div class="video-item${isPreview ? ' active' : ''}${isSrc ? ' is-source' : ''}" data-file="${esc(r.file)}">
      <div class="vi-main">
        <div class="vi-title"><b>${esc(r.label || r.file)}</b>${isSrc ? '<span class="vi-badge">切帧源</span>' : ''}</div>
        <span class="vi-meta">${esc(r.action || '—')} · ${esc(r.direction || '—')} · ${r.loop ? '首尾同帧（循环）' : '首尾过渡'} · ${esc(r.workflow_label || r.workflow || '默认工作流')} · ${esc(r.created || '')}</span>
        <span class="vi-seed"><span>种子</span><input class="seed-in" type="text" inputmode="numeric"
          value="${esc(r.seed ?? '')}" data-file="${esc(r.file)}" placeholder="无记录"
          title="这段用的种子，可直接改（只改记录，已生成的视频不变）"><button class="btn ghost tiny"
          data-act="seed" title="把这个种子填进左侧生成表单，下次生成用它">用于下次生成</button></span>
      </div>
      <div class="vi-actions">
        <button class="btn ghost tiny${isPreview ? ' on' : ''}" data-act="preview">${isPreview ? '✓ 预览中' : '预览'}</button>
        <button class="btn tiny${isSrc ? ' src-on' : ' ghost'}" data-act="source"
          title="${isSrc ? '第 ④ 步正在切这段视频' : '设为第 ④ 步切帧源'}">${isSrc ? '✓ 切帧源' : '设为切帧源'}</button>
        <button class="btn ghost tiny" data-act="rename">重命名</button>
        <button class="btn ghost tiny danger" data-act="delete">删除</button>
      </div>
    </div>`;
  }).join('');
  $('#videoSourceHint').innerHTML = pinned
    ? `切帧源已固定为「<b>${esc((vids.find(v => v.file === srcFile) || {}).label || srcFile || '—')}</b>」，<a href="#" id="unpinSource">改为跟随最新</a>`
    : `切帧源跟随最新一段（<b>${esc((vids.find(v => v.file === srcFile) || {}).label || '—')}</b>），新生成的视频会自动成为切帧源`;

  $$('#videoList .video-item').forEach(item => {
    const r = vids.find(v => v.file === item.dataset.file);
    if (!r) return;
    $$('[data-act]', item).forEach(btn => {
      btn.onclick = async () => {
        const url = `/api/projects/${encodeURIComponent(m.name)}/videos/`;
        try {
          if (btn.dataset.act === 'preview') {
            state.previewVideoUrl = r.url;
            renderVideoList();
          } else if (btn.dataset.act === 'source') {
            const already = m.params.frame_source === r.file;
            await api(`/api/projects/${encodeURIComponent(m.name)}/params`,
              { method: 'POST', body: { params: { frame_source: r.file } } });
            m.params.frame_source = r.file;
            $('#frameSource').value = r.file;
            updateFrameSourceInfo();
            renderVideoList();                       // 立刻重画：徽章 / 高亮 / 按钮状态一起变
            const row = $(`#videoList .video-item[data-file="${CSS.escape(r.file)}"]`);
            if (row) {                               // 再闪一下，视线能马上落到这一行
              row.classList.add('just-set');
              row.scrollIntoView({ block: 'nearest' });
              setTimeout(() => row.classList.remove('just-set'), 1100);
            }
            toast(`${already ? '切帧源保持为' : '已设为切帧源'}：${r.label || r.file}`);
            logLine(`切帧源已设为「${r.label || r.file}」，第 ④ 步将切这段视频`);
          } else if (btn.dataset.act === 'seed') {
            const field = $('[data-param="video_seed"]');
            const val = String(r.seed ?? '').trim();
            if (!field || !/^\d+$/.test(val)) { toast('这段没有可用的种子'); return; }
            field.value = val;
            await saveParams();
            toast(`种子 ${val} 已填入生成表单；填回 0 恢复自动`);
            logLine(`视频种子固定为 ${val}，下次生成新视频段将使用它（首尾帧与动作仍按表单当前设置）`);
          } else if (btn.dataset.act === 'rename') {
            const label = prompt('新的视频段名称', r.label || r.file);
            if (label === null) return;
            await api(url + 'rename', { method: 'POST', body: { file: r.file, label } });
            await loadManifest(m.name);
            logLine(`视频段已重命名为「${label}」`);
          } else if (btn.dataset.act === 'delete') {
            if (!confirm(`确定删除视频段「${r.label || r.file}」吗？\n将同时删除它专属的帧组与对应已选帧，其它视频段不受影响。`)) return;
            await api(url + 'delete', { method: 'POST', body: { file: r.file } });
            state.previewVideoUrl = '';
            await loadManifest(m.name);
            logLine(`已删除视频段「${r.label || r.file}」`);
          }
        } catch (e) { alert('操作失败：' + e.message); }
      };
    });
  });

  // 种子是直接可改的输入框；改完只更新该段的记录，已生成的视频文件不变
  $$('#videoList .seed-in').forEach(inp => {
    inp.onchange = async () => {
      const rec = vids.find(v => v.file === inp.dataset.file);
      const raw = inp.value.trim();
      if (!/^\d+$/.test(raw)) {
        inp.value = String(rec?.seed ?? '');
        toast('种子需要是非负整数');
        return;
      }
      try {
        await api(`/api/projects/${encodeURIComponent(m.name)}/videos/seed`,
          { method: 'POST', body: { file: inp.dataset.file, seed: raw } });
        if (rec) rec.seed = raw;
        toast('这段的种子记录已改为 ' + raw);
        logLine(`视频段「${rec?.label || inp.dataset.file}」的种子记录改为 ${raw}（已生成的视频不变）`);
      } catch (e) { alert('保存种子失败：' + e.message); }
    };
  });

  const unpin = $('#unpinSource');
  if (unpin) unpin.onclick = async e => {
    e.preventDefault();
    try {
      await api(`/api/projects/${encodeURIComponent(m.name)}/params`,
        { method: 'POST', body: { params: { frame_source: '' } } });
      m.params.frame_source = '';
      $('#frameSource').value = '';
      updateFrameSourceInfo();
      renderVideoList();
      toast('切帧源已改为跟随最新一段');
      logLine('切帧源取消固定，之后新生成的视频会自动成为切帧源');
    } catch (err) { alert('操作失败：' + err.message); }
  };

  const cur = vids.find(v => v.url === state.previewVideoUrl) || vids[vids.length - 1];
  $('#videoBox').innerHTML = cur ? `<video controls loop playsinline src="${cur.url}"></video>` : '尚未生成视频';
  $('#videoBox').classList.remove('muted');
}

/* ---------------------------------------------------------------- 步骤 / 任务 */
function showStep(step) {
  state.step = step;
  // 没有项目时，步骤面板里的输入框和按钮都无处可存，留着只会让人误以为能直接填。
  if (!state.manifest) { renderEmpty(); return; }
  $$('.step').forEach(b => b.classList.toggle('active', b.dataset.step === step));
  $$('.panel').forEach(p => p.hidden = p.dataset.panel !== step);
  if (state.manifest) {
    fillParams();
    renderAll();
  }
  markActivePreset();
  if (step === 'select') requestAnimationFrame(fitCanvas);
}

function renderSteps() {
  if (!state.manifest) return;
  $$('.step').forEach(b => {
    const em = $('em', b);
    const st = (state.manifest.stages || {})[b.dataset.step];
    // 挑帧页用 selection 数量表示状态
    let status = st?.status || 'idle';
    if (b.dataset.step === 'select') status = (state.manifest.selection || []).length ? 'done' : status;
    if (b.dataset.step === 'frames') status = (state.manifest.files.frames || []).length ? 'done' : status;
    if (b.dataset.step === 'reference') {
      status = (state.manifest.files.reference || []).some(f => f.name === 'front.png') ? 'done' : status;
    }
    if (b.dataset.step === 'keyframe') status = keyList().length ? 'done' : status;
    em.className = status === 'done' ? 'done' : status === 'failed' ? 'failed' : status === 'running' ? 'running' : '';
    em.title = status;
  });
}

const STAGE_NAMES = {
  reference: '① 正面设定', keyframe: '② 单帧', video: '③ 视频', frames: '④ 切帧',
  sprites: '⑥ 重绘 / 抠图', export: '⑦ 导出',
};

function startJob(body, title) {
  if (!state.manifest) { alert('请先新建或选择项目'); return; }
  // 「强制重跑」不再是可选项：每次执行都覆盖该步已有产物
  api(`/api/projects/${encodeURIComponent(state.manifest.name)}/run`, {
    method: 'POST', body: { force: true, ...body },
  }).then(({ job_id }) => {
    state.job = job_id; state.logCount = 0;
    $('#running').hidden = false;
    $('#runningText').textContent = `${title} 运行中…`;
    $('#jobState').textContent = `任务 ${job_id} 运行中`;
    logLine(`===== 启动任务 ${job_id}（${body.stage}）=====`);
    pollJob(job_id, 0);
  }).catch(e => alert('启动失败：' + e.message));
}

function runStage(stage) {
  startJob({ stage }, STAGE_NAMES[stage] || stage);
}

// 状态轮询连续失败时的退避（毫秒），到达上限后保持 5s 一直重试，不放弃任务
const POLL_BACKOFF = [1000, 1000, 2000, 3000, 5000];

function resetCancelButton() {
  const b = $('#btnCancel');
  if (b) { b.disabled = false; b.textContent = '取消'; }
}

function stopJobPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.job = null;
  $('#running').hidden = true;
  $('#jobState').textContent = '空闲';
  resetCancelButton();
}

async function resumeJob(name) {
  if (!name) return;
  let jobs;
  try { jobs = (await api('/api/jobs')).jobs || []; }
  catch (_) { return; }                    // 服务暂时不可用，由下次手动操作重试
  const running = jobs.find(j => j.project === name && j.status === 'running');
  if (!running) return;
  state.job = running.id;
  $('#running').hidden = false;
  $('#runningText').textContent = `${running.stage === 'all' ? '完整流水线' : (STAGE_NAMES[running.stage] || running.stage)} 运行中（已恢复监控）…`;
  $('#jobState').textContent = `任务 ${running.id} 运行中`;
  try {
    const full = await api(`/api/jobs/${running.id}?since=0`, { allowErrorField: true });
    (full.logs || []).slice(-20).forEach(logLine);   // 只回放最近日志，避免刷屏
    state.logCount = full.log_count ?? (full.logs || []).length;
    logLine(`===== 已恢复监控后台任务 ${running.id}（${running.stage}）=====`);
  } catch (_) { /* 取历史日志失败不影响继续监控 */ }
  pollJob(running.id, 0);
}

function pollJob(id, failCount = 0) {
  clearTimeout(state.pollTimer);
  if (state.job !== id) return;                     // 已切换项目 / 已结束的旧轮询
  api(`/api/jobs/${id}?since=${state.logCount}`, { allowErrorField: true }).then(job => {
    if (state.job !== id) return;
    if (failCount) logLine(`任务状态连接已恢复（自动重试 ${failCount} 次）`);
    (job.logs || []).forEach(logLine);
    state.logCount = job.log_count ?? state.logCount + (job.logs || []).length;
    $('#running').hidden = false;
    if (job.status === 'running') {
      $('#jobState').textContent = `任务 ${id} 运行中`;
      state.pollTimer = setTimeout(() => pollJob(id, 0), 1000);
      return;
    }
    $('#running').hidden = true;
    const statusText = { done: '已完成', failed: '失败', cancelled: '已取消' }[job.status] || job.status;
    $('#jobState').textContent = `任务 ${id} ${statusText}`;
    logLine(`===== 任务结束：${statusText}${job.error ? ' — ' + job.error : ''} =====`);
    resetCancelButton();
    const name = state.manifest?.name;
    state.job = null;
    if (name) loadManifest(name).catch(() => loadProjects());
  }).catch(e => {
    if (state.job !== id) return;
    // 状态接口一次失败不代表任务结束：后台任务仍在跑，退避后继续轮询
    const n = failCount + 1;
    const wait = POLL_BACKOFF[Math.min(n - 1, POLL_BACKOFF.length - 1)];
    $('#running').hidden = false;
    $('#jobState').textContent = `状态获取失败，${Math.round(wait / 1000)}s 后自动重试（第 ${n} 次）`;
    if (n === 1 || n % 10 === 0) {
      logLine(`状态轮询失败（第 ${n} 次）：${e.message}；任务仍在后台运行，不会放弃，将自动重试`);
    }
    state.pollTimer = setTimeout(() => pollJob(id, n), wait);
  });
}

/* ---------------------------------------------------------------- 渲染 */
function tile(stage, f, label, checker = false) {
  return `<div class="tile"><div class="thumb ${checker ? 'checker' : ''}">
      <img src="${f.url}" alt="${f.name}"></div>
    <div class="cap"><b>${label || f.name}</b><span>${(f.size / 1024).toFixed(0)} KB</span></div></div>`;
}

function loadText(url) {
  return fetch(url).then(r => r.ok ? r.text() : '').catch(() => '');
}

/* ---------------------------------------------------------------- 帧组视图 */
function allFrames() {
  return (state.manifest?.files?.frames || []).filter(f => /\.png$/i.test(f.name));
}

function frameGroups() { return state.manifest?.frame_groups || []; }

function currentGroup() {
  const ids = frameGroups().map(g => g.id);
  const want = state.groupFilter ?? state.manifest?.params?.frame_group ?? '';
  if (ids.includes(want)) return want;
  return ids.length ? ids[0] : '';
}

// ④ 选中的切帧源对应的帧组 id；老版 loop.mp4 对应平铺组 ''
function frameSourceGroup() {
  const m = state.manifest;
  if (!m) return null;
  const vids = m.videos || [];
  const chosen = $('#frameSource')?.value || '';
  const rec = chosen ? vids.find(v => v.file === chosen) : vids[vids.length - 1];
  if (!rec) return null;
  if (rec.legacy || rec.file === 'loop.mp4') return '';
  return String(rec.file || '').replace(/\.[^.]+$/, '');
}

function renderFrameViews() {
  const m = state.manifest;
  if (!m) return;
  const groups = frameGroups();
  const ids = groups.map(g => g.id);
  // ④ 换了切帧源、而这一段的帧还没切出来时，序列帧跟着显示空态，
  // 不能继续停在上一视频段的图上让人误以为已经切好了
  const pending = state.sourcePreviewGroup;
  const waiting = pending !== null && pending !== undefined && !ids.includes(pending);
  const gid = waiting ? pending : currentGroup();
  const gmeta = groups.find(g => g.id === gid) || {};
  const gname = gid ? (gmeta.label || videoLabel(gid) || gid) : '旧版帧序列';
  const sel = $('#frameGroup');
  if (!ids.length) {
    sel.innerHTML = '<option value="">暂无帧组</option>';
  } else {
    sel.innerHTML = ids.map(id => {
      const g = groups.find(x => x.id === id) || {};
      const name = id === '' ? '旧版帧序列' : (g.label || videoLabel(id) || id);
      return `<option value="${esc(id)}">${esc(name)}（${g.count ?? 0} 帧）</option>`;
    }).join('');
  }
  if (waiting) {
    sel.innerHTML = `<option value="${esc(gid)}">${esc(gname)}（尚未切帧）</option>` + sel.innerHTML;
  }
  sel.value = waiting ? gid : currentGroup();
  const frames = allFrames().filter(f => (f.group || '') === gid);
  const emptyMsg = waiting
    ? `所选视频段「${esc(gname)}」还没有切帧，点上面的「执行切帧」生成这一段的帧`
    : '该帧组还没有帧，请先在第 ④ 步选择视频段执行切帧';
  $('#frameStrip').innerHTML = frames.slice(0, 120).map(f =>
    `<img loading="lazy" src="${f.url}" title="${esc(frameKey(f))}">`).join('')
    || `<div class="muted">${emptyMsg}</div>`;
  // ④ 帧序列上方：当前是哪一组、有多少帧，以及「只删这一组」
  const hint = $('#frameStripHint'), clearBtn = $('#btnClearFrameGroup');
  if (hint) hint.textContent = frames.length
    ? `当前显示：${gname} · ${frames.length} 帧`
    : (waiting ? `「${gname}」还没有帧，请先执行切帧` : '当前帧组还没有帧');
  if (clearBtn) clearBtn.disabled = !gid || !frames.length;
  state.frames = frames;
  renderFrameGrid(frames);
  $('#selCount').textContent = `已选 ${state.selection.size} 帧（全项目）`;
  preparePlayback();
}

async function changeFrameGroup(value) {
  state.groupFilter = value;
  state.sourcePreviewGroup = null;
  // ④ 的隐藏参数与 ⑤ 的帧组下拉保持一致，避免后续 saveParams 把旧值写回去
  const hidden = $('#frameGroupParam');
  if (hidden) hidden.value = value;
  renderFrameViews();
  if (state.manifest) {
    try {
      const r = await api(`/api/projects/${encodeURIComponent(state.manifest.name)}/params`,
        { method: 'POST', body: { params: { frame_group: value } } });
      state.manifest.params = r.params;
      logLine(`挑帧页已切到帧组「${value || '旧版帧序列'}」`);
    } catch (e) { alert('保存帧组失败：' + e.message); }
  }
  requestAnimationFrame(fitCanvas);
}

async function renderAll() {
  if (!state.manifest) return;
  const token = ++state.renderToken;
  const m = state.manifest;
  // 异步读取的产物（提示词/统计文本）只属于本次渲染对应的项目
  const alive = () => token === state.renderToken && state.manifest?.name === m.name;
  renderSteps();
  const baseFile = (m.files.source || []).find(f => f.name === 'base.png');
  $('#basePreview').innerHTML = baseFile
    ? `<img src="${baseFile.url}" alt="角色底图"><span>已上传底图 · ${(baseFile.size / 1024).toFixed(0)} KB</span>`
    : '未上传底图';
  $('#basePreview').classList.toggle('muted', !baseFile);
  markActiveSwatch();
  fillDirs();
  renderExtraViews();
  const ref = m.files.reference || [];

  // ① 正面设定立绘（全流程的角色基准）
  const sheet = ref.find(f => f.name === 'front.png');
  $('#refSheet').innerHTML = sheet
    ? `<img src="${sheet.url}" alt="正面立绘" data-zoom data-caption="正面设定 · 全流程的角色基准">
       <span>${esc(sheet.name)} · ${(sheet.size / 1024).toFixed(0)} KB · 点图放大</span>`
    : '尚未生成';
  $('#refSheet').classList.toggle('muted', !sheet);
  $('#refFiles').innerHTML = sheet
    ? tile('reference', sheet, '正面设定 · 全流程的角色基准')
    : '<div class="muted">尚未生成。填好角色描述后点「生成正面立绘」。</div>';
  const promptPath = m.paths.reference + '/front.prompt.txt';
  // 这个 txt 只有跑过 ① 才有；没跑过就请求只会在控制台刷 404
  const sheetPrompt = sheet && m.stages?.reference?.status === 'done'
    ? await loadText(`/file?project=${encodeURIComponent(m.name)}&path=${promptPath}`)
    : '';
  if (!alive()) return;              // 期间已切换项目：不要把旧项目内容写进新页面
  $('#refPrompts').textContent = sheetPrompt || '—';
  $('#refPrompts').classList.toggle('muted', !sheetPrompt);

  // ② 单帧库
  renderKeys();

  // ③ 视频段列表 / 预览
  renderVideoList();
  updateFrameSourceInfo();

  // ④ / ⑤ 切帧结果 + 挑帧（按帧组隔离，不同动作 / 方向不会混在一起）
  renderFrameViews();
  const fmUrl = `/file?project=${encodeURIComponent(m.name)}&path=${m.paths.frames}/manifest.json`;
  if (!(m.files.frames || []).length) {
    $('#frameStats').textContent = '—';    // 还没切过帧，清单文件不存在，别去请求
  } else loadText(fmUrl).then(t => {
    if (!alive()) return;
    if (!t) { $('#frameStats').textContent = allFrames().length ? `共 ${allFrames().length} 帧` : '—'; return; }
    try {
      const d = JSON.parse(t);
      $('#frameStats').textContent = `上次切帧：${d.label || d.source} · 共 ${d.count} 帧 · ` +
        `抽取 ${d.extract_fps} fps · 源视频 ${d.video?.width}×${d.video?.height} / ${d.video?.duration}s`;
    } catch (_) { $('#frameStats').textContent = '—'; }
  });

  // ⑥ 精灵帧：按动作分组展示，每组独立目录 / 独立质检
  renderSpriteGroups();

  // ⑦ 导出：每组一套 spritesheet / GIF / ZIP
  renderExportGroups();
}

/* ------------------------------------------------ 去背景 / 导出（按组隔离） */
// 阶段产物统一带 group 字段（相对阶段目录的第一段路径），根目录产物 group 为 ''
function groupAssets(stage, groupId, test) {
  return (state.manifest?.files?.[stage] || [])
    .filter(f => (f.group || '') === groupId && (!test || test(f)))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function groupTitle(g) { return g.label || videoLabel(g.id) || g.id || '旧版帧序列'; }

// 帧组 id 就是视频段 id；帧组自己的 manifest 没写 label 时，回退到视频段的中文名
function videoLabel(id) {
  if (!id) return '';
  const v = (state.manifest?.videos || []).find(r => r.id === id ||
    String(r.file || '').replace(/\.[^.]+$/, '') === id);
  return v ? (v.label || '') : '';
}

async function renderSpriteGroups() {
  const m = state.manifest;
  if (!m) return;
  const token = state.renderToken;
  const groups = m.sprite_groups || [];
  const box = $('#spriteGrid');
  if (!groups.length) {
    box.innerHTML = '<div class="muted">尚未重绘。</div>';
    $('#spriteStats').textContent = '—';
    $('#alphaReport').innerHTML = '重绘完成后这里按动作分组显示每帧透明占比；不透明占比越低，说明背景去得越干净。';
    return;
  }
  box.innerHTML = groups.map(g => {
    const frames = groupAssets('sprites', g.id, f => /\.png$/i.test(f.name));
    const dir = `06_sprites/${g.id ? g.id + '/' : ''}`;
    return `<div class="group-block">
      <div class="group-head"><b>${esc(groupTitle(g))}</b>
        <span class="pill">${frames.length} 帧</span>
        <span class="muted group-path">${esc(dir)}</span>
        ${g.id ? `<span class="grow"></span>
        <button class="btn ghost tiny" data-sprite-only="${esc(g.id)}" title="只重绘这一段视频的帧，其它组不动">只重绘这组</button>
        <button class="btn ghost danger tiny" data-clear-group="sprites" data-group="${esc(g.id)}"
          data-label="${esc(groupTitle(g))}" title="只删这一组的精灵帧（连同它的导出），其它组不动">删除这组</button>` : ''}</div>
      <div class="media-grid">${frames.map(f => tile('sprites', f, f.name, true)).join('') ||
        '<div class="muted">该组还没有精灵帧</div>'}</div>
    </div>`;
  }).join('');
  $$('[data-sprite-only]', box).forEach(b => {
    b.onclick = () => runScoped('#spriteScope', b.dataset.spriteOnly, 'sprites');
  });
  $$('[data-clear-group]', box).forEach(b => {
    b.onclick = () => clearGroup(b.dataset.clearGroup, b.dataset.group, b.dataset.label);
  });

  // 每组自己的 alpha_report.json：不能只读根目录那一份了
  const reports = await Promise.all(groups.map(async g => {
    const dir = g.id ? `${m.paths.sprites}/${g.id}` : m.paths.sprites;
    const t = await loadText(`/file?project=${encodeURIComponent(m.name)}&path=${dir}/alpha_report.json&_=${Date.now()}`);
    try { return t ? JSON.parse(t) : null; } catch (_) { return null; }
  }));
  if (token !== state.renderToken) return;      // 期间切了项目，别把旧数据写进新页面

  let total = 0, weighted = 0, weighed = 0;
  const rows = groups.map((g, i) => {
    const entries = reports[i] ? Object.entries(reports[i]) : [];
    const count = g.count || entries.length;
    // 目录被迁移/重切后，旧报告可能与当前帧数不一致，这种脏数据不能参与统计
    const fresh = entries.length === count;
    const avg = fresh ? entries.reduce((s, [, v]) => s + (v.opaque_ratio || 0), 0) / entries.length : 0;
    total += count;
    if (fresh) { weighted += avg * count; weighed += count; }
    return { g, entries, avg, count, fresh };
  });
  $('#spriteStats').textContent = `共 ${groups.length} 组 · ${total} 帧` +
    (weighed ? ` · 平均不透明占比 ${(weighted / weighed * 100).toFixed(1)}%` : '');
  $('#alphaReport').innerHTML = rows.map(({ g, entries, avg, count, fresh }) => {
    const worst = fresh
      ? entries.slice().sort((a, b) => (b[1].opaque_ratio || 0) - (a[1].opaque_ratio || 0)).slice(0, 4)
      : [];
    return `<div class="ar-group">
      <div class="ar-head"><b>${esc(groupTitle(g))}</b>
        <span>${count} 帧${fresh ? ` · 不透明 ${(avg * 100).toFixed(1)}%` : ''}</span></div>
      ${worst.map(([name, v]) => `<div class="ar-row">${esc(name)} · ${v.width}×${v.height} · ` +
        `透明 ${((v.transparent_ratio || 0) * 100).toFixed(0)}% · 不透明 ${((v.opaque_ratio || 0) * 100).toFixed(0)}%</div>`).join('')}
      ${fresh ? '' : '<div class="ar-row muted">质检数据与当前帧数不匹配，重新执行一次「重绘 / 抠图」</div>'}
    </div>`;
  }).join('');
}

function renderExportGroups() {
  const m = state.manifest;
  if (!m) return;
  const groups = m.export_groups || [];
  const box = $('#exportBox');
  if (!box) return;
  if (!groups.length) {
    box.innerHTML = '<div class="muted">还没有导出文件，点上面「生成导出文件」。</div>';
    return;
  }
  const kb = f => `${(f.size / 1024).toFixed(0)} KB`;
  // 预览与下载放进同一块，组的名字只出现一次——看图和拿文件就不会对不上号
  box.innerHTML = groups.map(g => {
    const files = groupAssets('export', g.id);
    const sheet = files.find(f => f.name === 'spritesheet.png');
    const meta = files.find(f => f.name === 'spritesheet.json');
    const gif = files.find(f => f.name === 'preview.gif');
    const zip = files.find(f => /\.zip$/i.test(f.name));
    const title = groupTitle(g);
    const dir = `export/${g.id ? g.id + '/' : ''}`;
    return `<div class="group-block">
      <div class="group-head"><b>${esc(title)}</b>
        <span class="pill">${g.count} 个文件</span>
        <span class="muted group-path">${esc(dir)}</span>
        ${g.id ? `<span class="grow"></span>
        <button class="btn ghost tiny" data-export-only="${esc(g.id)}" title="只重新导出这一段视频的那套文件">只导出这组</button>
        <button class="btn ghost danger tiny" data-clear-group="export" data-group="${esc(g.id)}"
          data-label="${esc(title)}" title="只删这一组的 spritesheet / GIF / ZIP，其它组不动">删除这组</button>` : ''}</div>
      <div class="export-media">
        <figure class="fig-sheet">
          <div class="ex-img checker">${sheet
            ? `<img src="${sheet.url}" alt="${esc(title)} spritesheet" data-zoom
                 data-caption="${esc(title)} · spritesheet.png${meta ? ' + spritesheet.json' : ''}">`
            : '<span class="muted">该组还没有 spritesheet</span>'}</div>
          <figcaption><b>spritesheet.png</b>
            <span>${sheet ? kb(sheet) : '—'}${meta ? ` · 帧矩形见 spritesheet.json（${kb(meta)}）` : ''}</span></figcaption>
        </figure>
        <figure class="fig-gif">
          <div class="ex-img checker">${gif
            ? `<img src="${gif.url}" alt="${esc(title)} 循环预览" class="gif-preview" data-zoom
                 data-caption="${esc(title)} · preview.gif 循环预览">`
            : '<span class="muted">该组还没有 GIF</span>'}</div>
          <figcaption><b>preview.gif</b><span>${gif ? kb(gif) + ' · 循环预览' : '—'}</span></figcaption>
        </figure>
      </div>
      <div class="ex-links">
        ${[sheet, meta, gif, zip].filter(Boolean).map(f =>
          `<a href="${f.url}" download>${esc(f.name)} <span class="muted">· ${kb(f)}</span></a>`).join('')
          || '<span class="muted">该组还没有可下载的文件</span>'}
      </div>
    </div>`;
  }).join('');
  $$('[data-export-only]', box).forEach(b => {
    b.onclick = () => runScoped('#exportScope', b.dataset.exportOnly, 'export');
  });
  $$('[data-clear-group]', box).forEach(b => {
    b.onclick = () => clearGroup(b.dataset.clearGroup, b.dataset.group, b.dataset.label);
  });
}

/* ---------------------------------------------------------------- 挑帧 + 播放器 */
function renderFrameGrid(frames) {
  $('#frameGrid').innerHTML = frames.map((f, i) => {
    const k = frameKey(f);
    return `<div class="fcell ${state.selection.has(k) ? 'sel' : ''}" data-name="${esc(k)}" data-i="${i}" title="${esc(k)}">
      <img loading="lazy" src="${f.url}">
      <span class="idx">${i}</span><span class="tick">${state.selection.has(k) ? '✓' : ''}</span>
    </div>`;
  }).join('') || '<div class="muted">该帧组还没有帧，请先执行第 ④ 步切帧（可切换上方帧组）。</div>';
  $$('#frameGrid .fcell').forEach(c => {
    c.onclick = () => {
      const k = c.dataset.name;
      if (state.selection.has(k)) state.selection.delete(k); else state.selection.add(k);
      c.classList.toggle('sel');
      $('.tick', c).textContent = state.selection.has(k) ? '✓' : '';
      $('#selCount').textContent = `已选 ${state.selection.size} 帧（全项目）`;
      preparePlayback();
    };
  });
}

function preparePlayback() {
  const frames = state.frames || [];
  const onlySel = $('#playOnlySelected').checked;
  const list = (onlySel && state.selection.size ? frames.filter(f => state.selection.has(frameKey(f))) : frames);
  state.playFrames = list;
  state.playIndex = 0;
  // 缓存 key 必须用带项目名的完整 URL：所有项目的帧都叫 frame_XXXX.png，
  // 用文件名做 key 切换项目时会直接复用上一个项目的图。
  const cache = state.images || {};
  list.forEach(f => {
    if (cache[f.url]) return;
    const img = new Image();
    img.onload = () => {
      const cur = state.playFrames[state.playIndex];
      if (cur && cur.url === f.url) drawFrame();
    };
    img.src = f.url;
    cache[f.url] = img;
  });
  state.images = cache;
  $('#playRange').max = Math.max(0, list.length - 1);
  $('#playRange').value = 0;
  drawFrame();
}

function fitCanvas() {
  // canvas 的 CSS 百分比高度在 grid 容器里不可靠：按容器可用空间精确算出等比显示尺寸，
  // 保证 640×640 之类的帧能完整显示，不会因为容器 overflow:hidden 被裁掉半张。
  const canvas = $('#player');
  const wrap = canvas?.parentElement;
  if (!canvas || !wrap || !canvas.width || !canvas.height) return;
  const availW = wrap.clientWidth - 16, availH = wrap.clientHeight - 16;
  if (availW < 90 || availH < 90) return;          // 面板隐藏时先不计算
  const scale = Math.min(1, availW / canvas.width, availH / canvas.height);
  canvas.style.width = Math.max(1, Math.round(canvas.width * scale)) + 'px';
  canvas.style.height = Math.max(1, Math.round(canvas.height * scale)) + 'px';
}

function drawFrame() {
  const list = state.playFrames || [];
  const canvas = $('#player'), ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!list.length) {
    $('#playInfo').textContent = '没有可播放的帧';
    return;
  }
  const f = list[state.playIndex];
  const img = state.images[f.url];
  if (img && img.complete && img.naturalWidth) {
    if (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight) {
      canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
    }
    ctx.drawImage(img, 0, 0);
  }
  fitCanvas();
  $('#playInfo').textContent = `${state.playIndex + 1} / ${list.length} · ${frameKey(f)}`;
  $('#playRange').value = state.playIndex;
  $$('#frameGrid .fcell').forEach(c => c.classList.toggle('cur', c.dataset.name === frameKey(f)));
}

function setPlay(on) {
  state.playing = on;
  $('#btnPlay').textContent = on ? '⏸ 暂停' : '▶ 播放';
  clearInterval(state.timer);
  if (!on) return;
  const fps = Math.max(1, Math.min(60, Number($('#playFps').value) || 12));
  state.timer = setInterval(() => {
    const n = state.playFrames.length;
    if (!n) return setPlay(false);
    state.playIndex = (state.playIndex + 1) % n;
    drawFrame();
  }, 1000 / fps);
}

async function saveSelection() {
  if (!state.manifest) return;
  const files = [...state.selection];   // 跨帧组保留全部已选帧，不只是当前组
  const r = await api(`/api/projects/${encodeURIComponent(state.manifest.name)}/selection`,
    { method: 'POST', body: { files } });
  state.manifest.selection = r.selection;
  renderSteps();
  logLine(`已保存帧选择：${r.selection.length} 帧`);
}

/* ---------------------------------------------------------------- 事件绑定 */
function bind() {
  bindLogResize();
  bindZoom();
  initBackgroundControls();
  $('#btnUploadBase').onclick = () => uploadBaseImage().catch(e => alert(e.message));
  $('#btnClearBase').onclick = () => clearBaseImage();
  $('#btnRefreshGif').onclick = refreshGif;
  $('#btnNew').onclick = newProject;
  $('#btnDeleteProject').onclick = deleteProject;
  $('#btnSettings').onclick = settings;
  $('#projectSelect').onchange = e => {
    if (!e.target.value) {
      // 选回占位行 = 取消选择：清掉项目上下文，左侧步骤随之禁用
      stopJobPolling();
      state.manifest = null;
      state.selection = new Set();
      state.frames = [];
      state.playFrames = [];
      state.playIndex = 0;
      state.images = {};
      renderEmpty();
      return;
    }
    loadManifest(decodeURIComponent(e.target.value), { resume: true });
  };
  $$('.step').forEach(b => b.onclick = () => showStep(b.dataset.step));
  $('#frameGroup').addEventListener('change', e => changeFrameGroup(e.target.value));
  $('#btnClearFrameGroup').onclick = () => {
    const gid = currentGroup();
    if (!gid) return;
    const g = frameGroups().find(x => x.id === gid) || {};
    clearGroup('frames', gid, g.label || videoLabel(gid) || gid);
  };
  $('#frameSource').addEventListener('change', () => {
    updateFrameSourceInfo();
    if (!state.manifest) return;
    const gid = frameSourceGroup();
    if (gid === null) return;
    const exists = frameGroups().some(g => g.id === gid);
    // 已切过帧 → 序列帧 / 挑帧网格一起切到这一组；还没切 → 显示空态，
    // 别继续停在上一视频段的帧上。frame_source + frame_group 由通用
    // saveParams 一次性提交，避免两个并发请求互相覆盖参数。
    state.sourcePreviewGroup = exists ? null : gid;
    if (exists) {
      state.groupFilter = gid;
      const hidden = $('#frameGroupParam');
      if (hidden) hidden.value = gid;
    }
    renderFrameViews();
  });
  $$('[data-run]').forEach(b => b.onclick = async () => { await saveParams(); runStage(b.dataset.run); });
  $$('[data-param]').forEach(el => {
    el.addEventListener('change', () => { if (state.manifest) assign(state.manifest.params, el.dataset.param, parseValue(el)); });
    el.addEventListener('blur', () => { if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') saveParams(); });
    if (el.tagName === 'SELECT' || el.type === 'checkbox') el.addEventListener('change', saveParams);
  });
  $('#btnAddView').onclick = addExtraView;
  $$('#dirPicks input[data-dir]').forEach(i => {
    i.addEventListener('change', () => {
      if (!$$('#dirPicks input[data-dir]').some(x => x.checked)) {
        i.checked = true;                      // 至少要保留一个方向
        toast('至少要保留一个方向', 'bad');
        return;
      }
      saveDirs();
    });
  });
  $('#firstKeySelect').addEventListener('change', () => { renderKeys(); updateKeyPicked(); });
  $('#lastKeySelect').addEventListener('change', updateKeyPicked);
  $('#btnCancel').onclick = async () => {
    if (!state.job) return;
    const btn = $('#btnCancel');
    btn.disabled = true;
    btn.textContent = '正在停止…';
    try {
      const r = await api(`/api/jobs/${state.job}/cancel`, { method: 'POST', body: {} });
      logLine(r?.interrupting
        ? '已请求取消：正在中断 ComfyUI 本任务，本地步骤立即停止'
        : '已请求取消：本任务尚未提交 ComfyUI，本地步骤立即停止');
    } catch (e) {
      resetCancelButton();
      logLine('取消失败：' + e.message);
    }
  };
  $('#btnClearLog').onclick = () => { $('#log').textContent = ''; };

  $('#btnPlay').onclick = () => setPlay(!state.playing);
  $('#btnPrev').onclick = () => { const n = state.playFrames.length; if (n) { state.playIndex = (state.playIndex - 1 + n) % n; drawFrame(); } };
  $('#btnNext').onclick = () => { const n = state.playFrames.length; if (n) { state.playIndex = (state.playIndex + 1) % n; drawFrame(); } };
  $('#playFps').onchange = () => { if (state.playing) setPlay(true); };
  $('#playOnlySelected').onchange = () => { setPlay(false); preparePlayback(); };
  $('#playRange').oninput = e => { state.playIndex = Number(e.target.value); drawFrame(); };
  $('#btnSelAll').onclick = () => { (state.frames || []).forEach(f => state.selection.add(frameKey(f))); renderFrameGrid(state.frames); $('#selCount').textContent = `已选 ${state.selection.size} 帧（全项目）`; preparePlayback(); };
  $('#btnSelNone').onclick = () => {
    (state.frames || []).forEach(f => state.selection.delete(frameKey(f)));  // 只清当前帧组
    renderFrameGrid(state.frames);
    $('#selCount').textContent = `已选 ${state.selection.size} 帧（全项目）`;
    preparePlayback();
  };
  $('#btnSelInvert').onclick = () => {
    (state.frames || []).forEach(f => { const k = frameKey(f); state.selection.has(k) ? state.selection.delete(k) : state.selection.add(k); });
    renderFrameGrid(state.frames); $('#selCount').textContent = `已选 ${state.selection.size} 帧（全项目）`; preparePlayback();
  };
  $('#btnSelEven').onclick = () => {
    const fs = state.frames || [], n = Math.max(1, Number($('#evenN').value) || 8);
    // 只重排当前帧组的选择，其它视频段已选的帧保持不动
    fs.forEach(f => state.selection.delete(frameKey(f)));
    if (fs.length <= n) fs.forEach(f => state.selection.add(frameKey(f)));
    else for (let i = 0; i < n; i++) state.selection.add(frameKey(fs[Math.round(i * (fs.length - 1) / Math.max(1, n - 1))]));
    renderFrameGrid(fs); $('#selCount').textContent = `已选 ${state.selection.size} 帧（全项目）`; preparePlayback();
  };
  $('#btnSaveSel').onclick = () => saveSelection().catch(e => alert(e.message));
  const motionEl = $('[data-param="video_motion"]');
  if (motionEl) motionEl.addEventListener('input', markActivePreset);
  document.addEventListener('keydown', e => {
    if (state.step !== 'select' || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    if (e.code === 'Space') { e.preventDefault(); setPlay(!state.playing); }
    if (e.code === 'ArrowLeft') $('#btnPrev').click();
    if (e.code === 'ArrowRight') $('#btnNext').click();
  });
}

(async function init() {
  bind();
  loadPresets();
  await Promise.all([loadHealth(), loadWorkflows()]);
  const qs = new URLSearchParams(location.search);
  try { await loadProjects(qs.get('project')); }
  catch (e) { logLine('项目列表加载失败：' + e.message); }
  const step = qs.get('step');
  if (step && STAGES.includes(step)) showStep(step);
  else if (!state.manifest) showStep('reference');
  resumeJob(state.manifest?.name);   // 刷新页面后继续监控后台任务
})();

