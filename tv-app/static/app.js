/* 云电视前端 —— 无广告、无统计，播放与源管理全部走本地服务 */
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const LS = { vol: 'cloudtv.volume.v1', last: 'cloudtv.last.v1', group: 'cloudtv.group.v1', mode: 'cloudtv.mode.v1' };

  const state = {
    channels: [],
    full: {},
    group: '全部',
    q: '',
    current: null,
    detail: null,
    sourceIdx: 0,
    meta: {},
    pollTimer: null,
    // 播放通道：'direct' = 浏览器直连源站（不占服务器带宽），'proxy' = 走服务器代理
    mode: 'proxy',
    // 用户偏好（可手动切换）：'direct' = 优先直连、失败回退代理（默认），'proxy' = 一律代理
    pref: null,
  };

  const video = $('#video');
  let hls = null;

  const toastEl = $('#toast');
  let toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2600);
  }

  const readJSON = (k, d) => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } };
  const writeJSON = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} };

  async function api(path, opts) {
    const res = await fetch(path, { cache: 'no-store', ...opts });
    // 会话过期时被反代 302 到登录页，fetch 拿到 HTML：让整个窗口去登录
    if (res.redirected && !(res.headers.get('Content-Type') || '').includes('json')) {
      location.replace(res.url);
      throw new Error('unauthorized');
    }
    if (res.status === 401) {
      let target = '/login.html';
      try {
        const info = await res.json();
        if (info && info.login) target = info.login;
      } catch { /* 响应不是 JSON 时用默认值 */ }
      location.replace(target);
      throw new Error('unauthorized');
    }
    if (!res.ok) throw new Error(res.status);
    return res.json();
  }

  /* ---------------- 数据 ---------------- */
  async function loadMeta() {
    try {
      const meta = await api('api/meta');
      state.meta = meta;
      renderStatus();
      renderPortalLinks();
      if (meta.refreshing) pollMeta();
    } catch {
      $('#statusText').textContent = '无法连接本地服务';
      $('#statusDot').classList.add('off');
    }
  }

  // 「首页 / 状态」按钮：地址由后端按部署位置给出，独立运行时为空 → 隐藏
  function renderPortalLinks() {
    const p = state.meta.portal || {};
    [['homeBtn', p.home], ['statusBtn', p.status]].forEach(([id, href]) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (href) { el.href = href; el.hidden = false; }
      else { el.hidden = true; }
    });
  }

  function renderStatus() {
    const m = state.meta;
    const dot = $('#statusDot');
    dot.classList.toggle('busy', !!m.refreshing);
    dot.classList.remove('off');
    if (m.refreshing) {
      $('#statusText').textContent = `探活中：${m.progress || '…'}`;
      return;
    }
    if (m.total) {
      $('#statusText').textContent = `${m.online}/${m.total} 个频道可用`;
    } else {
      $('#statusText').textContent = '频道表为空';
    }
  }

  function pollMeta() {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(async () => {
      try {
        const meta = await api('api/meta');
        state.meta = meta;
        renderStatus();
        if (meta.refreshing) return pollMeta();
        await loadChannels();
        toast(`探活完成：${meta.online}/${meta.total} 可用`);
      } catch { /* ignore */ }
    }, 2500);
  }

  async function loadChannels() {
    try {
      const data = await api('api/channels');
      state.channels = data.items || [];
      renderGroups();
      renderList();
      await loadMeta();
      restoreLast();
    } catch {
      $('#statusText').textContent = '频道表加载失败';
      $('#statusDot').classList.add('off');
    }
  }

  function restoreLast() {
    if (state.current) return;
    const last = readJSON(LS.last, null);
    if (!last) return;
    const ch = state.channels.find((c) => c.id === last.id);
    if (ch) selectChannel(ch, false);
  }

  /* ---------------- 渲染 ---------------- */
  function renderGroups() {
    const groups = {};
    state.channels.forEach((c) => { groups[c.group || '其他'] = (groups[c.group || '其他'] || 0) + 1; });
    const box = $('#groups');
    box.innerHTML = '';
    [['全部', state.channels.length], ...Object.entries(groups)].forEach(([name, count]) => {
      const b = document.createElement('button');
      b.className = 'group-chip' + (state.group === name ? ' active' : '');
      b.textContent = `${name} ${count}`;
      b.onclick = () => { state.group = name; writeJSON(LS.group, name); renderGroups(); renderList(); };
      box.appendChild(b);
    });
  }

  function visibleChannels() {
    let items = state.channels.slice();
    if (state.group !== '全部') items = items.filter((c) => (c.group || '其他') === state.group);
    if (state.q) {
      const q = state.q.toLowerCase();
      items = items.filter((c) => c.name.toLowerCase().includes(q));
    }
    const nat = (v) => String(v).replace(/(\d+)/g, (m) => m.padStart(4, '0'));
    items.sort((a, b) => (Number(b.online) - Number(a.online)) || nat(a.name).localeCompare(nat(b.name)));
    return items;
  }

  function renderList() {
    const list = $('#channelList');
    list.innerHTML = '';
    const items = visibleChannels();
    if (!items.length) {
      list.innerHTML = '<div class="pane-note" style="padding:12px 8px">没有匹配的频道</div>';
      return;
    }
    items.forEach((c, i) => {
      const el = document.createElement('button');
      el.className = 'ch' + (state.current && state.current.id === c.id ? ' active' : '') + (c.online ? '' : ' offline');
      el.innerHTML = `
        <span class="ch-no">${String(i + 1).padStart(2, '0')}</span>
        <span class="ch-body">
          <span class="ch-name">${esc(c.name)}</span>
          <span class="ch-meta">${esc(c.group || '')} · ${c.online ? (c.latency ? c.latency + 'ms' : '在线') : (c.error || '离线')}</span>
        </span>
        <span class="ch-live"></span>`;
      el.onclick = () => selectChannel(c, true);
      list.appendChild(el);
    });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
  }

  function updateNow() {
    const c = state.current;
    if (!c) return;
    $('#nowTitle').textContent = c.name;
    const originLabel = { seed: '官方源', demo: '示例源', import: '导入源', custom: '自定义源' }[c.origin] || '自定义源';
    const bits = [c.group, originLabel];
    if (state.detail && state.detail.sources) bits.push(`线路 ${state.sourceIdx + 1}/${state.detail.sources.length}`);
    bits.push(state.mode === 'direct' ? '本地直连' : '服务器代理');
    $('#nowSub').textContent = bits.filter(Boolean).join(' · ');
    const link = $('#officialLink');
    if (c.official) { link.href = c.official; link.hidden = false; } else { link.hidden = true; }
    renderLines();
    document.title = `${c.name} · 云电视`;
  }

  function renderLines() {
    const box = $('#lines');
    box.innerHTML = '';
    const d = state.detail;
    if (!d || !d.sources) return;
    d.sources.forEach((s, i) => {
      const b = document.createElement('button');
      b.className = 'line-chip' + (i === state.sourceIdx ? ' active' : '') + (s.ok ? '' : ' bad');
      b.textContent = s.label || `线路 ${i + 1}`;
      b.title = s.url;
      b.onclick = () => { state.sourceIdx = i; startPlayback(); updateNow(); };
      box.appendChild(b);
    });
  }

  /* ---------------- 播放 ---------------- */
  async function selectChannel(ch, autoplay) {
    state.current = ch;
    state.sourceIdx = 0;
    writeJSON(LS.last, { id: ch.id });
    renderList();
    $('#stageEmpty').hidden = true;
    try {
      state.detail = await api(`api/channels/${ch.id}`);
    } catch {
      state.detail = null;
      toast('读取频道信息失败');
      return;
    }
    state.sourceIdx = 0;
    updateNow();
    if (autoplay) startPlayback();
    else { $('#bigPlay').hidden = false; $('#nowSub').textContent = (ch.group || '') + ' · 点击播放'; }
  }

  function teardown() {
    if (hls) { try { hls.destroy(); } catch {} hls = null; }
    video.pause();
    video.removeAttribute('src');
    video.load();
  }

  /* 该源能否让浏览器直连源站播放（不经过服务器）：
     - 服务器允许直连（config.direct_play）且用户没锁定代理模式
     - HTTPS 页面不能加载 http:// 源（混合内容会被浏览器拦截）
     - hls.js 基于 fetch，必须源站放行跨域（探活时已检测 src.cors）；
       原生 video 标签（mp4 直链 / Safari 原生 HLS）不要求跨域，可以直接连 */
  function canDirect(src, isHls, canHlsJs) {
    if (state.meta.direct_play === false) return false;
    if (state.pref === 'proxy') return false;
    if (location.protocol === 'https:' && /^http:\/\//i.test(src.url)) return false;
    if (isHls && canHlsJs) return !!src.cors;
    return true;
  }

  function startPlayback(forceProxy) {
    const d = state.detail;
    if (!d || !d.sources || !d.sources.length) {
      toast('该频道没有可用源');
      return;
    }
    const src = d.sources[state.sourceIdx];
    if (!src) return;
    teardown();
    $('#spinner').hidden = false;
    $('#bigPlay').hidden = true;

    const isHls = src.hls || /\.m3u8/i.test(src.url);
    const canHlsJs = !!(window.Hls && window.Hls.isSupported());
    state.mode = (!forceProxy && canDirect(src, isHls, canHlsJs)) ? 'direct' : 'proxy';
    renderMode();
    const url = state.mode === 'direct' ? src.url : `api/stream/${d.id}?i=${state.sourceIdx}`;

    if (isHls && canHlsJs) {
      hls = new Hls({
        enableWorker: true, lowLatencyMode: false, backBufferLength: 30, maxBufferLength: 30,
        abrEwmaDefaultEstimate: 400000, startLevel: -1,
        manifestLoadingTimeOut: 12000, manifestLoadingMaxRetry: 1, fragLoadingTimeOut: 25000,
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => { $('#spinner').hidden = true; }));
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (!data || !data.fatal) return;
        if (state.mode === 'direct') {
          // 直连失败（跨域 / 防盗链 / 混流），原源自动改走服务器代理重试
          toast('直连失败，已改走服务器代理');
          startPlayback(true);
        } else {
          nextSource('拉流失败');
        }
      });
    } else {
      video.src = url;
      video.play().catch(() => { $('#spinner').hidden = true; });
    }
  }

  // 播放通道按钮 + 标题栏提示：直连=流量在本地浏览器↔源站，代理=流量经过服务器
  function renderMode() {
    const btn = $('#modeBtn');
    if (btn) {
      const direct = state.mode === 'direct';
      btn.classList.toggle('direct', direct);
      btn.title = direct
        ? '当前：直连播放（流量不经过服务器，失败自动回退代理）'
        : '当前：服务器代理（流量经过服务器）';
    }
    if (state.current) updateNow();
  }

  function nextSource(reason) {
    const d = state.detail;
    if (!d) return;
    if (state.sourceIdx + 1 < d.sources.length) {
      state.sourceIdx += 1;
      toast(`${reason}，切换到线路 ${state.sourceIdx + 1}`);
      startPlayback();
      updateNow();
      return;
    }
    $('#spinner').hidden = true;
    toast('这个频道的所有线路都连不上');
    $('#nowSub').textContent = '所有线路不可用，可在源管理里替换地址';
    $('#bigPlay').hidden = false;
  }

  video.addEventListener('playing', () => {
    $('#spinner').hidden = true;
    $('#bigPlay').hidden = true;
    document.body.dataset.playing = 'true';
    if ('mediaSession' in navigator && state.current) {
      navigator.mediaSession.playbackState = 'playing';
    }
  });
  video.addEventListener('pause', () => { document.body.dataset.playing = 'false'; });
  video.addEventListener('waiting', () => { $('#spinner').hidden = false; });
  video.addEventListener('error', () => {
    if (!state.current) return;
    if (!video.src && !video.currentSrc) return; // 切台/销毁播放器产生的空 src 错误，忽略
    if (state.mode === 'direct') {
      // 直连模式下 video 标签加载失败（防盗链 / 协议受限等）：回退服务器代理
      toast('直连失败，已改走服务器代理');
      startPlayback(true);
    } else {
      nextSource('线路中断');
    }
  });

  /* ---------------- 控件 ---------------- */
  $('#playBtn').onclick = () => (video.paused ? video.play().catch(() => nextSource('播放失败')) : video.pause());
  $('#bigPlay').onclick = () => video.play().catch(() => nextSource('播放失败'));

  // 手动切换「直连 / 代理」偏好；直连模式下点一下锁定为代理，再点恢复自动
  $('#modeBtn').onclick = () => {
    state.pref = (state.pref === 'proxy') ? 'direct' : 'proxy';
    writeJSON(LS.mode, state.pref);
    toast(state.pref === 'proxy'
      ? '已锁定服务器代理（所有流量经过服务器）'
      : '已恢复优先直连（源站允许时本地直连，失败自动回退代理）');
    if (state.current) startPlayback();
  };

  const vol = $('#volume');
  const savedVol = Number(readJSON(LS.vol, 85));
  vol.value = Number.isFinite(savedVol) ? savedVol : 85;
  video.volume = vol.value / 100;
  vol.oninput = () => {
    video.volume = vol.value / 100;
    video.muted = false;
    writeJSON(LS.vol, vol.value);
    $('#vwave').style.opacity = video.volume === 0 ? '.25' : '1';
  };
  $('#muteBtn').onclick = () => {
    video.muted = !video.muted;
    $('#vwave').style.opacity = video.muted ? '.25' : '1';
    document.querySelector('#muteBtn svg').style.opacity = video.muted ? '.5' : '1';
  };
  $('#fullBtn').onclick = () => {
    const stage = $('#stage');
    if (document.fullscreenElement) document.exitFullscreen();
    else if (stage.requestFullscreen) stage.requestFullscreen();
  };
  $('#pipBtn').onclick = async () => {
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else if (video.requestPictureInPicture) await video.requestPictureInPicture();
      else toast('当前浏览器不支持画中画');
    } catch { toast('画中画启动失败'); }
  };

  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') return;
    if (e.code === 'Space') { e.preventDefault(); $('#playBtn').click(); }
    if (e.key === 'f' || e.key === 'F') $('#fullBtn').click();
    if (e.key === 'm' || e.key === 'M') $('#muteBtn').click();
    if (e.key === 'p' || e.key === 'P') $('#pipBtn').click();
    if (e.key === 'ArrowUp') { e.preventDefault(); vol.value = Math.min(100, Number(vol.value) + 5); vol.dispatchEvent(new Event('input')); }
    if (e.key === 'ArrowDown') { e.preventDefault(); vol.value = Math.max(0, Number(vol.value) - 5); vol.dispatchEvent(new Event('input')); }
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      const items = visibleChannels();
      if (!items.length) return;
      const idx = state.current ? items.findIndex((c) => c.id === state.current.id) : -1;
      const next = (idx + (e.key === 'ArrowRight' ? 1 : -1) + items.length) % items.length;
      selectChannel(items[next], true);
    }
  });

  $('#searchInput').oninput = (e) => { state.q = e.target.value.trim(); renderList(); };

  $('#refreshBtn').onclick = async () => {
    const btn = $('#refreshBtn');
    btn.disabled = true;
    const html = btn.innerHTML;
    btn.innerHTML = '<svg class="spin" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 11a8 8 0 1 0-2.6 6"/><path d="M20 4v7h-7"/></svg><span>探活中</span>';
    try {
      const r = await api('api/refresh', { method: 'POST' });
      toast(r.started ? '已开始探活，请稍候' : '探活已在进行中');
      pollMeta();
    } catch { toast('触发探活失败'); }
    finally { setTimeout(() => { btn.disabled = false; btn.innerHTML = html; }, 2000); }
  };

  /* ---------------- 源管理 ---------------- */
  const modal = $('#modal');

  $('#manageBtn').onclick = async () => {
    modal.hidden = false;
    switchTab('list');
    await loadAdminList();
  };
  $('#emptyManage').onclick = () => $('#manageBtn').click();
  $('#hintManage').onclick = () => $('#manageBtn').click();
  $('#closeModal').onclick = () => { modal.hidden = true; };
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.hidden = true; });

  document.querySelectorAll('.tab').forEach((t) => {
    t.onclick = () => switchTab(t.dataset.tab);
  });

  function switchTab(name) {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.pane').forEach((p) => { p.hidden = p.id !== `pane-${name}`; });
  }

  async function loadAdminList() {
    const box = $('#adminList');
    box.innerHTML = '<div class="pane-note">读取中…</div>';
    const details = await Promise.all(state.channels.map(async (c) => {
      try { return await api(`api/channels/${c.id}`); } catch { return c; }
    }));
    state.full = {};
    details.forEach((d) => { state.full[d.id] = d; });
    box.innerHTML = '';
    details.forEach((d) => {
      const row = document.createElement('div');
      row.className = 'admin-row' + (d.online ? '' : ' off');
      row.innerHTML = `
        <span class="nm">${esc(d.name)}<small>${esc(d.group || '')} · ${(d.sources || []).length} 个源 · ${d.online ? '在线' : esc(d.error || '离线')}</small></span>
        <span class="tag">${({seed:'预置',demo:'示例',import:'导入'})[d.origin] || '自定义'}</span>
        <button data-act="edit">编辑</button>
        <button data-act="del" class="danger">删除</button>`;
      row.querySelector('[data-act="edit"]').onclick = () => fillForm(d);
      row.querySelector('[data-act="del"]').onclick = async () => {
        if (!confirm(`删除频道「${d.name}」？`)) return;
        try {
          await api(`api/channels/${d.id}`, { method: 'DELETE' });
          toast('已删除');
          await loadChannels();
          await loadAdminList();
        } catch { toast('删除失败（可能是只读或需要 token）'); }
      };
      box.appendChild(row);
    });
  }

  function clearForm() {
    ['fId', 'fName', 'fGroup', 'fOfficial', 'fUrls'].forEach((id) => { document.getElementById(id).value = ''; });
    $('#testResult').textContent = '';
  }

  $('#newBtn').onclick = () => { clearForm(); switchTab('edit'); };

  function fillForm(d) {
    $('#fId').value = d.id || '';
    $('#fName').value = d.name || '';
    $('#fGroup').value = d.group || '';
    $('#fOfficial').value = d.official || '';
    $('#fUrls').value = (d.sources || []).map((s) => s.url).join('\n');
    $('#testResult').textContent = '';
    switchTab('edit');
  }

  $('#saveBtn').onclick = async () => {
    const urls = $('#fUrls').value.split('\n').map((s) => s.trim()).filter(Boolean);
    const body = {
      id: $('#fId').value || undefined,
      name: $('#fName').value.trim(),
      group: $('#fGroup').value.trim() || '自定义',
      official: $('#fOfficial').value.trim(),
      sources: urls.map((u) => ({ url: u, label: '自定义源' })),
    };
    if (!body.name || !urls.length) return toast('名称和至少一个地址是必填的');
    try {
      await api('api/channels', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      toast('已保存，正在探活');
      clearForm();
      modal.hidden = true;
      await loadChannels();
      pollMeta();
    } catch { toast('保存失败（可能需要 admin token）'); }
  };

  $('#testBtn').onclick = async () => {
    const url = $('#fUrls').value.split('\n').map((s) => s.trim()).filter(Boolean)[0];
    const out = $('#testResult');
    if (!url) return toast('先填一个地址');
    out.textContent = '测试中…';
    out.className = 'test-result';
    try {
      const r = await api('api/probe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
      out.textContent = r.ok ? `可以访问 · ${r.latency}ms · ${r.hls ? 'HLS' : r.type}` : `不可用：${r.note || r.type}`;
      out.className = 'test-result ' + (r.ok ? 'ok' : 'bad');
    } catch {
      out.textContent = '测试请求失败';
      out.className = 'test-result bad';
    }
  };

  $('#importBtn').onclick = async () => {
    const text = $('#fM3u').value.trim();
    if (!text) return toast('先粘贴 M3U 内容');
    const out = $('#importResult');
    out.textContent = '导入中…';
    out.className = 'test-result';
    try {
      const r = await api('api/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ m3u: text }) });
      out.textContent = `新增 ${r.added} 个频道（解析 ${r.parsed} 条）`;
      out.className = 'test-result ok';
      await loadChannels();
      pollMeta();
    } catch {
      out.textContent = '导入失败，请检查格式';
      out.className = 'test-result bad';
    }
  };

  /* ---------------- 启动 ---------------- */
  async function boot() {
    const g = readJSON(LS.group, '全部');
    if (g) state.group = g;
    state.pref = readJSON(LS.mode, 'direct');
    await loadChannels();
    if (state.channels.length && state.channels.every((c) => !c.online)) {
      toast('所有频道探活失败，请检查服务器网络或在源管理中替换地址');
    }
  }
  boot();
})();
