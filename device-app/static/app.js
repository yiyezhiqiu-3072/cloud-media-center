/* 客户分切设备管理 —— 主页宫格 / 基础数据 / 单据表单 / 序时簿 / 总览
 * 接口走相对路径 api/...（挂在 nginx /device/ 下同样可用） */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  const state = {
    view: 'home', meta: null, options: {}, records: [], overview: null,
    formType: null, editing: null, photos: {}, optionKind: null, ledgerType: null,
    filters: { from: '', to: '', machine: '', status: '', q: '' }, busy: false,
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, { cache: 'no-store', credentials: 'same-origin', ...opts });
    let body = null;
    try { body = await res.json(); } catch (_) { /* 非 JSON */ }
    if (res.status === 401) {
      const e = new Error('请先登录'); e.status = 401; throw e;
    }
    if (!res.ok) {
      const e = new Error((body && (body.message || body.error)) || ('HTTP ' + res.status));
      e.status = res.status; throw e;
    }
    return body || {};
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }
  function todayStr() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
      '-' + String(d.getDate()).padStart(2, '0');
  }
  function fmtStamp(sec) {
    if (!sec) return '';
    const d = new Date(Number(sec) * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  let toastTimer;
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2400);
  }

  const GROUPS = [
    { title: '工厂基础数据', icon: 'factory', items: [
      { k: 'bases', label: '基地', t: 'option' },
      { k: 'workshops', label: '车间', t: 'option' },
      { k: 'machines', label: '机台', t: 'option' },
    ] },
    { title: '设备基础数据', icon: 'device', items: [
      { k: 'camera_models', label: '相机型号', t: 'option' },
      { k: 'software_versions', label: '软件版本', t: 'option' },
      { k: 'dongle_versions', label: '加密狗版本', t: 'option' },
      { k: 'dongle_methods', label: '加密狗安装方式', t: 'option' },
      { k: 'ipc_cpus', label: '工控机CPU', t: 'option' },
    ] },
    { title: '视觉相关', icon: 'visual', items: [
      { k: 'basic', label: '分切机基本信息表', t: 'form' },
      { k: 'vision', label: '分切机F品视觉防呆', t: 'form' },
      { k: 'software', label: '分切机软件与工控机', t: 'form' },
      { k: 'centering', label: '分切机卷芯居中', t: 'form' },
      { k: 'maintenance', label: '设备维护表', t: 'form' },
      { k: 'summary', label: '分切机总览', t: 'overview' },
    ] },
    { title: '视觉相关序时簿', icon: 'ledger', items: [
      { k: 'basic', label: '分切机基本信息表', t: 'ledger' },
      { k: 'vision', label: '分切机F品视觉防呆', t: 'ledger' },
      { k: 'software', label: '分切机软件与工控机', t: 'ledger' },
      { k: 'centering', label: '分切机卷芯居中', t: 'ledger' },
      { k: 'maintenance', label: '设备维护表', t: 'ledger' },
    ] },
  ];

  const ICON = '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11M15 9v11"/>' +
    '<path d="M6 6h.01M10 6h.01"/></svg>';

  function head(title, sub, back) {
    return '<div class="page-head">' +
      (back ? '<button type="button" class="back-btn" data-act="home">←</button>' : '') +
      '<div><h2>' + esc(title) + '</h2>' + (sub ? '<p>' + esc(sub) + '</p>' : '') + '</div>' +
      '</div>';
  }

  // ---------------------------------------------------------------- 主页
  function renderHome() {
    let html = '<div class="home-tip">选择功能开始录入或查询</div>';
    GROUPS.forEach((g) => {
      html += '<section class="group"><h3>' + esc(g.title) + '</h3><div class="grid">';
      g.items.forEach((it) => {
        html += '<button type="button" class="tile" data-act="' + it.t + '" data-k="' + it.k + '">' +
          '<span class="tile-ico">' + ICON + '</span>' +
          '<span class="tile-label">' + esc(it.label) + '</span></button>';
      });
      html += '</div></section>';
    });
    return html;
  }

  // ---------------------------------------------------------------- 基础数据
  function renderOptions(kind) {
    const info = (state.meta.option_kinds || {})[kind] || { label: kind };
    const items = state.options[kind] || [];
    let html = head(info.label, '共 ' + items.length + ' 项 · 供单据下拉选择', true);
    html += '<section class="panel"><div class="add-row">' +
      '<input id="optName" maxlength="80" placeholder="新增' + esc(info.label) + '名称">' +
      '<button type="button" class="primary" data-act="option-add">添加</button></div>';
    html += '<div class="opt-list">';
    if (!items.length) html += '<div class="empty">还没有数据，先在上面添加一项。</div>';
    items.forEach((o) => {
      html += '<div class="opt-row"><span class="opt-name">' + esc(o.name) + '</span>' +
        '<span class="opt-ops">' +
        '<button type="button" class="mini-btn" data-act="option-edit" data-id="' + esc(o.id) + '">改名</button>' +
        '<button type="button" class="mini-btn danger" data-act="option-del" data-id="' + esc(o.id) + '">删除</button>' +
        '</span></div>';
    });
    html += '</div></section>';
    return html;
  }

  // ---------------------------------------------------------------- 单据表单
  function fieldHtml(f, val) {
    const v = (val === undefined || val === null || val === '') ? (f.default || '') : val;
    const req = f.required ? ' <i>*</i>' : '';
    if (f.type === 'date') {
      const d = (v === 'today' || !v) ? todayStr() : v;
      return '<label class="fld"><span>' + esc(f.label) + req + '</span>' +
        '<input type="date" data-f="' + f.key + '" value="' + esc(d) + '"></label>';
    }
    if (f.type === 'select') {
      const opts = (state.options[f.options] || []).map((o) =>
        '<option value="' + esc(o.name) + '"' + (o.name === v ? ' selected' : '') + '>' +
        esc(o.name) + '</option>').join('');
      return '<label class="fld"><span>' + esc(f.label) + req + '</span>' +
        '<select data-f="' + f.key + '"><option value="">请选择</option>' + opts + '</select></label>';
    }
    if (f.type === 'number') {
      return '<label class="fld"><span>' + esc(f.label) + req + '</span>' +
        '<input type="number" step="any" data-f="' + f.key + '" value="' + esc(v) + '"></label>';
    }
    if (f.type === 'switch') {
      return '<label class="fld switch-row"><span>' + esc(f.label) + req + '</span>' +
        '<input type="checkbox" class="sw" data-f="' + f.key + '"' + (v ? ' checked' : '') + '>' +
        '</label>';
    }
    if (f.type === 'textarea') {
      return '<label class="fld col"><span>' + esc(f.label) + req + '</span>' +
        '<textarea rows="3" data-f="' + f.key + '" placeholder="' + esc(f.hint || '') + '">' +
        esc(v) + '</textarea></label>';
    }
    if (f.type === 'photo') {
      return '<div class="fld col"><span>' + esc(f.label) + '</span>' +
        '<div class="photo-box" data-photo-box="' + f.key + '"></div>' +
        '<label class="add-photo">＋ 添加照片' +
        '<input type="file" accept="image/*" multiple data-photo-input="' + f.key + '" hidden>' +
        '</label></div>';
    }
    return '<label class="fld"><span>' + esc(f.label) + req + '</span>' +
      '<input type="text" data-f="' + f.key + '" value="' + esc(v) + '" placeholder="' +
      esc(f.hint || '') + '"></label>';
  }

  function renderPhotoBox(key) {
    const box = $('[data-photo-box="' + key + '"]');
    if (!box) return;
    const list = state.photos[key] || [];
    box.innerHTML = list.map((p, i) =>
      '<span class="thumb"><img src="' + esc(p.url) + '" alt="照片" data-act="photo-view" data-url="' +
      esc(p.url) + '"><button type="button" class="thumb-del" data-act="photo-del" data-key="' +
      key + '" data-idx="' + i + '">×</button></span>').join('');
  }

  function renderForm(type, rec) {
    const schema = (state.meta.schemas || {})[type];
    if (!schema) return head('未知单据', '', true);
    const fs = (rec && rec.fields) || {};
    let html = head(schema.label, rec ? ('单据号 ' + (rec.doc_no || '')) : '新建单据', true);
    html += '<section class="panel form-panel">';
    if (rec && rec.status === 'audited') {
      html += '<div class="status-bar ok">✓ 已审核 · ' + esc(rec.audited_by || '') +
        ' · ' + esc(rec.audited_at || '') + '</div>';
    }
    schema.fields.forEach((f) => { html += fieldHtml(f, fs[f.key]); });
    html += '<div class="form-actions">' +
      '<button type="button" class="primary big" data-act="record-save">保存</button>' +
      '<button type="button" class="primary big secondary" data-act="record-audit">' +
      (rec && rec.status === 'audited' ? '撤销审核' : '审核') + '</button></div>';
    html += '<div class="stamp-row"><span>制单人：' + esc((rec && rec.created_by) || 'SYSTEM') +
      '</span><span>制单日期：' + esc(fmtStamp((rec && rec.created_at) || Date.now() / 1000)) +
      '</span><span>审核人：' + esc((rec && rec.audited_by) || '') +
      '</span><span>审核日期：' + esc((rec && rec.audited_at) || '') + '</span></div>';
    html += '</section>';
    return html;
  }

  function collectFields(schema) {
    const out = {};
    schema.fields.forEach((f) => {
      if (f.type === 'photo') { out[f.key] = (state.photos[f.key] || []).slice(); return; }
      const el = $('[data-f="' + f.key + '"]');
      if (!el) return;
      out[f.key] = f.type === 'switch' ? el.checked : el.value;
    });
    return out;
  }

  // ---------------------------------------------------------------- 序时簿
  function ledgerQuery() {
    const p = new URLSearchParams();
    if (state.ledgerType) p.set('type', state.ledgerType);
    const f = state.filters;
    if (f.from) p.set('from', f.from);
    if (f.to) p.set('to', f.to);
    if (f.machine) p.set('machine', f.machine);
    if (f.status) p.set('status', f.status);
    if (f.q) p.set('q', f.q);
    return p.toString();
  }

  function renderLedger(type) {
    const label = ((state.meta.schemas || {})[type] || {}).label || '全部单据';
    const f = state.filters;
    let html = head(label + ' · 序时簿', '共 ' + state.records.length + ' 条', true);
    html += '<section class="panel"><div class="filters">' +
      '<label>起<input type="date" id="flFrom" value="' + esc(f.from) + '"></label>' +
      '<label>止<input type="date" id="flTo" value="' + esc(f.to) + '"></label>' +
      '<label>机台<input type="text" id="flMachine" value="' + esc(f.machine) + '" placeholder="机台号"></label>' +
      '<label>状态<select id="flStatus">' +
        '<option value="">全部</option>' +
        '<option value="draft"' + (f.status === 'draft' ? ' selected' : '') + '>草稿</option>' +
        '<option value="audited"' + (f.status === 'audited' ? ' selected' : '') + '>已审核</option>' +
      '</select></label>' +
      '<label>关键字<input type="text" id="flQ" value="' + esc(f.q) + '" placeholder="搜索"></label>' +
      '<span class="filter-ops"><button type="button" class="mini-btn" data-act="filter-apply">查询</button>' +
      '<button type="button" class="mini-btn" data-act="filter-reset">重置</button>' +
      '<button type="button" class="mini-btn" data-act="ledger-export">导出 CSV</button></span>' +
      '</div></section>';
    html += '<div class="record-list">';
    if (!state.records.length) html += '<div class="empty">这个条件下还没有单据。</div>';
    state.records.forEach((r) => {
      const fs = r.fields || {};
      const summary = Object.keys(fs).filter((k) =>
        typeof fs[k] === 'string' && fs[k] && k !== 'doc_no' && k !== 'day').slice(0, 2)
        .map((k) => esc(fs[k])).join(' · ');
      html += '<div class="rec-card"><div class="rec-top">' +
        '<b>' + esc(r.type_label || '') + '</b>' +
        '<span class="badge ' + (r.status === 'audited' ? 'ok' : '') + '">' +
        (r.status === 'audited' ? '已审核' : '草稿') + '</span></div>' +
        '<div class="rec-meta">单据号 ' + esc(r.doc_no || '') + ' · ' + esc(r.day || '') +
        ' · 机台 ' + esc(r.machine_no || '—') + '</div>' +
        (summary ? '<div class="rec-sum">' + summary + '</div>' : '') +
        '<div class="rec-ops">' +
          '<button type="button" class="mini-btn" data-act="record-edit" data-id="' + esc(r.id) + '">查看 / 编辑</button>' +
          (r.status === 'audited' ? '' :
            '<button type="button" class="mini-btn" data-act="record-audit-one" data-id="' + esc(r.id) + '">审核</button>') +
          '<button type="button" class="mini-btn danger" data-act="record-del" data-id="' + esc(r.id) + '">删除</button>' +
        '</div></div>';
    });
    html += '</div>';
    return html;
  }

  // ---------------------------------------------------------------- 总览
  function renderOverview() {
    let html = head('分切机总览', '按机台汇总各类单据的最新状态', true);
    const rows = (state.overview && state.overview.machines) || [];
    if (!rows.length) return html + '<div class="empty">还没有单据，先去录入一条吧。</div>';
    rows.forEach((m) => {
      html += '<section class="panel ov-card"><div class="ov-head"><b>' + esc(m.machine_no) +
        '</b><span>共 ' + m.total + ' 条 · 已审核 ' + m.audited + ' 条</span></div><div class="ov-grid">';
      Object.keys(state.meta.schemas || {}).forEach((t) => {
        const cur = (m.types || {})[t];
        html += '<div class="ov-cell ' + (cur ? (cur.status === 'audited' ? 'ok' : 'draft') : 'none') + '">' +
          '<span>' + esc(state.meta.schemas[t].label) + '</span>' +
          '<small>' + (cur ? (cur.status === 'audited' ? '已审核' : '草稿') + ' · ' + esc(cur.day) : '未录入') +
          '</small></div>';
      });
      html += '</div></section>';
    });
    return html;
  }

  // ---------------------------------------------------------------- 导航历史
  // 手机用户习惯按系统返回键。旧版 go() 用 replaceState，不产生历史记录，
  // 在录入表单里一按返回就直接退出应用（且未保存内容全丢）。
  // 现在每次视图切换 pushState，返回键改为「逐级返回」；退到主页后
  // 2.5 秒内再按一次才明确退出到门户，避免误触退出。
  const NAV_TAG = 'cmDeviceNav';
  let navDepth = 0;
  let lastRootBack = 0;
  function navState(view, params, depth) {
    return { tag: NAV_TAG, view: view, params: params || {}, depth: depth };
  }

  // ---------------------------------------------------------------- 渲染 / 导航
  function render() {
    const app = $('#app');
    if (state.view === 'home') app.innerHTML = renderHome();
    else if (state.view === 'options') app.innerHTML = renderOptions(state.optionKind);
    else if (state.view === 'form') app.innerHTML = renderForm(state.formType, state.editing);
    else if (state.view === 'ledger') app.innerHTML = renderLedger(state.ledgerType);
    else if (state.view === 'overview') app.innerHTML = renderOverview();
    if (state.view === 'form') {
      const schema = state.meta.schemas[state.formType];
      schema.fields.filter((f) => f.type === 'photo').forEach((f) => renderPhotoBox(f.key));
    }
  }

  async function go(view, params, fromHistory = false) {
    const p = params || {};
    if (!fromHistory) {
      navDepth += 1;
      history.pushState(navState(view, p, navDepth), '', '#' + view);
    }
    Object.assign(state, p);
    state.view = view;
    if (view === 'ledger') {
      try {
        const r = await api('api/records?' + ledgerQuery());
        state.records = r.items || [];
      } catch (e) { toast('加载失败：' + (e.message || e)); }
    } else if (view === 'overview') {
      try {
        state.overview = await api('api/overview');
      } catch (e) { toast('加载失败：' + (e.message || e)); }
    }
    render();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function loadOptions() {
    try {
      const r = await api('api/options');
      state.options = r.items || {};
    } catch (e) { toast('字典加载失败：' + (e.message || e)); }
  }

  // ---------------------------------------------------------------- 照片
  function compress(file) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onerror = () => reject(new Error('读取照片失败'));
      fr.onload = () => {
        const img = new Image();
        img.onload = () => {
          const max = 1600;
          const scale = Math.min(1, max / Math.max(img.width, img.height));
          const cv = document.createElement('canvas');
          cv.width = Math.round(img.width * scale);
          cv.height = Math.round(img.height * scale);
          cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
          resolve(cv.toDataURL('image/jpeg', 0.82));
        };
        img.onerror = () => reject(new Error('照片解码失败'));
        img.src = fr.result;
      };
      fr.readAsDataURL(file);
    });
  }

  async function handlePhoto(file, key) {
    if (!file || state.busy) return;
    state.busy = true;
    toast('照片上传中…');
    try {
      const dataUrl = await compress(file);
      const r = await api('api/photos', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ b64: dataUrl.split(',')[1] }),
      });
      state.photos[key] = (state.photos[key] || []).concat([{ url: r.url, name: r.name }]);
      renderPhotoBox(key);
      toast('照片已添加');
    } catch (e) { toast('上传失败：' + (e.message || e)); }
    finally { state.busy = false; }
  }

  function openLightbox(url) {
    $('#lightboxImg').src = url;
    $('#photoLightbox').hidden = false;
  }
  function closeLightbox() {
    $('#photoLightbox').hidden = true;
    $('#lightboxImg').src = '';
  }

  // ---------------------------------------------------------------- 动作
  async function saveRecord(audit) {
    const schema = state.meta.schemas[state.formType];
    const payload = { type: state.formType, fields: collectFields(schema) };
    if (audit) payload.audit = !!(state.editing && state.editing.status === 'audited' ? false : true);
    try {
      let r;
      if (state.editing) {
        r = await api('api/records/' + encodeURIComponent(state.editing.id), {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      } else {
        r = await api('api/records', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      }
      state.editing = r.record;
      toast(audit ? (payload.audit ? '已保存并审核' : '已撤销审核') : '已保存');
      render();
      schema.fields.filter((f) => f.type === 'photo').forEach((f) => renderPhotoBox(f.key));
    } catch (e) { toast('保存失败：' + (e.message || e)); }
  }

  async function onClick(e) {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    const id = el.dataset.id;
    if (act === 'home') return go('home');
    if (act === 'option') return go('options', { optionKind: el.dataset.k });
    if (act === 'form') return go('form', { formType: el.dataset.k, editing: null, photos: {} });
    if (act === 'ledger') return go('ledger', { ledgerType: el.dataset.k });
    if (act === 'overview') return go('overview');
    if (act === 'back') return go('home');

    if (act === 'option-add') {
      const name = ($('#optName').value || '').trim();
      if (!name) return toast('请填写名称');
      try {
        await api('api/options', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: state.optionKind, name }),
        });
        await loadOptions(); render(); toast('已添加');
      } catch (err) { toast('添加失败：' + (err.message || err)); }
      return;
    }
    if (act === 'option-edit') {
      const cur = (state.options[state.optionKind] || []).find((x) => x.id === id);
      const name = window.prompt('修改名称', cur ? cur.name : '');
      if (!name || !name.trim()) return;
      try {
        await api('api/options/' + state.optionKind + '/' + encodeURIComponent(id), {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name.trim() }),
        });
        await loadOptions(); render(); toast('已修改');
      } catch (err) { toast('修改失败：' + (err.message || err)); }
      return;
    }
    if (act === 'option-del') {
      if (!window.confirm('确定删除这一项？')) return;
      try {
        await api('api/options/' + state.optionKind + '/' + encodeURIComponent(id), { method: 'DELETE' });
        await loadOptions(); render(); toast('已删除');
      } catch (err) { toast('删除失败：' + (err.message || err)); }
      return;
    }

    if (act === 'photo-del') {
      const key = el.dataset.key, idx = Number(el.dataset.idx);
      (state.photos[key] || []).splice(idx, 1);
      renderPhotoBox(key);
      return;
    }
    if (act === 'photo-view') { openLightbox(el.dataset.url); return; }

    if (act === 'record-save') return saveRecord(false);
    if (act === 'record-audit') return saveRecord(true);
    if (act === 'record-edit') {
      const r = state.records.find((x) => x.id === id);
      if (!r) return;
      const photos = {};
      Object.keys(r.fields || {}).forEach((k) => {
        if (Array.isArray(r.fields[k])) photos[k] = r.fields[k].slice();
      });
      return go('form', { formType: r.type, editing: r, photos });
    }
    if (act === 'record-audit-one') {
      try {
        await api('api/records/' + encodeURIComponent(id), {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ audit: true }),
        });
        toast('已审核'); return go('ledger');
      } catch (err) { toast('审核失败：' + (err.message || err)); }
      return;
    }
    if (act === 'record-del') {
      if (!window.confirm('确定删除这条单据？')) return;
      try {
        await api('api/records/' + encodeURIComponent(id), { method: 'DELETE' });
        toast('已删除'); return go('ledger');
      } catch (err) { toast('删除失败：' + (err.message || err)); }
      return;
    }
    if (act === 'filter-apply') {
      state.filters = {
        from: $('#flFrom').value || '', to: $('#flTo').value || '',
        machine: ($('#flMachine').value || '').trim(), status: $('#flStatus').value || '',
        q: ($('#flQ').value || '').trim(),
      };
      return go('ledger');
    }
    if (act === 'filter-reset') {
      state.filters = { from: '', to: '', machine: '', status: '', q: '' };
      return go('ledger');
    }
    if (act === 'ledger-export') {
      const a = document.createElement('a');
      a.href = 'api/export?' + ledgerQuery() + '&fmt=csv';
      a.download = 'device-' + (state.ledgerType || 'all') + '.csv';
      document.body.appendChild(a); a.click(); a.remove();
      return;
    }
  }

  async function onChange(e) {
    const input = e.target.closest('[data-photo-input]');
    if (!input) return;
    const key = input.dataset.photoInput;
    const files = Array.from(input.files || []).slice(0, 9);
    for (const f of files) await handlePhoto(f, key);
    input.value = '';
  }

  // ---------------------------------------------------------------- 登录
  function showLogin() { $('#loginModal').classList.add('show'); }
  async function doLogin() {
    $('#loginErr').textContent = '';
    try {
      await api('api/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: $('#lu').value.trim(), password: $('#lp').value }),
      });
      location.reload();
    } catch (e) { $('#loginErr').textContent = e.status === 401 ? '账号或密码不正确' : '网络错误'; }
  }

  function wire() {
    $('#app').addEventListener('click', onClick);
    $('#app').addEventListener('change', onChange);
    $('#lightboxClose').addEventListener('click', closeLightbox);
    $('#photoLightbox').addEventListener('click', (e) => {
      if (e.target === $('#photoLightbox')) closeLightbox();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeLightbox(); });
    $('#loginBtn').addEventListener('click', doLogin);
    $('#lp').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });

    // 系统返回键：优先在应用内逐级返回，而不是直接退出
    window.addEventListener('popstate', (e) => {
      const s = e.state;
      if (!s || s.tag !== NAV_TAG) return;      // 非本应用的历史条目，交给浏览器
      navDepth = s.depth || 0;
      go(s.view, s.params || {}, true);          // 恢复视图，不再压栈
      if (s.view === 'home' && navDepth === 0) {
        const now = Date.now();
        if (now - lastRootBack < 2500) {
          location.href = '../';                 // 连按两次 → 明确退出到门户
          return;
        }
        lastRootBack = now;
        history.pushState(navState('home', {}, 0), '', '#home');
        toast('已是主页 · 再按一次返回键退出');
      }
    });
  }

  async function init() {
    wire();
    try {
      state.meta = await api('api/meta');
    } catch (e) { toast('加载配置失败：' + (e.message || e)); return; }
    try {
      const s = await api('api/session');
      if (!s.ok) { showLogin(); return; }
    } catch (e) { showLogin(); return; }
    await loadOptions();
    const h = location.hash.replace('#', '');
    // 根哨兵：两条 home 记录，让「在主页按返回」先回到主页本身而不是直接离开
    history.replaceState(navState('home', {}, 0), '', '#home');
    history.pushState(navState('home', {}, 0), '', '#home');
    if (h === 'overview') return go('overview');
    render();
  }

  // 自动化自测钩子（只读，不改变页面行为）
  window.CM_NAV = { go: go, state: state, navState: navState, depth: () => navDepth };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
