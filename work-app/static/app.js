/* 工作日志 —— 记录页 + 零依赖甘特图
 * 接口走相对路径 api/...（挂在 nginx /work/ 下也正常工作）
 * 统一登录：挂在门户子路径下由 CloudAuth 保证；独立运行时用页面内登录遮罩 */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  const STATUS_LABEL = {
    planned: '将要发生', doing: '正在发生', followup: '待跟踪结果', done: '已闭环',
  };
  const STATUS_KEYS = ['planned', 'doing', 'followup', 'done'];

  const state = {
    day: '', items: [], stats: {}, filter: 'all', view: 'record',
    editing: null, formStatus: 'planned',
    pendingPhotos: [], ganttMode: 'cards',
  };

  // ---------------------------------------------------------------- 纯函数（甘特图几何）
  function toMin(hhmm) {
    const p = String(hhmm || '00:00').split(':');
    return (parseInt(p[0], 10) || 0) * 60 + (parseInt(p[1], 10) || 0);
  }
  function fmtMin(m) {
    m = Math.max(0, Math.min(24 * 60, Math.round(m)));
    return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
  }
  function windowFor(items) {
    // 按当天事项范围自适应：最早开始往前 1 小时、最晚结束往后 1 小时，
    // 至少 4 小时、最多 0–24 点。旧版固定 06:00–22:00 会把 15 分钟的事压成 1.5% 宽。
    if (!items || !items.length) return { start: 6 * 60, end: 22 * 60, total: 16 * 60 };
    let lo = Infinity, hi = -Infinity;
    items.forEach((it) => {
      const s = toMin(it.start), e = Math.max(toMin(it.end), s + 15);
      lo = Math.min(lo, s); hi = Math.max(hi, e);
    });
    lo = Math.max(0, Math.floor((lo - 60) / 60) * 60);
    hi = Math.min(24 * 60, Math.ceil((hi + 60) / 60) * 60);
    if (hi - lo < 4 * 60) {
      const mid = (lo + hi) / 2;
      lo = Math.max(0, Math.floor((mid - 2 * 60) / 60) * 60);
      hi = Math.min(24 * 60, lo + 4 * 60);
      lo = Math.max(0, hi - 4 * 60);
    }
    return { start: lo, end: hi, total: Math.max(1, hi - lo) };
  }
  function barGeom(item, win) {
    const s = Math.max(toMin(item.start), win.start);
    const e = Math.min(toMin(item.end), win.end);
    return {
      left: ((s - win.start) / win.total) * 100,
      width: Math.max(((e - s) / win.total) * 100, 2.2),
    };
  }
  function nowPercent(win, d) {
    const m = d.getHours() * 60 + d.getMinutes();
    if (m < win.start || m > win.end) return null;
    return ((m - win.start) / win.total) * 100;
  }
  function todayStr() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
      '-' + String(d.getDate()).padStart(2, '0');
  }
  function shiftDay(day, n) {
    const p = String(day).split('-').map(Number);
    const d = new Date(p[0], p[1] - 1, p[2] + n);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
      '-' + String(d.getDate()).padStart(2, '0');
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }
  function fmtDuration(min) {
    const h = Math.floor((min || 0) / 60), m = (min || 0) % 60;
    return h ? h + ' 小时 ' + (m ? m + ' 分' : '') : (m || 0) + ' 分钟';
  }
  // 水印文字：拍摄时刻精确到秒 + 地点。纯函数，便于自动化测试。
  function watermarkLines(d, place) {
    const p = (n) => String(n).padStart(2, '0');
    const t = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    return ['时间：' + t, '地点：' + (String(place || '').trim() || '未获取')];
  }

  // ---------------------------------------------------------------- 请求
  async function api(path, opts = {}) {
    const res = await fetch(path, { cache: 'no-store', credentials: 'same-origin', ...opts });
    let body = null;
    try { body = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (res.status === 401) {
      if (body && body.portal_login && body.login) { location.href = body.login; }
      else showLogin();
      const e = new Error('请先登录'); e.status = 401; throw e;
    }
    if (!res.ok) {
      const e = new Error((body && (body.message || body.error)) || ('HTTP ' + res.status));
      e.status = res.status; throw e;
    }
    return body || {};
  }
  let toastTimer;
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2400);
  }

  // ---------------------------------------------------------------- 现场照片（相机 + 时间地点水印）
  const PLACE_KEY = 'cw_last_place';
  function savedPlace() {
    try { return localStorage.getItem(PLACE_KEY) || ''; } catch (_) { return ''; }
  }
  function rememberPlace(v) {
    try { if (v) localStorage.setItem(PLACE_KEY, v); } catch (_) { /* 隐私模式忽略 */ }
  }
  function setPhotoStatus(msg) {
    const el = $('#photoStatus');
    if (el) el.textContent = msg || '';
  }
  function renderPhotoPreview() {
    const box = $('#photoPreview');
    box.innerHTML = '';
    state.pendingPhotos.forEach((p, i) => {
      const d = document.createElement('div');
      d.className = 'photo-thumb';
      const img = document.createElement('img');
      img.src = p.url;
      img.alt = '现场照片 ' + (i + 1);
      img.title = (p.time || '') + ' · ' + (p.place || '');
      img.addEventListener('click', () => openLightbox(p.url));
      const rm = document.createElement('button');
      rm.type = 'button'; rm.className = 'rm'; rm.textContent = '×';
      rm.title = '移除这张照片';
      rm.addEventListener('click', () => {
        state.pendingPhotos.splice(i, 1);
        renderPhotoPreview();
      });
      d.appendChild(img); d.appendChild(rm);
      box.appendChild(d);
    });
  }
  function openLightbox(url) {
    $('#lightboxImg').src = url;
    $('#photoLightbox').hidden = false;
  }
  function closeLightbox() {
    $('#photoLightbox').hidden = true;
    $('#lightboxImg').src = '';
  }
  function geoPlace() {
    // 定位失败（非 HTTPS / 用户拒绝 / 超时）时回落空串，让水印标注「未获取」并可手填。
    return new Promise((resolve) => {
      if (!navigator.geolocation) return resolve('');
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v || ''); } };
      const timer = setTimeout(() => finish(''), 6000);
      try {
        navigator.geolocation.getCurrentPosition(
          (pos) => {
            clearTimeout(timer);
            const c = pos.coords;
            finish('GPS ' + c.latitude.toFixed(5) + ', ' + c.longitude.toFixed(5));
          },
          () => { clearTimeout(timer); finish(''); },
          { enableHighAccuracy: true, timeout: 5500, maximumAge: 60000 });
      } catch (_) { clearTimeout(timer); finish(''); }
    });
  }
  function readImage(file) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error('图片读取失败'));
        img.src = fr.result;
      };
      fr.onerror = () => reject(new Error('文件读取失败'));
      fr.readAsDataURL(file);
    });
  }
  function drawWatermarked(img, lines) {
    const MAX = 1600;                        // 先缩放，控制上传体积与内存
    const scale = Math.min(1, MAX / Math.max(img.naturalWidth, img.naturalHeight));
    const w = Math.max(1, Math.round(img.naturalWidth * scale));
    const h = Math.max(1, Math.round(img.naturalHeight * scale));
    const cv = document.createElement('canvas');
    cv.width = w; cv.height = h;
    const ctx = cv.getContext('2d');
    ctx.drawImage(img, 0, 0, w, h);

    const fontOf = (s) => '600 ' + s + 'px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
    let fs = Math.max(15, Math.round(w * 0.024));
    ctx.font = fontOf(fs);
    const widest = () => Math.max.apply(null, lines.map((l) => ctx.measureText(l).width));
    while (fs > 12 && widest() > w - fs * 1.6) {
      fs -= 1;
      ctx.font = fontOf(fs);
    }
    const pad = Math.round(fs * 0.8);
    const lineH = Math.round(fs * 1.35);
    const boxH = lineH * lines.length + pad * 2;
    const grad = ctx.createLinearGradient(0, h - boxH - fs, 0, h);
    grad.addColorStop(0, 'rgba(0,0,0,0)');
    grad.addColorStop(1, 'rgba(0,0,0,.72)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, h - boxH - fs, w, boxH + fs);
    ctx.fillStyle = '#fff';
    ctx.font = fontOf(fs);
    ctx.textBaseline = 'bottom';
    ctx.shadowColor = 'rgba(0,0,0,.55)';
    ctx.shadowBlur = fs * 0.25;
    lines.forEach((line, i) => {
      ctx.fillText(line, pad, h - pad - (lines.length - 1 - i) * lineH);
    });
    return cv.toDataURL('image/jpeg', 0.86);
  }
  async function handlePhoto(file) {
    if (!file) return;
    if (!/^image\//.test(file.type || '')) { toast('请选择图片文件'); return; }
    if (file.size > 25 * 1024 * 1024) { toast('原图过大，请换一张'); return; }
    if (state.pendingPhotos.length >= 9) { toast('最多 9 张照片'); return; }
    setPhotoStatus('正在生成水印并上传…');
    try {
      const img = await readImage(file);
      const typed = ($('#fPlace').value || '').trim();
      // 优先手填 → 自动定位 → 上次用过的地点（http 页面常拿不到定位）
      let place = typed || await geoPlace();
      if (!typed && !place) place = savedPlace();
      if (!typed && place) $('#fPlace').value = place;
      rememberPlace(($('#fPlace').value || '').trim());
      if (!place) toast('未能自动定位，可在「地点」里手动填写');
      const lines = watermarkLines(new Date(), place);
      const time = lines[0].replace('时间：', '');
      const loc = lines[1].replace('地点：', '');
      const dataUrl = drawWatermarked(img, lines);
      const b64 = String(dataUrl).split(',')[1] || '';
      const r = await api('api/photos', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ b64, time, place: loc }),
      });
      if (!r || !r.url) throw new Error('上传未返回图片地址');
      state.pendingPhotos.push({ url: r.url, name: r.name || '', time, place: loc });
      renderPhotoPreview();
      setPhotoStatus('已添加：' + time + ' · ' + loc);
    } catch (e) {
      setPhotoStatus('照片保存失败：' + (e.message || e));
      toast('照片保存失败：' + (e.message || e));
    }
  }
  function locate() {
    setPhotoStatus('正在定位…');
    geoPlace().then((p) => {
      if (!p) {
        setPhotoStatus('定位不可用（需 HTTPS 或授权），请手动填写地点');
        toast('定位不可用，请手动填写地点');
        return;
      }
      $('#fPlace').value = p;
      rememberPlace(p);
      setPhotoStatus('已定位：' + p + '（可手动改成具体地点）');
    });
  }

  // ---------------------------------------------------------------- 渲染：统计
  function renderStats(stats) {
    state.stats = stats || {};
    const s = state.stats;
    $('#statsBar').innerHTML =
      '<span class="stat">共 <b>' + (s.total || 0) + '</b> 件</span>' +
      '<span class="stat">时长 <b>' + esc(fmtDuration(s.minutes)) + '</b></span>' +
      STATUS_KEYS.map((k) =>
        '<span class="stat">' + STATUS_LABEL[k] + ' <b>' + (s[k] || 0) + '</b></span>').join('');
    $('#ganttSummary').innerHTML =
      '<span class="stat">' + esc(state.day) + '</span>' +
      STATUS_KEYS.map((k) =>
        '<span class="stat">' + STATUS_LABEL[k] + ' <b>' + (s[k] || 0) + '</b></span>').join('') +
      '<span class="stat">合计 <b>' + esc(fmtDuration(s.minutes)) + '</b></span>';
  }

  // ---------------------------------------------------------------- 渲染：事项列表
  function renderList() {
    const box = $('#itemList');
    const list = state.items.filter((it) => state.filter === 'all' || it.status === state.filter);
    box.innerHTML = '';
    if (!list.length) {
      box.innerHTML = '<div class="empty">' +
        (state.items.length ? '这个分类下还没有事项。' : '这一天还没有记录，用左边的表单添加第一件事吧。') +
        '</div>';
      return;
    }
    list.forEach((it) => {
      const el = document.createElement('div');
      el.className = 'item';
      el.dataset.status = it.status;
      const dur = Math.max(0, toMin(it.end) - toMin(it.start));
      const photos = Array.isArray(it.photos) ? it.photos : [];
      let html =
        '<div class="i-head">' +
          '<span class="i-time">' + esc(it.start) + '–' + esc(it.end) + '</span>' +
          '<span class="i-dur">' + esc(fmtDuration(dur)) + '</span>' +
          '<span class="badge" data-s="' + it.status + '">' + STATUS_LABEL[it.status] + '</span>' +
        '</div>' +
        '<div class="ttl">' + esc(it.title) + '</div>';
      if (it.note) html += '<div class="desc">' + esc(it.note) + '</div>';
      if (it.result) html += '<div class="result-box"><b>结果</b>' + esc(it.result) + '</div>';
      if (it.status === 'followup' && !it.result) {
        html += '<div class="need-result">⚠ 尚待填写结果</div>';
      }
      if (photos.length) {
        html += '<div class="item-photos">' + photos.map((p) =>
          '<img src="' + esc(p.url) + '" alt="现场照片" loading="lazy" data-photo="' +
          esc(p.url) + '">').join('') + '</div>';
      }
      html +=
        '<div class="ops">' +
          '<select data-act="status" title="切换状态">' +
            STATUS_KEYS.map((k) => '<option value="' + k + '"' +
              (k === it.status ? ' selected' : '') + '>' + STATUS_LABEL[k] + '</option>').join('') +
          '</select>' +
          '<div class="op-btns">' +
            '<button class="icon-btn" data-act="edit" title="编辑">✎</button>' +
            '<button class="icon-btn danger" data-act="del" title="删除">✕</button>' +
          '</div>' +
        '</div>';
      el.innerHTML = html;
      el.querySelector('[data-act="status"]').addEventListener('change', (e) => changeStatus(it, e.target.value));
      el.querySelector('[data-act="edit"]').addEventListener('click', () => editItem(it, false));
      el.querySelector('[data-act="del"]').addEventListener('click', () => deleteItem(it));
      $$('[data-photo]', el).forEach((img) =>
        img.addEventListener('click', () => openLightbox(img.dataset.photo)));
      box.appendChild(el);
    });
  }

  // ---------------------------------------------------------------- 渲染：甘特图
  function renderGantt() {
    const items = state.items.slice();
    const win = windowFor(items);
    const axis = $('#ganttAxis');
    const rows = $('#ganttRows');
    axis.innerHTML = '';
    rows.innerHTML = '';

    const hours = [];
    for (let m = Math.ceil(win.start / 60) * 60; m <= win.end; m += 60) hours.push(m);
    hours.forEach((m) => {
      const tick = document.createElement('div');
      tick.className = 'tick';
      tick.style.left = ((m - win.start) / win.total * 100) + '%';
      if ((m / 60) % 2 === 0 || hours.length <= 10) {
        const span = document.createElement('span');
        span.textContent = fmtMin(m);
        tick.appendChild(span);
      }
      axis.appendChild(tick);
    });

    const np = state.day === todayStr() ? nowPercent(win, new Date()) : null;
    if (np !== null) {
      const now = document.createElement('div');
      now.className = 'now-line';
      now.style.left = np + '%';
      const tag = document.createElement('span');
      tag.textContent = '现在 ' + fmtMin(new Date().getHours() * 60 + new Date().getMinutes());
      now.appendChild(tag);
      axis.appendChild(now);
    }

    $('#ganttEmpty').hidden = items.length > 0;
    $('#ganttDayLabel').textContent = state.day;

    items.forEach((it) => {
      const row = document.createElement('div');
      row.className = 'gantt-row';
      const label = document.createElement('div');
      label.className = 'gantt-row-label';
      const b = document.createElement('b');
      b.textContent = it.title;
      const small = document.createElement('small');
      small.textContent = STATUS_LABEL[it.status] + ' · ' + it.start + '–' + it.end;
      label.appendChild(b); label.appendChild(small);
      row.appendChild(label);

      const track = document.createElement('div');
      track.className = 'gantt-track';
      hours.forEach((m) => {
        const gl = document.createElement('div');
        gl.className = 'grid-line';
        gl.style.left = ((m - win.start) / win.total * 100) + '%';
        track.appendChild(gl);
      });
      if (np !== null) {
        const nl = document.createElement('div');
        nl.className = 'now-line';
        nl.style.left = np + '%';
        track.appendChild(nl);
      }
      const g = barGeom(it, win);
      const bar = document.createElement('div');
      bar.className = 'gantt-bar' + (it.status === 'followup' && !it.result ? ' need-result' : '');
      bar.dataset.status = it.status;
      bar.style.left = g.left + '%';
      bar.style.width = Math.min(g.width, 100 - g.left) + '%';
      bar.title = it.title + ' · ' + it.start + '–' + it.end + ' · ' + STATUS_LABEL[it.status] +
        (it.note ? '\n备注：' + it.note : '') + (it.result ? '\n结果：' + it.result : '');
      const txt = document.createElement('span');
      txt.textContent = it.title;
      const tm = document.createElement('span');
      tm.className = 'bar-time';
      tm.textContent = it.start + '–' + it.end;
      bar.appendChild(txt); bar.appendChild(tm);
      bar.addEventListener('click', () => editItem(it, true));
      track.appendChild(bar);
      row.appendChild(track);
      rows.appendChild(row);
    });
    renderGanttMobile(items, win);
  }

  // 手机端：每项一张卡片，用整行宽度画时间条，不用左右拖动也能看出起止与时长
  function renderGanttMobile(items, win) {
    const box = $('#ganttMobile');
    box.innerHTML = '';
    if (!items.length) {
      box.innerHTML = '<div class="empty">这一天还没有记录。</div>';
      return;
    }
    const axis = [];
    for (let i = 0; i <= 4; i++) {
      axis.push(fmtMin(Math.round((win.start + win.total * i / 4) / 60) * 60));
    }
    items.forEach((it) => {
      const g = barGeom(it, win);
      const dur = Math.max(0, toMin(it.end) - toMin(it.start));
      const left = Math.min(g.left, 88);
      const width = Math.min(Math.max(g.width, 11), 100 - left);
      // 完整时间放到色块下方的独立标签里，绝不被 overflow:hidden 裁切
      const center = Math.min(84, Math.max(16, left + width / 2));
      const card = document.createElement('div');
      card.className = 'gm-card';
      card.dataset.status = it.status;
      const meta = [it.note, it.result && ('结果：' + it.result)].filter(Boolean).join(' / ');
      card.innerHTML =
        '<div class="gm-top">' +
          '<div class="gm-title">' + esc(it.title) + '</div>' +
          '<div class="gm-time">' + STATUS_LABEL[it.status] + ' · ' +
            esc(it.start) + '–' + esc(it.end) + ' · ' + esc(fmtDuration(dur)) + '</div>' +
        '</div>' +
        '<div class="gm-track" data-track></div>' +
        '<div class="gm-range-wrap"><span class="gm-range" style="left:' +
          center.toFixed(1) + '%">' + esc(it.start + '–' + it.end) + '</span></div>' +
        '<div class="gm-axis">' +
          axis.map((t) => '<span>' + esc(t) + '</span>').join('') + '</div>' +
        (meta ? '<div class="gm-meta">' + esc(meta) + '</div>' : '');
      const bar = document.createElement('div');
      bar.className = 'gm-bar';
      bar.dataset.status = it.status;
      bar.style.left = left + '%';
      bar.style.width = width + '%';
      bar.title = it.title + ' · ' + it.start + '–' + it.end;
      bar.addEventListener('click', () => editItem(it, true));
      card.querySelector('[data-track]').appendChild(bar);
      box.appendChild(card);
    });
  }

  // ---------------------------------------------------------------- 视图 / 日期
  function setGanttMode(mode) {
    state.ganttMode = mode === 'table' ? 'table' : 'cards';
    document.body.classList.toggle('gm-table', state.ganttMode === 'table');
    $('#ganttModeCards').classList.toggle('on', state.ganttMode === 'cards');
    $('#ganttModeTable').classList.toggle('on', state.ganttMode === 'table');
  }
  function setView(v, push = true) {
    state.view = v;
    $('#recordView').hidden = v !== 'record';
    $('#ganttView').hidden = v !== 'gantt';
    // 双保险：inline display 不受样式表 display:grid 覆盖（.layout 曾导致记录页隐藏失效）
    $('#recordView').style.display = v === 'record' ? '' : 'none';
    $('#ganttView').style.display = v === 'gantt' ? '' : 'none';
    $('#viewToggle').textContent = v === 'record' ? '查看甘特图 →' : '← 返回记录';
    if (v === 'gantt') renderGantt();
    // 记录页可能已滚到很深，切换后若不定位会停在页面中间，看起来像「点了没跳过去」。
    // 渲染完成后自动回到顶部，整个甘特图从首屏开始显示（切回记录页同理）。
    var jump = function () {
      try { window.scrollTo({ top: 0, behavior: 'smooth' }); }
      catch (_) { window.scrollTo(0, 0); }
    };
    if (window.requestAnimationFrame) window.requestAnimationFrame(jump);
    else setTimeout(jump, 0);
    if (push) history.replaceState(null, '', '#' + v);
  }
  function setDay(day) {
    state.day = day;
    $('#dayInput').value = day;
    $('#fDay').value = day;
    $('#ganttDayLabel').textContent = day;
    loadAll();
  }

  // ---------------------------------------------------------------- 表单
  function setFormStatus(s) {
    state.formStatus = s;
    $$('#fStatus button').forEach((b) => b.classList.toggle('on', b.dataset.status === s));
  }
  function resetForm() {
    state.editing = null;
    $('#itemForm').reset();
    $('#fDay').value = state.day;
    $('#fStart').value = roundNow();
    $('#fEnd').value = '';
    setFormStatus('planned');
    $('#formTitle').textContent = '记录一件事';
    $('#fSubmit').textContent = '添加记录';
    $('#formReset').hidden = true;
    $('#fErr').textContent = '';
    state.pendingPhotos = [];
    $('#fPlace').value = savedPlace();       // 沿用上次填过的地点，http 下定位常不可用
    renderPhotoPreview();
    setPhotoStatus('');
  }
  function roundNow() {
    const d = new Date();
    const m = d.getHours() * 60 + Math.ceil(d.getMinutes() / 15) * 15;
    return fmtMin(m % (24 * 60));
  }
  function editItem(it, fromGantt) {
    state.editing = it.id;
    $('#fTitle').value = it.title || '';
    $('#fDay').value = it.day || state.day;
    $('#fStart').value = it.start || '';
    $('#fEnd').value = it.end || '';
    $('#fNote').value = it.note || '';
    $('#fResult').value = it.result || '';
    state.pendingPhotos = (Array.isArray(it.photos) ? it.photos : []).map((p) => Object.assign({}, p));
    $('#fPlace').value = (state.pendingPhotos[0] && state.pendingPhotos[0].place) || '';
    renderPhotoPreview();
    setPhotoStatus(state.pendingPhotos.length ? '已载入 ' + state.pendingPhotos.length + ' 张现场照片' : '');
    setFormStatus(it.status);
    $('#formTitle').textContent = '编辑事项';
    $('#fSubmit').textContent = '保存修改';
    $('#formReset').hidden = false;
    $('#fErr').textContent = '';
    if (fromGantt) setView('record');
    $('#fTitle').focus();
    $('#fTitle').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  async function submitForm(e) {
    e.preventDefault();
    const payload = {
      title: $('#fTitle').value.trim(),
      day: $('#fDay').value || state.day,
      start: $('#fStart').value,
      end: $('#fEnd').value,
      status: state.formStatus,
      note: $('#fNote').value,
      result: $('#fResult').value,
      photos: state.pendingPhotos,
    };
    $('#fErr').textContent = '';
    try {
      if (state.editing) {
        await api('api/items/' + encodeURIComponent(state.editing), {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast('已保存修改');
      } else {
        await api('api/items', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast('已添加记录');
      }
      const day = payload.day;
      resetForm();
      if (day !== state.day) setDay(day); else await loadAll();
    } catch (err) {
      $('#fErr').textContent = err.message || '保存失败';
    }
  }
  async function changeStatus(it, status) {
    try {
      await api('api/items/' + encodeURIComponent(it.id), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      });
      toast('已改为「' + STATUS_LABEL[status] + '」');
      await loadAll();
    } catch (err) { toast('更新失败：' + (err.message || err)); }
  }
  async function deleteItem(it) {
    if (!confirm('确定删除「' + it.title + '」吗？')) return;
    try {
      await api('api/items/' + encodeURIComponent(it.id), { method: 'DELETE' });
      toast('已删除');
      await loadAll();
    } catch (err) { toast('删除失败：' + (err.message || err)); }
  }
  function exportData(fmt) {
    const a = document.createElement('a');
    a.href = 'api/export?day=' + encodeURIComponent(state.day) + '&fmt=' + fmt;
    a.download = 'work-' + state.day + '.' + fmt;
    document.body.appendChild(a); a.click(); a.remove();
  }

  // ---------------------------------------------------------------- 登录
  function showLogin() {
    if (location.pathname.replace(/\/$/, '').indexOf('/work') !== -1) { /* 子路径下由 nginx 处理 */ }
    $('#loginModal').classList.add('show');
  }

  // ---------------------------------------------------------------- 加载
  async function loadAll() {
    try {
      const r = await api('api/items?day=' + encodeURIComponent(state.day));
      state.items = r.items || [];
      renderStats(r.stats || {});
      renderList();
      $('#ganttDayLabel').textContent = state.day;
      if (state.view === 'gantt') renderGantt();
    } catch (e) {
      if (e.status !== 401) toast('加载失败：' + (e.message || e));
    }
  }

  // ---------------------------------------------------------------- 事件
  function wire() {
    $('#itemForm').addEventListener('submit', submitForm);
    $('#formReset').addEventListener('click', resetForm);
    $$('#fStatus button').forEach((b) =>
      b.addEventListener('click', () => setFormStatus(b.dataset.status)));
    $$('#filters button').forEach((b) =>
      b.addEventListener('click', () => {
        state.filter = b.dataset.filter;
        $$('#filters button').forEach((x) => x.classList.toggle('on', x === b));
        renderList();
      }));

    $('#viewToggle').addEventListener('click', () =>
      setView(state.view === 'record' ? 'gantt' : 'record'));
    $('#emptyGoRecord').addEventListener('click', () => setView('record'));
    window.addEventListener('hashchange', () =>
      setView(location.hash === '#gantt' ? 'gantt' : 'record', false));

    $('#dayInput').addEventListener('change', () => setDay($('#dayInput').value || todayStr()));
    $('#prevDay').addEventListener('click', () => setDay(shiftDay(state.day, -1)));
    $('#nextDay').addEventListener('click', () => setDay(shiftDay(state.day, 1)));
    $('#todayBtn').addEventListener('click', () => setDay(todayStr()));
    $('#fDay').addEventListener('change', () => {
      // 表单里的日期只影响本条记录，不切换当前查看的日期
    });

    $('#exportCsv').addEventListener('click', () => exportData('csv'));
    $('#exportJson').addEventListener('click', () => exportData('json'));

    // 现场照片：相机 / 相册 / 定位 / 大图
    $('#photoBtn').addEventListener('click', () => $('#fPhotoCam').click());
    $('#albumBtn').addEventListener('click', () => $('#fPhotoLib').click());
    ['#fPhotoCam', '#fPhotoLib'].forEach((sel) => {
      $(sel).addEventListener('change', (e) => {
        const f = e.target.files && e.target.files[0];
        e.target.value = '';              // 允许重复选择同一张图
        handlePhoto(f);
      });
    });
    $('#locBtn').addEventListener('click', locate);
    $('#fPlace').addEventListener('change', () =>
      rememberPlace(($('#fPlace').value || '').trim()));
    $('#lightboxClose').addEventListener('click', closeLightbox);
    $('#photoLightbox').addEventListener('click', (e) => {
      if (e.target === $('#photoLightbox')) closeLightbox();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeLightbox();
    });

    // 手机端甘特图：时间条 / 可横向滚动的表格
    $('#ganttModeCards').addEventListener('click', () => setGanttMode('cards'));
    $('#ganttModeTable').addEventListener('click', () => setGanttMode('table'));

    const doLogin = async () => {
      $('#loginErr').textContent = '';
      try {
        const r = await api('api/login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: $('#lu').value.trim(), password: $('#lp').value }),
        });
        if (r.ok) location.reload();
      } catch (e) {
        $('#loginErr').textContent = e.status === 401 ? '用户名或密码不正确' : '网络错误';
      }
    };
    $('#loginBtn').addEventListener('click', doLogin);
    $('#lp').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });

    // 当前时间线每分钟更新（只重绘甘特区域，不打断输入）
    setInterval(() => { if (state.view === 'gantt') renderGantt(); }, 60000);
  }

  async function init() {
    wire();
    setGanttMode('cards');
    state.day = todayStr();
    $('#dayInput').value = state.day;
    resetForm();
    try {
      const s = await api('api/session');
      if (!s.ok) { showLogin(); return; }
    } catch (e) {
      if (e.status === 401) { showLogin(); return; }
    }
    await loadAll();
    setView(location.hash === '#gantt' ? 'gantt' : 'record', false);
  }

  // 纯函数 / 渲染函数导出：供自动化自测使用，不改变页面行为
  window.CW_PURE = {
    watermarkLines, windowFor, barGeom, fmtMin, toMin, fmtDuration,
    drawWatermarked, renderList, renderGanttMobile, setGanttMode, setView, state,
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
