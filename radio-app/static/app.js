/* 云收音机前端 —— 无广告、无追踪，全部逻辑本地运行 */
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const LS = {
    fav: 'cloudradio.favorites.v1',
    recent: 'cloudradio.recent.v1',
    vol: 'cloudradio.volume.v1',
    last: 'cloudradio.last.v1',
  };

  const CAT_COLORS = {
    新闻: ['#496a8a', '#35506b'],
    音乐: ['#b65c33', '#8a3f20'],
    交通: ['#4b7d5e', '#33604a'],
    财经: ['#94713c', '#6d512a'],
    曲艺: ['#8c4a52', '#67333a'],
    体育: ['#3f6f6b', '#2a514e'],
    民族: ['#7a5a8c', '#563c66'],
    少儿: ['#c07a3a', '#96582a'],
    综合: ['#6b6259', '#4a423b'],
  };

  const state = {
    stations: [],
    view: [],
    cat: '全部',
    region: '全部',
    q: '',
    sort: 'default',
    current: null,
    detail: null,
    streamIdx: 0,
    loading: false,
    favorites: new Set(readJSON(LS.fav, [])),
    recent: readJSON(LS.recent, []),
    sleepTimer: null,
    sleepEnd: 0,
    playStartedAt: 0,
    retryCount: 0,
    meta: {},
    metaTimer: null,
    epgDay: 'today',
    epgData: null,
    epgStation: null,
    epgCurrent: null,
  };

  // 复用 player.js 建立的共享音源（#cmAudio），跨应用切换时状态可接力
  const audio = document.getElementById('cmAudio') || new Audio();
  audio.preload = 'none';
  audio.crossOrigin = 'anonymous';
  let hls = null;

  /* ---------------- 工具 ---------------- */
  function readJSON(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch { return fallback; }
  }
  function writeJSON(key, val) {
    try { localStorage.setItem(key, JSON.stringify(val)); } catch { /* 忽略隐私模式 */ }
  }
  function hash(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
    return h;
  }
  function initials(name) {
    const clean = name.replace(/^(CNR[-\s]?\d+|CMG)\s*/i, '').trim();
    return (clean || name).slice(0, 1);
  }
  function avatarStyle(st) {
    const pair = CAT_COLORS[st.category] || CAT_COLORS['综合'];
    const deg = hash(st.name) % 360;
    return `background: linear-gradient(145deg, ${pair[0]}, ${pair[1]}), radial-gradient(circle at ${deg % 100}% 20%, rgba(255,255,255,.18), transparent 60%)`;
  }
  function fmtTime(iso) {
    if (!iso) return '未知';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '未知';
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 90) return '刚刚';
    if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  function fmtElapsed(sec) {
    const m = Math.floor(sec / 60), s = sec % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  let toastTimer = null;
  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
  }

  /* ---------------- 数据加载 ---------------- */
  async function api(path, opts) {
    const res = await fetch(path, { cache: 'no-store', ...opts });
    // 被反代 302 到登录页时，fetch 拿到的是 HTML 而不是 JSON：
    // 直接让整个窗口显示登录页，避免界面停在半加载状态
    if (res.redirected && !(res.headers.get('Content-Type') || '').includes('json')) {
      location.replace(res.url);
      throw new Error('unauthorized');
    }
    if (res.status === 401) {
      // 统一入口部署时后端会给出门户登录页地址（带 next），独立运行则回本地登录页
      let target = 'login';
      try {
        const info = await res.json();
        if (info && info.login) target = info.login;
      } catch { /* 响应不是 JSON 时用默认值 */ }
      location.replace(target);
      throw new Error('unauthorized');
    }
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  }

  async function loadMeta() {
    try {
      const meta = await api('api/meta');
      state.meta = meta;
      renderStatus();
      renderAuth();
      renderPortalLinks();
      if (typeof meta.listening === 'number') {
        const badge = $('#listenCount');
        if (badge) badge.textContent = meta.listening;
      }
      renderCatNav();
      renderRegions();
      renderMetaLine();
      if (meta.refreshing) scheduleMetaPoll();
    } catch (err) {
      console.error('[meta] 读取失败', err);
      $('#statusText').textContent = '无法连接本地服务';
      $('#statusDot').classList.add('off');
    }
  }

  async function loadChannels() {
    state.loading = true;
    $('#skeletons').hidden = false;
    try {
      const data = await api('api/channels?limit=500');
      state.stations = data.items || [];
      $('#skeletons').hidden = true;
      applyFilters();
      await loadMeta();
    } catch (err) {
      $('#skeletons').hidden = true;
      $('#statusText').textContent = '频道库加载失败';
      $('#statusDot').classList.add('off');
      $('#emptyState').hidden = false;
    } finally {
      state.loading = false;
    }
  }

  function scheduleMetaPoll() {
    clearTimeout(state.metaTimer);
    state.metaTimer = setTimeout(async () => {
      try {
        const meta = await api('api/meta');
        state.meta = meta;
        renderStatus();
        if (!meta.refreshing) {
          await loadChannels();
          if (meta.last_error) toast('抓取失败：' + meta.last_error);
          else toast('频道库已更新');
        } else {
          scheduleMetaPoll();
        }
      } catch { /* 忽略 */ }
    }, 2500);
  }

  /* ---------------- 状态与导航 ---------------- */
  function renderAuth() {
    const btn = $('#logoutBtn');
    if (!btn) return;
    // 由 nginx 做 basic auth 时，退出没有意义（浏览器会立刻重新认证），直接隐藏
    btn.hidden = !state.meta.auth || state.meta.auth_mode === 'proxy';
    btn.title = state.meta.auth ? `当前登录：${state.meta.user || ''}（点击退出）` : '';
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
    const meta = state.meta;
    const dot = $('#statusDot');
    const text = $('#statusText');
    dot.classList.toggle('busy', !!meta.refreshing);
    dot.classList.toggle('off', false);
    if (meta.refreshing) {
      text.textContent = meta.progress ? `正在更新：${meta.progress}` : '正在更新频道库…';
      return;
    }
    if (meta.stations || meta.total) {
      text.textContent = `${meta.total} 个可用频道 · ${fmtTime(meta.updated_at)}更新`;
    } else {
      text.textContent = '频道库待初始化';
    }
  }

  function renderMetaLine() {
    const meta = state.meta;
    const parts = [];
    if (meta.total) parts.push(`${meta.total} 个频道`);
    if (meta.updated_at) parts.push(fmtTime(meta.updated_at) + '更新');
    $('#metaLine').textContent = parts.length ? parts.join(' · ') : '频道库尚未建立';
  }

  function renderCatNav() {
    const counts = (state.meta.categories || {});
    const order = ['全部', '新闻', '音乐', '交通', '财经', '曲艺', '综合', '体育', '民族', '少儿'];
    const icons = { 全部: '◎', 新闻: '📰', 音乐: '♪', 交通: '🚗', 财经: '¥', 曲艺: '🎭', 综合: '◍', 体育: '⚽', 民族: '♫', 少儿: '🧸' };
    const nav = $('#catNav');
    nav.innerHTML = '';
    order.forEach((cat) => {
      if (cat !== '全部' && !counts[cat] && cat !== state.cat) return;
      const btn = document.createElement('button');
      btn.className = 'nav-item' + (state.cat === cat ? ' active' : '');
      btn.dataset.cat = cat;
      btn.innerHTML = `<span class="ico">${icons[cat] || '◍'}</span><span>${cat}</span><span class="n">${cat === '全部' ? state.stations.length : (counts[cat] || 0)}</span>`;
      btn.addEventListener('click', () => { state.cat = cat; renderCatNav(); applyFilters(); });
      nav.appendChild(btn);
    });
    // 收藏
    const fav = document.createElement('button');
    fav.className = 'nav-item' + (state.cat === '收藏' ? ' active' : '');
    fav.dataset.cat = '收藏';
    fav.innerHTML = `<span class="ico">★</span><span>我的收藏</span><span class="n">${state.favorites.size}</span>`;
    fav.addEventListener('click', () => { state.cat = '收藏'; renderCatNav(); applyFilters(); });
    nav.appendChild(fav);
  }

  function renderRegions() {
    const regions = state.meta.regions || {};
    const sel = $('#regionSelect');
    const current = state.region;
    sel.innerHTML = '<option value="全部">全部地区</option>';
    Object.keys(regions).sort((a, b) => regions[b] - regions[a]).forEach((r) => {
      const opt = document.createElement('option');
      opt.value = r; opt.textContent = `${r}（${regions[r]}）`;
      sel.appendChild(opt);
    });
    sel.value = current;
  }

  /* ---------------- 列表 ---------------- */
  function applyFilters() {
    const q = state.q.trim().toLowerCase();
    let items = state.stations.slice();
    if (state.cat === '收藏') {
      items = items.filter((s) => state.favorites.has(s.id));
    } else if (state.cat !== '全部') {
      items = items.filter((s) => s.category === state.cat);
    }
    if (state.region && state.region !== '全部') {
      items = items.filter((s) => s.region === state.region);
    }
    if (q) {
      items = items.filter((s) =>
        s.name.toLowerCase().includes(q) ||
        (s.tags || '').toLowerCase().includes(q) ||
        (s.region || '').toLowerCase().includes(q) ||
        (s.category || '').includes(q)
      );
    }
    if (state.sort === 'latency') {
      items.sort((a, b) => (a.latency || 9999) - (b.latency || 9999));
    }
    state.view = items;
    renderList();
    const label = state.cat === '全部' ? '全部电台' : (state.cat === '收藏' ? '我的收藏' : state.cat);
    $('#listTitle').textContent = state.region !== '全部' ? `${state.region} · ${label}` : label;
    $('#listCount').textContent = `${items.length} 个`;
  }

  function renderList() {
    const list = $('#channelList');
    const frag = document.createDocumentFragment();
    state.view.forEach((st, i) => {
      frag.appendChild(rowEl(st, i));
    });
    list.innerHTML = '';
    list.appendChild(frag);
    $('#emptyState').hidden = state.view.length !== 0;
    updateActiveRow();
  }

  function rowEl(st, index) {
    const row = document.createElement('div');
    row.className = 'ch-row';
    row.dataset.id = st.id;
    row.setAttribute('role', 'listitem');
    row.innerHTML = `
      <div class="ch-avatar" style="${avatarStyle(st)}">
        <span>${initials(st.name)}</span>
        <div class="bars"><i></i><i></i><i></i><i></i></div>
      </div>
      <div class="ch-main">
        <div class="ch-name">${escapeHtml(st.name)}</div>
        <div class="ch-meta">${escapeHtml(st.region)} · ${escapeHtml(st.category)}${st.bitrate ? ` · ${st.bitrate}kbps` : ''}${st.codec ? ` · ${escapeHtml(st.codec)}` : ''}</div>
      </div>
      <div class="ch-lat ${latClass(st.latency)}" title="${st.latency ? '最近一次探活 ' + st.latency + 'ms' : '直播流'}">${latLabel(st.latency)}</div>
      <div class="ch-actions">
        <button class="star ${state.favorites.has(st.id) ? 'on' : ''}" data-act="fav" aria-label="收藏" title="收藏">
          <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="m12 4.5 2.4 5 5.4.7-4 3.8 1 5.4-4.8-2.6-4.8 2.6 1-5.4-4-3.8 5.4-.7z"/></svg>
        </button>
        <button class="ch-play" data-act="play" aria-label="播放 ${escapeHtml(st.name)}">
          <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l11-6.5z"/></svg>
        </button>
      </div>`;
    row.addEventListener('click', (ev) => {
      const act = ev.target.closest('[data-act]');
      if (act && act.dataset.act === 'fav') {
        ev.stopPropagation();
        toggleFavorite(st.id);
        return;
      }
      if (state.current && state.current.id === st.id) {
        togglePlay();
        return;
      }
      playStation(st);
    });
    return row;
  }

  function latClass(ms) {
    if (!ms) return '';
    if (ms < 700) return 'fast';
    if (ms >= 2200) return 'slow';
    return '';
  }

  function latLabel(ms) {
    if (!ms) return '直播';
    if (ms < 700) return '流畅';
    if (ms < 2200) return '正常';
    return '较慢';
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function updateActiveRow() {
    document.querySelectorAll('.ch-row').forEach((row) => {
      const active = state.current && row.dataset.id === state.current.id;
      row.classList.toggle('active', !!active);
      row.classList.toggle('playing', !!active && audio.paused === false);
    });
  }

  /* ---------------- 收藏 / 最近播放 ---------------- */
  function toggleFavorite(id) {
    if (state.favorites.has(id)) state.favorites.delete(id);
    else state.favorites.add(id);
    writeJSON(LS.fav, [...state.favorites]);
    const st = state.stations.find((s) => s.id === id);
    toast(state.favorites.has(id) ? `已收藏「${st ? st.name : ''}」` : '已取消收藏');
    syncFavButton();
    renderCatNav();
    if (state.cat === '收藏') applyFilters();
    else {
      const row = document.querySelector(`.ch-row[data-id="${id}"] .star`);
      if (row) row.classList.toggle('on', state.favorites.has(id));
    }
  }

  function pushRecent(st) {
    state.recent = [{ id: st.id, name: st.name, category: st.category }, ...state.recent.filter((r) => r.id !== st.id)].slice(0, 8);
    writeJSON(LS.recent, state.recent);
    renderRecent();
  }

  function renderRecent() {
    const box = $('#recentList');
    if (!state.recent.length) {
      box.innerHTML = '<p class="hint">还没有播放记录</p>';
      return;
    }
    box.innerHTML = '';
    state.recent.forEach((r) => {
      const btn = document.createElement('button');
      btn.className = 'recent-item';
      btn.innerHTML = `<span class="dot-mini"></span><span>${escapeHtml(r.name)}</span>`;
      btn.addEventListener('click', async () => {
        const st = state.stations.find((s) => s.id === r.id);
        if (st) playStation(st);
        else {
          try {
            const detail = await api(`api/channels/${r.id}`);
            playStation(detail, true);
          } catch { toast('该频道已不在频道库中'); }
        }
      });
      box.appendChild(btn);
    });
  }

  /* ---------------- 播放核心 ---------------- */
  async function playStation(st, isDetail) {
    state.current = st;
    state.streamIdx = 0;
    state.retryCount = 0;
    state.playStartedAt = 0;
    writeJSON(LS.last, { id: st.id, name: st.name });
    pushRecent(st);
    renderRecent();
    updateNowPlaying(st);
    updateActiveRow();
    syncFavButton();

    let detail = isDetail ? st : null;
    if (!detail) {
      try {
        detail = await api(`api/channels/${st.id}`);
      } catch {
        toast('无法获取该频道的线路信息');
        return;
      }
    }
    if (!detail.streams || !detail.streams.length) {
      toast('该频道当前没有可用线路');
      return;
    }
    state.detail = detail;
    startStream();
    loadEpg(st, true);
  }

  function streamUrl(entry, idx) {
    return `api/stream/${state.current.id}?i=${idx}`;
  }

  function startStream() {
    const detail = state.detail;
    const idx = state.streamIdx;
    const entry = detail.streams[idx];
    if (!entry) {
      toast('所有线路均不可用，请稍后再试');
      setBuffering(false);
      return;
    }
    teardownHls();
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    setBuffering(true);

    const url = streamUrl(entry, idx);
    const useHls = entry.hls || /\.m3u8/i.test(entry.url);
    // 上报给跨应用常驻播放条：音源用绝对路径（/radio/api/stream/...），任何子路径都能重建
    if (window.CMPlayer && state.current) {
      const view = state.view && state.view.length ? state.view : state.stations;
      CMPlayer.report({
        type: 'radio', id: state.current.id,
        title: state.current.name || '',
        sub: [state.current.region, state.current.category].filter(Boolean).join(' · '),
        list: view.map((s) => ({ id: s.id, title: s.name || '', sub: [s.region, s.category].filter(Boolean).join(' · ') })),
        index: view.findIndex((s) => s.id === state.current.id),
        url: '/radio/api/stream/' + encodeURIComponent(state.current.id) + '?i=' + idx,
        streamIdx: idx, hls: !!useHls, playing: true
      });
    }
    const hlsJsReady = !!(window.Hls && window.Hls.isSupported());
    const nativeHls = audio.canPlayType('application/vnd.apple.mpegurl') !== '';

    if (useHls && (hlsJsReady || !nativeHls)) {
      hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 30,
        maxBufferLength: 24,
        manifestLoadingTimeOut: 12000,
        manifestLoadingMaxRetry: 1,
        fragLoadingTimeOut: 20000,
      });
      hls.loadSource(url);
      hls.attachMedia(audio);
      hls.on(Hls.Events.MANIFEST_PARSED, () => { audio.play().catch(() => setBuffering(false)); });
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (data && data.fatal) failover('拉流失败');
      });
    } else {
      audio.src = url;
      audio.play().catch(() => {
        setBuffering(false);
        toast('浏览器阻止了自动播放，请点击播放按钮');
      });
    }

    updateNowPlaying(state.current);
    if (window.navigator.mediaSession) {
      navigator.mediaSession.playbackState = 'playing';
    }
  }

  function teardownHls() {
    if (hls) {
      try { hls.destroy(); } catch { /* ignore */ }
      hls = null;
    }
  }

  function failover(reason) {
    const detail = state.detail;
    if (!detail) return;
    if (state.streamIdx + 1 < detail.streams.length) {
      state.streamIdx += 1;
      toast(`${reason}，切换到备用线路 ${state.streamIdx + 1}/${detail.streams.length}`);
      startStream();
      return;
    }
    setBuffering(false);
    toast('这个电台暂时连不上，换一个试试');
    document.getElementById('player').dataset.playing = 'false';
    updateNowPlaying(state.current, true);
  }

  function togglePlay() {
    if (!state.current) {
      if (state.view.length) playStation(state.view[0]);
      return;
    }
    if (!state.detail) {
      playStation(state.current);
      return;
    }
    if (audio.paused) {
      setBuffering(true);
      audio.play().catch(() => { failover('播放失败'); });
      if (hls && hls.media) { try { hls.startLoad(); } catch { /* ignore */ } }
    } else {
      audio.pause();
    }
  }

  function setBuffering(on) {
    $('#player').dataset.buffering = on ? 'true' : 'false';
  }

  function updateNowPlaying(st, failed) {
    if (!st) return;
    const detail = state.detail;
    const lines = detail && detail.streams ? detail.streams.length : 1;
    $('#nowTitle').textContent = st.name;
    $('#artInitial').textContent = initials(st.name);
    $('#art').style.background = avatarStyle(st);
    const narrow = window.innerWidth < 620;
    const metaBits = [st.region, st.category].filter(Boolean);
    if (failed) metaBits.push('连接失败');
    else if (detail) metaBits.push(`${st.bitrate && !narrow ? st.bitrate + 'kbps · ' : ''}线路 ${state.streamIdx + 1}/${lines}`);
    const live = state.epgCurrent;
    if (!failed && live && state.epgStation === st.id) {
      $('#nowSub').textContent = `正在播：${live.title}（${live.start}–${live.end}）`;
    } else {
      $('#nowSub').textContent = metaBits.join(' · ');
    }
    document.title = `${st.name} · 云收音机`;
    if (window.navigator.mediaSession && window.MediaMetadata) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: st.name,
        artist: `${st.region || ''} ${st.category || ''}`.trim() || '云收音机',
        album: '云收音机 · 直播',
        artwork: [{ src: 'icon.svg', sizes: '512x512', type: 'image/svg+xml' }],
      });
    }
  }

  /* ---------------- 收听上报 ---------------- */
  const reported = { stationId: null };
  let stopTimer = null;

  function reportPlaying(st) {
    if (!st || !state.meta.auth) return;
    clearTimeout(stopTimer);
    if (reported.stationId === st.id) return;
    reported.stationId = st.id;
    fetch('api/playing', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station_id: st.id }),
    }).then(syncListenBadge).catch(() => {});
  }

  function reportStop() {
    if (!reported.stationId) return;
    reported.stationId = null;
    fetch('api/playing/stop', { method: 'POST' }).then(syncListenBadge).catch(() => {});
  }

  function scheduleStop() {
    clearTimeout(stopTimer);
    stopTimer = setTimeout(reportStop, 1500);
  }

  window.addEventListener('pagehide', () => {
    if (reported.stationId) navigator.sendBeacon('api/playing/stop', new Blob([], { type: 'text/plain' }));
  });

  async function syncListenBadge() {
    try {
      const data = await api('api/listening');
      const badge = $('#listenCount');
      if (badge) badge.textContent = data.total ?? (data.items || []).length;
    } catch { /* 忽略 */ }
  }

  /* ---------------- 在线收听面板 ---------------- */
  const listenModal = $('#listenModal');
  let listenTimer = null;

  const dur = (sec) => {
    const s = Math.max(0, Math.floor(sec));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
             : `${m}:${String(ss).padStart(2, '0')}`;
  };

  async function refreshListening() {
    try {
      const data = await api('api/listening');
      $('#listenCount').textContent = data.total ?? 0;
      const box = $('#listenList');
      if (!data.items || !data.items.length) {
        box.innerHTML = '<p class="listen-empty">现在没有人在收听<br>选一个电台开始吧</p>';
        return;
      }
      box.innerHTML = data.items.map((it) => `
        <div class="listen-row${it.self ? ' me' : ''}">
          <div class="who">${escapeHtml(it.user || '未知')}${it.self ? '（我）' : ''}
            <small>${escapeHtml(it.ip || '')}</small>
          </div>
          <div class="station">${escapeHtml(it.station_name || '—')}
            <em>${escapeHtml(it.category || '')}${it.category ? ' · ' : ''}${escapeHtml(it.started_at_text || '')} 开始</em>
          </div>
          <div class="timer">${dur(it.seconds)}</div>
        </div>`).join('');
    } catch {
      $('#listenList').innerHTML = '<p class="listen-empty">读取失败，请稍后重试</p>';
    }
  }

  function openListening() {
    listenModal.hidden = false;
    refreshListening();
    clearInterval(listenTimer);
    listenTimer = setInterval(refreshListening, 10000);
  }

  function closeListening() {
    listenModal.hidden = true;
    clearInterval(listenTimer);
    listenTimer = null;
  }

  if ($('#listenBtn')) {
    $('#listenBtn').addEventListener('click', openListening);
    $('#listenClose').addEventListener('click', closeListening);
    listenModal.addEventListener('click', (e) => { if (e.target === listenModal) closeListening(); });
  }

  function syncFavButton() {
    const btn = $('#favBtn');
    const on = state.current && state.favorites.has(state.current.id);
    btn.style.color = on ? 'var(--accent-2)' : '';
    btn.querySelector('svg').style.fill = on ? 'currentColor' : 'none';
  }

  audio.addEventListener('playing', () => {
    setBuffering(false);
    reportPlaying(state.current);
    state.playStartedAt = state.playStartedAt || Date.now();
    $('#player').dataset.playing = 'true';
    updateActiveRow();
    if (navigator.mediaSession) navigator.mediaSession.playbackState = 'playing';
  });
  audio.addEventListener('waiting', () => setBuffering(true));
  audio.addEventListener('pause', () => {
    scheduleStop();
    $('#player').dataset.playing = 'false';
    updateActiveRow();
    if (navigator.mediaSession) navigator.mediaSession.playbackState = 'paused';
  });
  audio.addEventListener('error', () => {
    if (!state.current) return;
    if (state.retryCount < 1) {
      state.retryCount += 1;
      setTimeout(() => failover('线路中断'), 500);
    } else {
      failover('线路中断');
    }
  });

  /* ---------------- 上一个 / 下一个 ---------------- */
  function step(dir) {
    if (!state.view.length) return;
    let idx = 0;
    if (state.current) {
      const found = state.view.findIndex((s) => s.id === state.current.id);
      idx = found === -1 ? 0 : (found + dir + state.view.length) % state.view.length;
    }
    playStation(state.view[idx]);
  }

  /* ---------------- 定时关闭 ---------------- */
  function setSleep(minutes) {
    clearTimeout(state.sleepTimer);
    if (!minutes) {
      state.sleepEnd = 0;
      $('#sleepBtn').style.color = '';
      toast('已取消定时关闭');
      return;
    }
    state.sleepEnd = Date.now() + minutes * 60000;
    $('#sleepBtn').style.color = 'var(--accent-2)';
    state.sleepTimer = setTimeout(() => {
      audio.pause();
      state.sleepEnd = 0;
      $('#sleepBtn').style.color = '';
      toast('定时时间到，已停止播放');
    }, minutes * 60000);
    toast(`${minutes} 分钟后自动停止`);
  }

  setInterval(() => {
    if (!state.playStartedAt || audio.paused) return;
    $('#elapsed').textContent = fmtElapsed(Math.floor((Date.now() - state.playStartedAt) / 1000));
  }, 1000);

  /* ---------------- 事件绑定 ---------------- */
  $('#playBtn').addEventListener('click', togglePlay);
  $('#prevBtn').addEventListener('click', () => step(-1));
  $('#nextBtn').addEventListener('click', () => step(1));
  $('#favBtn').addEventListener('click', () => {
    if (!state.current) return toast('先选一个电台');
    toggleFavorite(state.current.id);
  });
  $('#lineBtn').addEventListener('click', () => {
    if (!state.detail || state.detail.streams.length < 2) return toast('该频道只有一条线路');
    state.streamIdx = (state.streamIdx + 1) % state.detail.streams.length;
    toast(`已切换到线路 ${state.streamIdx + 1}`);
    startStream();
  });

  const vol = $('#volume');
  const savedVol = Number(readJSON(LS.vol, 80));
  vol.value = Number.isFinite(savedVol) ? savedVol : 80;
  audio.volume = vol.value / 100;
  vol.addEventListener('input', () => {
    audio.volume = vol.value / 100;
    audio.muted = false;
    writeJSON(LS.vol, vol.value);
    updateMuteIcon();
  });
  $('#muteBtn').addEventListener('click', () => {
    audio.muted = !audio.muted;
    updateMuteIcon();
  });
  function updateMuteIcon() {
    const silent = audio.muted || audio.volume === 0;
    $('#wave1').style.opacity = silent ? '.2' : '1';
    $('#wave2').style.opacity = silent ? '.2' : '1';
    $('#muteBtn').style.color = silent ? '#8a7a68' : '';
  }
  updateMuteIcon();

  const sleepMenu = $('#sleepMenu');
  $('#sleepBtn').addEventListener('click', (e) => {
    e.stopPropagation();
    sleepMenu.hidden = !sleepMenu.hidden;
  });
  sleepMenu.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    setSleep(Number(btn.dataset.min));
    sleepMenu.hidden = true;
  });
  document.addEventListener('click', () => { sleepMenu.hidden = true; });

  const searchInput = $('#searchInput');
  let searchTimer = null;
  searchInput.addEventListener('input', () => {
    $('#clearSearch').hidden = !searchInput.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = searchInput.value; applyFilters(); }, 140);
  });
  $('#clearSearch').addEventListener('click', () => {
    searchInput.value = '';
    state.q = '';
    $('#clearSearch').hidden = true;
    applyFilters();
    searchInput.focus();
  });

  $('#regionSelect').addEventListener('change', (e) => {
    state.region = e.target.value;
    applyFilters();
  });

  document.querySelectorAll('.chip-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.sort = btn.dataset.sort;
      document.querySelectorAll('.chip-btn').forEach((b) => b.classList.toggle('active', b === btn));
      applyFilters();
    });
  });
  $('#sortDefault').classList.add('active');

  $('#resetFilters').addEventListener('click', () => {
    state.cat = '全部';
    state.region = '全部';
    state.q = '';
    searchInput.value = '';
    $('#clearSearch').hidden = true;
    $('#regionSelect').value = '全部';
    renderCatNav();
    applyFilters();
  });

  $('#refreshBtn').addEventListener('click', async () => {
    const btn = $('#refreshBtn');
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<svg class="spin" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 11a8 8 0 1 0-2.6 6"/><path d="M20 4v7h-7"/></svg><span>更新中…</span>';
    try {
      const res = await api('api/refresh', { method: 'POST' });
      toast(res.started ? '已开始抓取，预计 1–3 分钟' : '更新正在进行中');
      scheduleMetaPoll();
    } catch (err) {
      toast(err.message === '403' ? '服务端未授权该操作' : '触发更新失败：' + err.message);
    } finally {
      setTimeout(() => { btn.disabled = false; btn.innerHTML = original; }, 2200);
    }
  });

  const logoutBtn = $('#logoutBtn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      reported.stationId = null;
      const underPortal = !!(state.meta && state.meta.portal && state.meta.portal.home);
      try { await fetch('api/logout', { method: 'POST' }); } catch { /* 忽略 */ }
      if (underPortal) {
        // 统一入口：同时注销门户会话，否则会被登录页立即放行回来
        try { await fetch('/auth/logout', { method: 'POST' }); } catch { /* 忽略 */ }
        location.replace('/');
      } else {
        location.replace('login');
      }
    });
  }

  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    if (e.key === 'n' || e.key === 'N') step(1);
    if (e.key === 'p' || e.key === 'P') step(-1);
    if (e.key === 'ArrowUp') { e.preventDefault(); vol.value = Math.min(100, Number(vol.value) + 5); vol.dispatchEvent(new Event('input')); }
    if (e.key === 'ArrowDown') { e.preventDefault(); vol.value = Math.max(0, Number(vol.value) - 5); vol.dispatchEvent(new Event('input')); }
    if (e.key === '/') { e.preventDefault(); searchInput.focus(); }
  });

  if ('mediaSession' in navigator) {
    navigator.mediaSession.setActionHandler('play', togglePlay);
    navigator.mediaSession.setActionHandler('pause', () => audio.pause());
    navigator.mediaSession.setActionHandler('previoustrack', () => step(-1));
    navigator.mediaSession.setActionHandler('nexttrack', () => step(1));
  }

  /* ---------------- 节目表 ---------------- */
  const epgDrawer = $('#epgDrawer');
  const epgMask = $('#epgMask');
  const epgList = $('#epgList');

  async function loadEpg(st, silent) {
    if (!st) return;
    if (state.epgStation !== st.id) {
      state.epgStation = st.id;
      state.epgCurrent = null;
      state.epgData = null;
    }
    if (!silent) epgList.innerHTML = '<p class="epg-empty">正在读取节目表…</p>';
    try {
      const data = await api(`api/epg/${st.id}?day=${state.epgDay}`);
      if (state.epgStation !== st.id) return;
      state.epgData = data;
      state.epgCurrent = data.ok ? data.current : null;
      renderEpg();
      if (state.current && state.current.id === st.id) updateNowPlaying(state.current);
    } catch (err) {
      if (state.epgStation !== st.id) return;
      state.epgData = null;
      state.epgCurrent = null;
      epgList.innerHTML = '<p class="epg-empty"><strong>节目表读取失败</strong>请稍后重试</p>';
    }
  }

  function renderEpg() {
    const data = state.epgData;
    const st = state.current;
    $('#epgStation').textContent = st ? st.name : '节目表';

    if (!data) {
      $('#epgMeta').textContent = '暂无数据';
      return;
    }
    if (!data.ok) {
      $('#epgMeta').textContent = '该电台暂无节目单';
      epgList.innerHTML = `<p class="epg-empty"><strong>这个电台还没有节目单</strong>${escHtml(data.hint || '')}<br>节目表来自公开索引，部分地方台没有收录。</p>`;
      return;
    }
    const sameName = data.matched_name && data.matched_name !== (st ? st.name : '');
    $('#epgMeta').textContent = `${sameName ? '索引名：' + data.matched_name + ' · ' : ''}数据来源 ${data.source}`;

    const dayLabels = { yesterday: '昨天', today: '今天', tomorrow: '明天' };
    document.querySelectorAll('#epgTabs button').forEach((b) => {
      b.classList.toggle('active', b.dataset.day === data.day);
      b.textContent = dayLabels[b.dataset.day] || b.dataset.day;
    });

    const html = data.programs.map((p, i) => {
      const isNow = p.is_now;
      const past = !isNow && data.day === 'today' && data.current_index >= 0 && i < data.current_index;
      const progress = isNow && typeof p.progress === 'number'
        ? `<div class="epg-progress"><i style="width:${Math.max(2, Math.min(100, Math.round(p.progress * 100)))}%"></i></div>`
        : '';
      return `<div class="epg-item${isNow ? ' now' : ''}${past ? ' past' : ''}" data-idx="${i}">
        <div class="epg-time"><b>${escHtml(p.start)}</b>${escHtml(p.end)}</div>
        <div>
          <div class="epg-title">${escHtml(p.title)}${isNow ? '<span class="epg-badge">正在播出</span>' : ''}</div>
          ${progress}
        </div>
      </div>`;
    }).join('');
    epgList.innerHTML = html || '<p class="epg-empty">这一天还没有节目安排</p>';

    const nowEl = epgList.querySelector('.epg-item.now');
    if (nowEl) {
      requestAnimationFrame(() => {
        const top = nowEl.offsetTop - epgList.clientHeight / 2 + nowEl.clientHeight / 2;
        epgList.scrollTop = Math.max(0, top);
      });
    }
  }

  function openEpg() {
    if (!state.current) return toast('先选一个电台');
    epgDrawer.hidden = false;
    epgMask.hidden = false;
    requestAnimationFrame(() => {
      epgDrawer.classList.add('open');
      epgMask.classList.add('show');
    });
    epgDrawer.setAttribute('aria-hidden', 'false');
    if (!state.epgData || state.epgStation !== state.current.id) loadEpg(state.current, false);
    else renderEpg();
  }

  function closeEpg() {
    epgDrawer.classList.remove('open');
    epgMask.classList.remove('show');
    epgDrawer.setAttribute('aria-hidden', 'true');
    setTimeout(() => {
      if (!epgDrawer.classList.contains('open')) {
        epgDrawer.hidden = true;
        epgMask.hidden = true;
      }
    }, 260);
  }

  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  $('#epgBtn').addEventListener('click', () => {
    if (epgDrawer.classList.contains('open')) closeEpg();
    else openEpg();
  });
  $('#epgClose').addEventListener('click', closeEpg);
  epgMask.addEventListener('click', closeEpg);
  document.querySelectorAll('#epgTabs button').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.epgDay = btn.dataset.day;
      document.querySelectorAll('#epgTabs button').forEach((b) => b.classList.toggle('active', b === btn));
      if (state.current) loadEpg(state.current, false);
    });
  });
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    if (e.key === 'Escape' && epgDrawer.classList.contains('open')) closeEpg();
    if (e.key === 'e' || e.key === 'E') {
      if (epgDrawer.classList.contains('open')) closeEpg();
      else openEpg();
    }
  });

  /* ---------------- 启动 ---------------- */
  async function boot() {
    // 先确认登录态：会话过期 / 服务端换了 data 目录时，避免带着失效 cookie 继续点
    try {
      await api('api/session');
    } catch {
      return;   // 401 时 api() 已跳转登录页
    }
    renderRecent();
    await loadChannels();
    const last = readJSON(LS.last, null);
    if (last) {
      const st = state.stations.find((s) => s.id === last.id);
      if (st) {
        state.current = st;
        updateNowPlaying(st);
        updateActiveRow();
        syncFavButton();
      }
    }
    if ('serviceWorker' in navigator && location.protocol.startsWith('http')) {
      navigator.serviceWorker.register('sw.js').catch(() => {});
    }
    // 接入跨应用常驻播放条：控制委托回本页原有逻辑（线路切换 / EPG / 收藏都保留）
    if (window.CMPlayer) {
      CMPlayer.attach({
        type: 'radio',
        onToggle: togglePlay,
        onNext: () => step(1),
        onPrev: () => step(-1),
        onRestore: (s) => {
          if (!s || s.type !== 'radio' || !s.id) return false;
          const target = state.stations.find((x) => x.id === s.id);
          if (!target) return false;
          playStation(target);          // 内部会重新拉线路并起播（线路索引自行重置为 0）
          return true;
        }
      });
      CMPlayer.resume();               // 频道数据就绪后再接力
    }
  }
  boot();
})();
