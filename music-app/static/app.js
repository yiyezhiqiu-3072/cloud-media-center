/* 云音乐 —— 前端播放器
   * 上传本地音乐（base64 + JSON），生成并管理播放列表，浏览器内播放
   * 接口走相对路径 api/...（挂在 nginx /music/ 下也正常工作）
   * 登录态：挂在门户子路径下由 CloudAuth 的 cm_session 保证；
             独立运行时用页面内登录遮罩（/api/login） */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const audio = $('#audio');

  const state = {
    tracks: [],
    queue: [],             // 待播放曲目 id
    qi: -1,                // 当前在 queue 中的索引
    order: [],             // 播放顺序（queue 下标序列）——随机时是整队洗牌结果
    orderPos: -1,          // 当前曲目在 order 中的位置
    repeat: 'off',         // off | all | one（三态轮转）
    shuffle: false,        // 洗牌队列轮询，而非每次纯随机跳
    currentId: null,
    seeking: false,
    lyrics: [],            // 解析后的 [{time, text}]
    lyricsRaw: '',         // LRC 原文（编辑框回填）
    lyricIndex: -1,
    quality: 0,            // 0 = 原码率；否则为目标 kbps
    meta: { transcode: false, bitrate_steps: [] },
    npOpen: false,
    queueOpen: false,
  };
  const STEPS_FALLBACK = [64, 96, 128, 192, 256, 320];
  let pendingSeek = null;  // 切换码率后需要恢复的播放位置

  // ---------------------------------------------------------------- 请求
  async function api(path, opts = {}) {
    const res = await fetch(path, { cache: 'no-store', credentials: 'same-origin', ...opts });
    if (res.status === 401) { showLogin(); const e = new Error('unauthorized'); e.status = 401; throw e; }
    let body = null;
    try { body = await res.json(); } catch {}
    if (!res.ok) { const e = new Error((body && body.error) || ('HTTP ' + res.status)); e.status = res.status; e.body = body; throw e; }
    return body;
  }

  // ---------------------------------------------------------------- 工具
  function fmtTime(s) {
    if (s == null || !isFinite(s) || s < 0) return '0:00';
    s = Math.floor(s);
    const m = Math.floor(s / 60), sec = s % 60;
    return m + ':' + String(sec).padStart(2, '0');
  }
  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }
  let toastTimer;
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2400);
  }
  const isAudioName = (name) => /\.(mp3|flac|wav|m4a|ogg|oga|aac|opus|webm|caf|aiff|mp4)$/i.test(name);

  // ---------------------------------------------------------------- 上传
  let currentXhr = null;        // 当前上传请求，供「取消」按钮 abort
  let uploadBusy = false;
  let uploadAbort = false;
  let uploadHideTimer = null;
  // 共享 switcher.js 读取此函数：上传期间切换应用时改为新标签页打开，保证上传不中断
  window.__cmUploadActive = () => uploadBusy;

  // 上传区折叠：默认收起，点「＋ 上传音乐」展开；上传开始自动收起，只留下方进度面板
  let uploadOpen = false;
  function setUploadOpen(open) {
    const dz = $('#dropzone'), btn = $('#uploadToggleBtn');
    uploadOpen = !!open && !uploadBusy;   // 上传中强制保持收起
    if (dz) dz.hidden = !uploadOpen;
    if (btn) {
      btn.setAttribute('aria-expanded', uploadOpen ? 'true' : 'false');
      btn.classList.toggle('on', uploadOpen);
      btn.textContent = uploadOpen ? '▴ 收起上传' : '＋ 上传音乐';
      btn.disabled = uploadBusy;          // 上传中禁用开关，避免误点展开打断「只显示进度」
    }
  }

  function fileToB64(file, onProgress) {
    return new Promise((res, rej) => {
      const r = new FileReader();
      r.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total); };
      r.onload = () => { const s = r.result; res(s.slice(s.indexOf(',') + 1)); };
      r.onerror = () => rej(new Error('文件读取失败'));
      r.readAsDataURL(file);
    });
  }
  function fileDuration(file) {
    return new Promise((res) => {
      const a = document.createElement('audio'); a.preload = 'metadata';
      const url = URL.createObjectURL(file); a.src = url;
      a.onloadedmetadata = () => { const d = isFinite(a.duration) ? Math.round(a.duration * 1000) / 1000 : null; URL.revokeObjectURL(url); res(d); };
      a.onerror = () => { URL.revokeObjectURL(url); res(null); };
    });
  }

  // fetch 拿不到上传进度，这里改用 XMLHttpRequest 的 upload.onprogress
  function postUpload(payload, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', 'api/upload', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.withCredentials = true;
      currentXhr = xhr;
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
      };
      xhr.onload = () => {
        currentXhr = null;
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch {}
        if (xhr.status === 401) {
          showLogin();
          const err = new Error('请先登录'); err.status = 401; reject(err); return;
        }
        if (xhr.status < 200 || xhr.status >= 300) {
          const err = new Error((body && (body.message || body.error)) || ('HTTP ' + xhr.status));
          err.status = xhr.status; reject(err); return;
        }
        resolve(body || {});
      };
      xhr.onerror = () => { currentXhr = null; reject(new Error('网络中断，上传未完成')); };
      xhr.onabort = () => { currentXhr = null; const err = new Error('已取消'); err.aborted = true; reject(err); };
      xhr.send(JSON.stringify(payload));
    });
  }

  function showUploadPanel(on) {
    clearTimeout(uploadHideTimer);
    const p = $('#uploadPanel');
    if (!p) return;
    p.hidden = !on;
    if (on) setUploadProgress('准备上传…', 0, '—');
  }
  function setUploadProgress(label, pct, detail) {
    const p = $('#uploadPanel');
    if (!p) return;
    const v = Math.max(0, Math.min(100, pct || 0));
    $('#upLabel').textContent = label;
    $('#upFill').style.width = v.toFixed(1) + '%';
    $('#upPct').textContent = Math.round(v) + '%';
    $('#upDetail').textContent = detail || '—';
  }
  function shortName(n) { return n.length > 34 ? n.slice(0, 31) + '…' : n; }

  async function uploadFiles(files) {
    if (uploadBusy) { toast('已有上传任务在进行中'); return; }
    const all = Array.from(files);
    const list = all.filter((f) => isAudioName(f.name) || /^audio\//.test(f.type));
    const skip = all.length - list.length;
    if (!list.length) { toast(skip ? '已跳过 ' + skip + ' 个非音频文件' : '没有选择文件'); return; }

    uploadBusy = true; uploadAbort = false;
    setUploadOpen(false);              // 上传一开始自动收起上传区，只留下方进度面板
    showUploadPanel(true);
    let done = 0, fail = 0, firstError = '';
    try {
      for (let i = 0; i < list.length; i++) {
        if (uploadAbort) break;
        const f = list[i];
        const tag = '(' + (i + 1) + '/' + list.length + ') ';
        const nm = shortName(f.name);
        const size = fmtSize(f.size);
        try {
          // 阶段 1：本地读取 + base64 编码，占整条进度 0~15%
          setUploadProgress(tag + '读取文件 · ' + nm, 0, '0 B / ' + size);
          const [b64, dur] = await Promise.all([
            fileToB64(f, (loaded, total) => {
              const ratio = total ? loaded / total : 0;
              const bytes = Math.min(f.size, Math.round(ratio * f.size));
              setUploadProgress(tag + '读取文件 · ' + nm, 15 * ratio, fmtSize(bytes) + ' / ' + size);
            }),
            fileDuration(f),
          ]);
          if (uploadAbort) break;
          // 阶段 2：网络上传，占 15~95%。XHR 的 total 是请求体字节数（base64 会膨胀约 4/3），
          // 这里换算回原始文件字节展示，避免「已传」看起来比文件本身还大
          setUploadProgress(tag + '正在上传 · ' + nm, 15, '0 B / ' + size);
          const r = await postUpload(
            { name: f.name, mime: f.type || '', b64, duration: dur },
            (loaded, total) => {
              const ratio = total ? loaded / total : 0;
              const bytes = Math.min(f.size, Math.round(ratio * f.size));
              setUploadProgress(tag + '正在上传 · ' + nm, 15 + 80 * ratio, fmtSize(bytes) + ' / ' + size);
            }
          );
          // 阶段 3：请求体已发完，等服务端写盘返回
          setUploadProgress(tag + '写入中 · ' + nm, 97, size + ' / ' + size);
          void r;
          done++;
          setUploadProgress(tag + '完成 · ' + nm, 100, size + ' / ' + size);
        } catch (e) {
          if (e.aborted || uploadAbort) break;
          if (e.status === 401) break;       // 弹登录框后中止
          fail++;
          if (!firstError) firstError = shortName(f.name) + '：' + (e.message || e);
          setUploadProgress(tag + '失败 · ' + nm, 0, (e.message || '上传失败'));
        }
      }
    } finally {
      currentXhr = null;
      uploadBusy = false;
      setUploadOpen(false);          // 上传结束：恢复开关可用，上传区保持收起
      uploadHideTimer = setTimeout(() => showUploadPanel(false), 900);
    }
    const parts = [];
    if (done) parts.push('成功 ' + done + ' 首');
    if (fail) parts.push('失败 ' + fail + ' 首');
    if (skip) parts.push('跳过 ' + skip + ' 个非音频');
    if (uploadAbort) parts.push('已取消');
    if (parts.length) toast(parts.join('，') + (firstError ? '（' + firstError + '）' : ''));
    await loadAll();
  }

  // ---------------------------------------------------------------- 渲染：音乐库
  function renderLibrary() {
    const list = $('#trackList');
    $('#libCount').textContent = state.tracks.length + ' 首';
    if (!state.tracks.length) {
      list.innerHTML = '<div class="pl-empty">还没有音乐，拖拽或选择文件上传吧。</div>';
      return;
    }
    list.innerHTML = '';
    state.tracks.forEach((t, i) => {
      const row = document.createElement('div');
      row.className = 'track' + (t.id === state.currentId ? ' playing' : '');
      row.dataset.id = t.id;
      const sub = [t.artist, t.bitrate ? Math.round(t.bitrate) + ' kbps' : '', fmtSize(t.size)]
        .filter(Boolean).join(' · ');
      row.innerHTML = `
        <div class="idx">${t.id === state.currentId ? eqBars() : (i + 1)}</div>
        <div class="meta">
          <div class="t-title">${escapeHtml(t.title || t.name)}</div>
          <div class="t-sub">${escapeHtml(sub)}</div>
        </div>
        <div class="t-dur">${fmtTime(t.duration)}</div>
        <div class="acts">
          <button class="icon-btn danger" data-act="del" title="删除">🗑</button>
        </div>`;
      row.addEventListener('click', (e) => {
        if (e.target.closest('.acts')) return;
        playTrackFromLibrary(t.id);     // 列表里点歌名只播放，不跳到播放页
      });
      row.querySelector('[data-act="del"]').addEventListener('click', (e) => {
        e.stopPropagation(); deleteTrack(t.id);
      });
      list.appendChild(row);
    });
  }

  function eqBars() {
    return '<span class="eq"><i></i><i></i><i></i></span>';
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  // ---------------------------------------------------------------- 队列轮询
  function trackUrl(id, q) {
    return 'api/track/' + encodeURIComponent(id) + (q ? '?q=' + q : '');
  }
  function shuffled(n, first) {
    const a = Array.from({ length: n }, (_, i) => i);
    for (let i = n - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    // 当前曲目固定在第一位，其余打乱：整队依次播放，不重复也不漏播
    const k = a.indexOf(first);
    if (k > 0) { a.splice(k, 1); a.unshift(first); }
    return a;
  }
  function rebuildOrder(startIndex) {
    const n = state.queue.length;
    if (!n) { state.order = []; state.orderPos = -1; return; }
    const s = Math.max(0, Math.min(startIndex, n - 1));
    if (state.shuffle && n > 1) {
      state.order = shuffled(n, s);
      state.orderPos = 0;
    } else {
      state.order = Array.from({ length: n }, (_, i) => i);
      state.orderPos = s;
    }
  }
  function playQueue(ids, index) {
    if (!ids.length) return;
    state.queue = ids.slice();
    rebuildOrder(index);
    loadByOrder();
  }
  function playTrackFromLibrary(id) {
    playQueue(state.tracks.map((t) => t.id), state.tracks.findIndex((t) => t.id === id));
  }
  function loadByOrder() {
    if (!state.order.length || state.orderPos < 0) return;
    state.qi = state.order[state.orderPos];
    const id = state.queue[state.qi];
    if (id) loadAndPlay(id);
  }
  // 服务端电台切歌 / 本页较晚才初始化时，只同步 UI 与歌词，绝不重设 audio.src
  // （音源与进度由共享播放条按服务端时间轴统一控制，避免两个地方抢着 seek）
  function syncRadioUI(id, index) {
    const t = state.tracks.find((x) => x.id === id);
    if (!t) return;
    state.currentId = id;
    // 让本页的上一首/下一首与电台队列对齐，用户手动切歌后可继续接着播
    state.queue = state.tracks.map((x) => x.id);
    state.order = state.queue.map((_x, i) => i);
    state.orderPos = (typeof index === 'number' && index >= 0)
      ? index : Math.max(0, state.queue.indexOf(id));
    updateNowPlaying(t);
    loadLyrics(id);
    renderLibrary();
    if (state.npOpen) { renderNowPlaying(); renderQueuePanel(); }
  }
  function wireRadio() {
    window.addEventListener('cm:radio', (e) => {
      const d = e.detail;
      if (!d || !d.track) return;
      syncRadioUI(String(d.track.id), d.index);
    });
  }
  function leaveRadio() {
    if (window.CMPlayer && CMPlayer.personal) CMPlayer.personal();
  }
  function inRadio() {
    return !!(window.CMPlayer && CMPlayer.radioMode && CMPlayer.radioMode());
  }
  function loadAndPlay(id) {
    const t = state.tracks.find((x) => x.id === id);
    if (!t) return;
    leaveRadio();                    // 用户主动点歌 → 临时脱离电台同步
    state.currentId = id;
    audio.src = trackUrl(id, state.quality);
    audio.play().catch(() => toast('播放失败：' + (t.title || t.name)));
    updateNowPlaying(t);
    loadLyrics(id);
    renderLibrary();
    if (state.npOpen) { renderNowPlaying(); renderQueuePanel(); }
    // 上报给跨应用常驻播放条：音源用绝对路径，任何子路径页面都能重建
    if (window.CMPlayer) CMPlayer.report({
      type: 'music', id: id,
      title: t.title || t.name || '', sub: t.artist || '',
      list: state.tracks.map((x) => ({ id: x.id, title: x.title || x.name || '', sub: x.artist || '' })),
      index: state.tracks.findIndex((x) => x.id === id),
      url: '/music/api/track/' + encodeURIComponent(id) + (state.quality ? '?q=' + encodeURIComponent(state.quality) : ''),
      q: state.quality || 0, playing: true
    });
  }
  function updateNowPlaying(t) {
    $('#npTitle').textContent = t.title || t.name;
    $('#npArtist').textContent = t.artist || '未知艺人';
  }
  function togglePlay() {
    leaveRadio();                    // 手动播放/暂停 → 个人模式
    if (!state.currentId) {
      if (state.tracks.length) playTrackFromLibrary(state.tracks[0].id);
      return;
    }
    if (audio.paused) audio.play().catch(() => {}); else audio.pause();
  }
  // manual=true 表示手动点「下一首」，此时忽略单曲循环
  function next(manual = false) {
    if (manual) leaveRadio();         // 手动切歌 → 个人模式
    if (!state.order.length) return;
    if (!manual && state.repeat === 'one') {
      audio.currentTime = 0; audio.play().catch(() => {}); return;
    }
    let pos = state.orderPos + 1;
    if (pos >= state.order.length) {
      // 一轮播完：列表循环 / 随机轮询重新洗一轮继续，顺序模式播完即停
      if (state.repeat === 'all' || state.shuffle) {
        const start = state.shuffle ? Math.floor(Math.random() * state.queue.length) : 0;
        rebuildOrder(start);
        pos = 0;
      } else { stopPlay(); return; }
    }
    state.orderPos = pos;
    loadByOrder();
  }
  function prev() {
    leaveRadio();                    // 手动切歌 → 个人模式
    if (!state.order.length) return;
    if (audio.currentTime > 3) { audio.currentTime = 0; return; }
    let pos = state.orderPos - 1;
    if (pos < 0) {
      if (state.repeat === 'all' || state.shuffle) pos = state.order.length - 1;
      else { audio.currentTime = 0; return; }
    }
    state.orderPos = pos;
    loadByOrder();
  }
  function stopPlay() {
    audio.pause(); audio.currentTime = 0; state.currentId = null;
    $('#npTitle').textContent = '未在播放';
    $('#npArtist').textContent = '选择一首歌曲开始';
    $('#player').dataset.playing = 'false';
    renderPlayIcons();
    renderLibrary();
    if (state.npOpen) renderNowPlaying();
  }
  function setRepeat() {
    state.repeat = state.repeat === 'off' ? 'all' : state.repeat === 'all' ? 'one' : 'off';
    renderMode(); renderQueuePanel();
  }
  function setShuffle() {
    state.shuffle = !state.shuffle;
    rebuildOrder(state.qi >= 0 ? state.qi : 0);   // 以当前曲目为起点重建，不打断播放
    renderMode(); renderQueuePanel();
    toast(state.shuffle ? '随机轮询：整队洗牌后依次播放' : '已恢复顺序播放');
  }
  function jumpToOrderPos(pos) {
    state.orderPos = pos;
    loadByOrder();
    renderQueuePanel();
  }

  // ---------------------------------------------------------------- 播放页
  function openNp() {
    state.npOpen = true;
    $('#npView').hidden = false;
    // 沉浸式页打开时收起「⚙」浮出的原生播放台，否则它会(z9997)盖在播放页(z60)上
    document.body.classList.remove('cm-native-open');
    document.body.classList.add('cm-np-open');   // 沉浸式页打开时隐藏共享播放条
    document.body.style.overflow = 'hidden';
    renderNowPlaying();
    renderQueuePanel();
    state.lyricIndex = -1;
    syncLyrics(audio.currentTime || 0);
  }
  function closeNp() {
    state.npOpen = false;
    $('#npView').hidden = true;
    document.body.classList.remove('cm-np-open');
    document.body.style.overflow = '';
  }
  // 供共享播放条 player.js 点击曲名 / 封面时展开沉浸式播放页
  window.__cmOpenNp = openNp;
  function renderNowPlaying() {
    const t = state.tracks.find((x) => x.id === state.currentId);
    $('#npvTitle').textContent = t ? (t.title || t.name) : '未在播放';
    $('#npvArtist').textContent = t ? (t.artist || '未知艺人') : '—';
    $('#npvBitrate').textContent = t && t.bitrate ? Math.round(t.bitrate) + ' kbps' : '码率未知';
    $('#npvFormat').textContent = t ? String(t.ext || '').replace('.', '').toUpperCase() : '—';
    // 没有封面图时按曲目 id 生成稳定的封面色相
    const hue = t ? ((parseInt(t.id.slice(0, 2), 16) || 0) % 60 + 18) : 28;
    $('#npCover').style.background =
      'linear-gradient(145deg, hsl(' + hue + ' 45% 38%), hsl(' + hue + ' 35% 18%) 55%, #1a1410)';
    renderMode();
    renderPlayIcons();
    renderQuality(t);
  }
  function renderMode() {
    const map = { off: '关', all: '列表循环', one: '单曲循环' };
    const label = state.shuffle ? '随机轮询'
      : state.repeat === 'all' ? '列表循环'
      : state.repeat === 'one' ? '单曲循环' : '顺序播放';
    $('#npvMode').textContent = label;
    $('#repeatBtn').classList.toggle('on', state.repeat !== 'off');
    $('#npvRepeat').classList.toggle('on', state.repeat !== 'off');
    $('#npvRepeatOne').hidden = state.repeat !== 'one';
    $('#repeatBtn').title = '循环：' + map[state.repeat];
    $('#npvRepeat').title = $('#repeatBtn').title;
    $('#shuffleBtn').classList.toggle('on', state.shuffle);
    $('#npvShuffle').classList.toggle('on', state.shuffle);
    const st = state.shuffle ? '随机轮询：开（整队洗牌、依次播放）' : '随机播放：关';
    $('#shuffleBtn').title = st;
    $('#npvShuffle').title = st;
  }
  function renderPlayIcons() {
    const paused = audio.paused;
    const p = '<path d="M8 5v14l11-7z"/>';
    const q = '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>';
    $('#playIcon').innerHTML = paused ? p : q;
    $('#npvPlayIcon').innerHTML = paused ? p : q;
    const pe = $('#player');
    if (pe) pe.dataset.playing = paused ? 'false' : 'true';
  }
  function renderQueuePanel() {
    const box = $('#npvQueue');
    if (!box) return;
    if (!state.order.length) { box.innerHTML = '<div class="q-head">队列为空</div>'; return; }
    const loop = state.repeat === 'all' || state.shuffle;
    box.innerHTML = '<div class="q-head">' + (state.shuffle ? '随机轮询顺序' : '播放队列') +
      ' · 共 ' + state.queue.length + ' 首' + (loop ? '（播完自动再轮一轮）' : '（播完即停）') + '</div>';
    for (let k = 0; k < state.order.length; k++) {
      const pos = (state.orderPos + k) % state.order.length;
      const t = state.tracks.find((x) => x.id === state.queue[state.order[pos]]);
      if (!t) continue;
      const row = document.createElement('div');
      row.className = 'q-row' + (k === 0 ? ' on' : '');
      row.innerHTML = '<span class="q-n">' + (k === 0 ? '▶' : (k + 1)) + '</span>' +
        '<span class="q-t">' + escapeHtml(t.title || t.name) + '</span>';
      row.addEventListener('click', () => jumpToOrderPos(pos));
      box.appendChild(row);
    }
  }

  // ---------------------------------------------------------------- 歌词（LRC 自动滚动）
  function parseLRC(text) {
    const out = [];
    const tagRe = /\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;
    const om = /\[offset:\s*(-?\d+)\s*\]/i.exec(text || '');
    const offset = om ? parseInt(om[1], 10) / 1000 : 0;   // 毫秒 → 秒
    (text || '').split(/\r?\n/).forEach((line) => {
      const times = [];
      let m;
      tagRe.lastIndex = 0;
      while ((m = tagRe.exec(line))) {
        times.push((+m[1]) * 60 + (+m[2]) + (m[3] ? parseFloat('0.' + m[3]) : 0));
      }
      if (!times.length) return;
      tagRe.lastIndex = 0;
      const content = line.replace(tagRe, '').trim();
      times.forEach((tt) => out.push({ time: Math.max(0, tt + offset), text: content }));
    });
    out.sort((a, b) => a.time - b.time);
    return out;
  }
  function renderLyrics() {
    $$('.js-lyrics').forEach((box) => {
      if (!state.lyrics.length) {
        box.innerHTML = '<div class="ly-empty">这首歌还没有歌词。<br>' +
          '支持标准 LRC 时间轴，播放时会自动滚动并高亮当前句。<br>' +
          '<button type="button" class="ly-add-btn">添加 / 粘贴歌词</button></div>';
        const b = box.querySelector('.ly-add-btn');
        if (b) b.addEventListener('click', openLyricEditor);
        return;
      }
      box.innerHTML = '';
      state.lyrics.forEach((line) => {
        const el = document.createElement('div');
        el.className = 'ly-line';
        el.textContent = line.text || '♪';
        el.addEventListener('click', () => { if (isFinite(audio.duration)) audio.currentTime = line.time; });
        box.appendChild(el);
      });
      box.scrollTop = 0;
    });
    state.lyricIndex = -1;
  }
  function syncLyrics(t) {
    if (!state.lyrics.length) return;
    let lo = 0, hi = state.lyrics.length - 1, found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (state.lyrics[mid].time <= t) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    if (found < 0 || found === state.lyricIndex) return;
    state.lyricIndex = found;
    // 桌面侧栏与沉浸式页里的歌词各自独立滚动 / 高亮（仅滚动可见容器）
    $$('.js-lyrics').forEach((box) => {
      if (!box.offsetParent) return;
      const lines = $$('.ly-line', box);
      lines.forEach((el, i) => el.classList.toggle('on', i === found));
      const cur = lines[found];
      if (!cur) return;
      const delta = cur.getBoundingClientRect().top - box.getBoundingClientRect().top
        - box.clientHeight / 2 + cur.clientHeight / 2;
      box.scrollTo({ top: box.scrollTop + delta, behavior: 'smooth' });
    });
  }
  async function loadLyrics(id) {
    state.lyrics = []; state.lyricsRaw = '';
    if (id) {
      try {
        const r = await api('api/lyrics/' + encodeURIComponent(id));
        state.lyricsRaw = r.lyrics || '';
        state.lyrics = parseLRC(state.lyricsRaw);
      } catch (_) { /* 拿不到歌词按无歌词处理 */ }
    }
    renderLyrics();
    state.lyricIndex = -1; syncLyrics(audio.currentTime || 0);
  }
  function openLyricEditor() {
    if (!state.currentId) { toast('请先播放一首歌曲'); return; }
    $('#lyricErr').textContent = '';
    $('#lyricText').value = state.lyricsRaw || '';
    $('#lyricModal').classList.add('show');
    $('#lyricText').focus();
  }
  async function saveLyrics() {
    if (!state.currentId) return;
    const text = $('#lyricText').value;
    if (text.trim() && !parseLRC(text).length) {
      $('#lyricErr').textContent = '没识别到时间标签，请检查 [mm:ss.xx] 格式';
      return;
    }
    try {
      await api('api/tracks/' + encodeURIComponent(state.currentId), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lyrics: text }),
      });
      state.lyricsRaw = text;
      state.lyrics = parseLRC(text);
      renderLyrics();
      state.lyricIndex = -1;
      syncLyrics(audio.currentTime || 0);
      $('#lyricModal').classList.remove('show');
      toast(text.trim() ? '歌词已保存' : '歌词已清除');
      await loadAll();
    } catch (e) {
      $('#lyricErr').textContent = '保存失败：' + (e.message || e);
    }
  }
  async function clearLyrics() {
    if (!state.currentId) { toast('请先播放一首歌曲'); return; }
    if (!state.lyricsRaw || !confirm('确定清除这首歌的歌词吗？')) return;
    try {
      await api('api/tracks/' + encodeURIComponent(state.currentId), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lyrics: '' }),
      });
      state.lyricsRaw = ''; state.lyrics = [];
      renderLyrics();
      toast('歌词已清除');
      await loadAll();
    } catch (e) { toast('清除失败：' + (e.message || e)); }
  }

  // ---------------------------------------------------------------- 码率 / 音质
  function renderQuality(t) {
    const sel = $('#npvQualitySel');
    if (!sel) return;
    const steps = (state.meta.bitrate_steps && state.meta.bitrate_steps.length)
      ? state.meta.bitrate_steps : (state.meta.transcode ? STEPS_FALLBACK : []);
    const orig = t && t.bitrate ? Math.round(t.bitrate) : 0;
    let html = '<option value="0">原码率' + (orig ? '（约 ' + orig + ' kbps）' : '') + '</option>';
    steps.forEach((k) => {
      if (orig && k >= orig) return;        // 不做无意义的升码
      html += '<option value="' + k + '">' + k + ' kbps · 省流量</option>';
    });
    sel.innerHTML = html;
    const valid = $$('#npvQualitySel option').map((o) => o.value);
    if (!valid.includes(String(state.quality))) state.quality = 0;
    sel.value = String(state.quality);
    $('#npvQualityHint').textContent = state.meta.transcode
      ? '已启用 ffmpeg：切换码率会按需转码并缓存，播放进度保持不变。'
      : '服务器未找到 ffmpeg，仅可播放原始码率；在 config.json 设置 ffmpeg_path 并重启云音乐即可启用码率切换。';
  }
  function applyQuality() {
    if (!state.currentId) return;
    const wasPlaying = !audio.paused;
    const pos = audio.currentTime;
    pendingSeek = isFinite(pos) && pos > 0 ? pos : null;
    audio.src = trackUrl(state.currentId, state.quality);
    if (wasPlaying) audio.play().catch(() => {});
  }

  // ---------------------------------------------------------------- 播放器事件
  function wirePlayer() {
    $('#playBtn').addEventListener('click', togglePlay);
    $('#npvPlay').addEventListener('click', togglePlay);
    $('#nextBtn').addEventListener('click', () => next(true));
    $('#npvNext').addEventListener('click', () => next(true));
    $('#prevBtn').addEventListener('click', prev);
    $('#npvPrev').addEventListener('click', prev);

    audio.addEventListener('play', () => { renderPlayIcons(); renderLibrary(); });
    audio.addEventListener('pause', () => { renderPlayIcons(); renderLibrary(); });
    // 电台同步模式下由共享播放条按服务端时间轴接下一首，本页不能重复切歌
    audio.addEventListener('ended', () => {
      if (inRadio()) return;
      next(false);                   // 必须包一层，否则 Event 会被当成 manual
    });
    audio.addEventListener('loadedmetadata', () => {
      if (pendingSeek != null && isFinite(audio.duration)) {
        audio.currentTime = Math.min(pendingSeek, audio.duration);
        pendingSeek = null;
      }
      $('#durTime').textContent = fmtTime(audio.duration);
      $('#npvDur').textContent = fmtTime(audio.duration);
    });
    audio.addEventListener('timeupdate', () => {
      if (state.seeking || !isFinite(audio.duration)) return;
      const pct = (audio.currentTime / audio.duration) * 1000;
      $('#seek').value = String(pct);
      $('#npvSeek').value = String(pct);
      $('#curTime').textContent = fmtTime(audio.currentTime);
      $('#npvCur').textContent = fmtTime(audio.currentTime);
      syncLyrics(audio.currentTime);
    });
    const bindSeek = (el) => {
      el.addEventListener('input', () => { leaveRadio(); state.seeking = true; });
      el.addEventListener('change', () => {
        if (isFinite(audio.duration)) audio.currentTime = (el.value / 1000) * audio.duration;
        state.seeking = false;
      });
    };
    bindSeek($('#seek'));
    bindSeek($('#npvSeek'));

    const vol = $('#vol');
    vol.addEventListener('input', () => { audio.volume = parseFloat(vol.value); });
    audio.volume = parseFloat(vol.value);

    $('#repeatBtn').addEventListener('click', setRepeat);
    $('#npvRepeat').addEventListener('click', setRepeat);
    $('#shuffleBtn').addEventListener('click', setShuffle);
    $('#npvShuffle').addEventListener('click', setShuffle);

    // 播放页开关
    $('#expandBtn').addEventListener('click', (e) => { e.stopPropagation(); openNp(); });
    $('#cover').addEventListener('click', openNp);
    $('#player .now .meta').addEventListener('click', openNp);
    $('#npClose').addEventListener('click', closeNp);
    $('#npQueueBtn').addEventListener('click', () => {
      state.queueOpen = !state.queueOpen;
      $('#npvQueue').hidden = !state.queueOpen;
      $('#npvPanelTitle').textContent = state.queueOpen ? '播放队列' : '歌词';
      $('#npvLyrics').style.display = state.queueOpen ? 'none' : '';
      renderQueuePanel();
    });

    // 码率切换
    $('#npvQualitySel').addEventListener('change', () => {
      state.quality = parseInt($('#npvQualitySel').value, 10) || 0;
      applyQuality();
      toast(state.quality ? '已切换到 ' + state.quality + ' kbps' : '已切换回原码率');
    });

    // 歌词编辑（沉浸式页与桌面侧栏都有入口）
    $('#npvLyricEdit').addEventListener('click', openLyricEditor);
    $('#npvLyricClear').addEventListener('click', clearLyrics);
    $$('.ly-add').forEach((b) => b.addEventListener('click', openLyricEditor));
    $$('.ly-clear').forEach((b) => b.addEventListener('click', clearLyrics));
    $('#lyricCancel').addEventListener('click', () => $('#lyricModal').classList.remove('show'));
    $('#lyricSave').addEventListener('click', saveLyrics);
    $('#lyricPick').addEventListener('click', () => $('#lyricFile').click());
    $('#lyricFile').addEventListener('change', () => {
      const f = $('#lyricFile').files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = () => { $('#lyricText').value = r.result; $('#lyricErr').textContent = '已载入 ' + f.name; };
      r.onerror = () => { $('#lyricErr').textContent = '文件读取失败'; };
      r.readAsText(f, 'utf-8');
      $('#lyricFile').value = '';
    });

    renderMode();
    renderPlayIcons();

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if ($('#lyricModal').classList.contains('show')) { $('#lyricModal').classList.remove('show'); return; }
        if (state.npOpen) { closeNp(); return; }
      }
      if (e.code === 'Space' && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || ''))) {
        e.preventDefault(); togglePlay();
      }
    });
  }

  // ---------------------------------------------------------------- 上传交互
  function wireUpload() {
    const dz = $('#dropzone'), input = $('#fileInput');
    const toggle = $('#uploadToggleBtn');
    if (toggle) toggle.addEventListener('click', () => setUploadOpen(!uploadOpen));
    setUploadOpen(false);              // 与 HTML 的 hidden 一致：默认收起
    $('#pickBtn').addEventListener('click', () => input.click());
    dz.addEventListener('click', (e) => {
      // 「选择文件」按钮自己会触发一次；点路径文字时不要弹出文件框（方便复制）
      if (e.target.closest && (e.target.closest('#pickBtn') || e.target.closest('code'))) return;
      if (e.target === dz || (e.target.closest && e.target.closest('.dz-inner'))) input.click();
    });
    input.addEventListener('change', () => { if (input.files.length) uploadFiles(input.files); input.value = ''; });
    ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('drag'); }));
    dz.addEventListener('drop', (e) => {
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) uploadFiles(files);
    });
    const cancel = $('#upCancelBtn');
    if (cancel) cancel.addEventListener('click', () => {
      uploadAbort = true;
      if (currentXhr) currentXhr.abort();
      setUploadProgress('正在取消…', 0, '—');
    });
  }

  // ---------------------------------------------------------------- 曲库操作按钮
  function wireLibraryOps() {
    const rb = $('#resortBtn'), rf = $('#refreshBtn');
    if (rb) rb.addEventListener('click', resortLibrary);
    if (rf) rf.addEventListener('click', refreshLibrary);
  }

  // ---------------------------------------------------------------- 删除曲目
  async function deleteTrack(id) {
    const t = state.tracks.find((x) => x.id === id);
    if (!window.confirm('从音乐库删除「' + ((t && (t.title || t.name)) || '该曲目') + '」？此操作不可恢复。')) return;
    try {
      await api('api/tracks/' + id, { method: 'DELETE' });
      if (state.currentId === id) stopPlay();
      await loadAll();
      toast('已删除：' + ((t && (t.title || t.name)) || ''));
    } catch (e) { if (e.status !== 401) toast('删除失败：' + (e.message || e)); }
  }

  // ---------------------------------------------------------------- 随机重排 / 刷新
  async function resortLibrary() {
    if (!state.tracks.length) { toast('曲库是空的，无需重排'); return; }
    if (!window.confirm('将随机重排全部 ' + state.tracks.length +
        ' 首，并把每首曲目的上传时间随机重写一遍（每次点击顺序都不一样）。确定继续？')) return;
    try {
      const r = await api('api/resort', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ by: 'random' }) });
      await loadAll();
      toast('已随机重排 ' + (r.count || state.tracks.length) + ' 首');
    } catch (e) { if (e.status !== 401) toast('重排失败：' + (e.message || e)); }
  }
  async function refreshLibrary() {
    try { await loadAll(); toast('已刷新曲库'); }
    catch (e) { if (e.status !== 401) toast('刷新失败：' + (e.message || e)); }
  }

  // ---------------------------------------------------------------- 登录（独立运行）
  function showLogin() {
    // 挂在门户子路径下时由 nginx 处理登录，这里不该出现；独立运行才显示
    if (location.pathname.replace(/\/$/, '').indexOf('/music') !== -1 && window.__prefix) { /* noop */ }
    $('#loginModal').classList.add('show');
  }
  function wireLogin() {
    const doLogin = async () => {
      const u = $('#lu').value.trim(), p = $('#lp').value;
      $('#loginErr').textContent = '';
      try {
        const r = await api('api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p }) });
        if (r.ok) location.reload();
      } catch (e) {
        if (e.status !== 401) $('#loginErr').textContent = '网络错误';
        else $('#loginErr').textContent = (e.body && e.body.message) || '用户名或密码不正确';
      }
    };
    $('#loginBtn').addEventListener('click', doLogin);
    $('#lp').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
    $('#lu').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#lp').focus(); });
  }

  // ---------------------------------------------------------------- 初始化
  // 从后端读取实际保存目录与单首上限，直接显示在上传区
  async function loadMeta() {
    try {
      const m = await api('api/meta');
      if (m.max_upload_mb) {
        $('#dzHint').textContent = '支持 mp3 / flac / wav / m4a / ogg 等，单首最大 ' + m.max_upload_mb + 'MB';
      }
      if (m.upload_dir) {
        $('#storePath').textContent = m.upload_dir;
        $('#dzStore').hidden = false;
      }
      state.meta.transcode = !!m.transcode;
      state.meta.bitrate_steps = Array.isArray(m.bitrate_steps) ? m.bitrate_steps : [];
    } catch (_) { /* meta 不可用不影响上传 */ }
  }

  async function loadAll() {
    try {
      const t = await api('api/tracks');
      state.tracks = t.items || [];
      renderLibrary();
      if (state.npOpen) { renderNowPlaying(); renderQueuePanel(); }
    } catch (e) {
      if (e.status !== 401) toast('加载失败：' + (e.message || e));
    }
  }

  async function init() {
    wirePlayer(); wireUpload(); wireLibraryOps(); wireLogin(); wireRadio();
    try {
      const s = await api('api/session');
      if (!s.ok) { showLogin(); return; }
    } catch (e) {
      if (e.status === 401) { showLogin(); return; }
      // 其它错误（如后端未就绪）继续尝试加载
    }
    await loadMeta();
    await loadAll();
    // 接入跨应用常驻播放条：控制委托回本页原有逻辑（队列/随机/循环/歌词都保留）
    if (window.CMPlayer) {
      CMPlayer.attach({
        type: 'music',
        onToggle: togglePlay,
        onNext: () => next(true),
        onPrev: prev,
        onRestore: (s) => {
          if (!s || s.type !== 'music' || !s.id) return false;
          if (!state.tracks.some((x) => x.id === s.id)) return false;
          playTrackFromLibrary(s.id);                 // 重建队列并播放
          if (s.position > 1) {
            audio.addEventListener('loadedmetadata', function once() {
              try { audio.currentTime = s.position; } catch (_) {}
              audio.removeEventListener('loadedmetadata', once);
            });
          }
          return true;
        }
      });
      if (!inRadio()) {
        CMPlayer.resume();     // 个人模式：数据就绪后再做本地状态接力
      } else {
        // 电台已在本页 init 之前接入（事件错过）→ 补一次 UI / 歌词同步
        const s = CMPlayer.state();
        if (s && s.type === 'music' && s.id) syncRadioUI(s.id, s.index);
      }
    }
    // 从其它应用点共享播放条的曲名跳进来（/music/#np）时自动展开沉浸式播放页
    if (location.hash === '#np') {
      openNp();
      history.replaceState(null, '', location.pathname);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
