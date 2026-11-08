/* player.js —— 云媒中心「跨应用常驻播放条 + 状态接力」
 *
 * 背景：4 个应用是各自独立的整页（nginx 子路径反代到不同 exe），切换器是整页跳转，
 *       浏览器会销毁当前页的 <audio>，声音必然中断。本模块用 localStorage 做状态接力：
 *       页面加载后读取「最后播放状态」，自动重建音源并续播（约 1 秒重新缓冲）。
 *
 * 设计要点：
 *  1. 复用页面已有的 audio 元素（云音乐是 #audio），否则创建 #cmAudio，保证全站只有一个音源，
 *     各应用内部逻辑（歌词同步 / HLS / 沉浸式播放页 / EPG）完全不用改，只是把播放源换成同一个元素。
 *  2. 各应用通过 CMPlayer.report(...) 上报「当前音源 + 播放列表快照」；通过 CMPlayer.attach(...)
 *     注册控制回调（上一首/下一首/播放暂停/原生页恢复），共享条优先委托给应用，保留队列/随机逻辑。
 *  3. 在没有原生播放器的页面（工作日志 / 在线电视），共享条自己用绝对音源 URL 播放，
 *     并用保存的列表快照切上一首/下一首——这就是「切到工作表仍保留最后状态、能继续切歌/切台」。
 *  4. 音源用绝对路径（/music/api/track/... 、/radio/api/stream/...），同域、带登录 Cookie，
 *     任何子路径页面都能播放。
 *  5. 浏览器禁止无手势自动播放时，播放条显示「点按继续」，一次点击即可恢复。
 *
 * 范围：UI 只在窄屏（<=900px，手机/平板）显示并替换各应用自带底栏；
 *       桌面端不注入界面、不隐藏任何原生播放器（保留音乐页并排歌词台），但状态接力照常生效。
 *       样式自带，按 body 背景亮度自动适配深浅主题，不依赖各应用 CSS 变量。
 */
(function () {
  'use strict';

  var KEY = 'cm.player.v1';
  var APP_URL = { radio: '/radio/', tv: '/tv/', music: '/music/', work: '/work/' };
  var m = location.pathname.match(/\/(radio|tv|music|work)\//);
  if (!m) return;                       // 独立运行（非门户子路径）不启用，避免绝对 URL 失效
  var here = m[1];

  // ---------------- 状态存取 ----------------
  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || 'null') || null; }
    catch (_) { return null; }
  }
  function save(s) {
    try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (_) { /* 隐私模式 */ }
  }
  var st = load() || {
    type: '', id: '', title: '未在播放', sub: '选择一首歌曲开始',
    list: [], index: -1, url: '', q: 0, streamIdx: 0, hls: false,
    position: 0, duration: 0, playing: false, ts: 0
  };

  // ---------------- 唯一音源 ----------------
  // 云音乐页面已有 <audio id="audio">；其它页面新建一个。两者都作为全站共享音源。
  var audio = document.getElementById('audio');   // 云音乐页已有 <audio id="audio">
  if (!audio) {
    audio = document.createElement('audio');
    audio.id = 'cmAudio';         // 云收音机 / 工作日志 / 电视共用这一个 ID
    audio.preload = 'none';
    audio.crossOrigin = 'anonymous';
    audio.setAttribute('playsinline', '');
    document.body.appendChild(audio);
  }
  var app = null;                 // 当前页注册的原生播放器控制回调
  var ownHls = null;              // 非原生页播放 HLS 时用
  var hlsWait = null;             // hls.js 动态加载队列
  var resumed = false;
  var seekPending = null;
  var unloading = false;          // 切页时不要把 pause 误写成「已停止」
  var switching = false;          // 换源过程中同理
  var reportGrace = 0;            // report 后短时间忽略异步 pause 事件，避免把「播放中」写成停止

  // ---------------- 主题 ----------------
  function isDark() {
    try {
      var bg = getComputedStyle(document.body).backgroundColor || '';
      var c = bg.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
      if (!c) {
        var h = bg.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
        if (h) {
          var s = h[1].length === 3 ? h[1].split('').map(function (x) { return x + x; }).join('') : h[1];
          c = [null, parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)];
        }
      }
      if (!c) return true;
      return (0.299 * (+c[1]) + 0.587 * (+c[2]) + 0.114 * (+c[3])) < 128;
    } catch (_) { return true; }
  }
  var dark = isDark();
  var barBg = dark ? 'rgba(33,27,21,.95)' : 'rgba(255,252,247,.96)';
  var barFg = dark ? '#f4ede2' : '#1f1b18';
  var barSub = dark ? '#968878' : '#8a7a68';
  var barLine = dark ? 'rgba(255,255,255,.12)' : 'rgba(60,44,28,.16)';
  var accent = '#e0a458';

  // ---------------- 样式 ----------------
  var css = document.createElement('style');
  css.textContent =
    '#cmPBar{display:none;position:fixed;left:0;right:0;bottom:0;z-index:9998;' +
      'flex-direction:column;align-items:stretch;justify-content:center;gap:6px;' +
      'padding:8px 12px calc(8px + env(safe-area-inset-bottom));' +
      'background:' + barBg + ';color:' + barFg + ';border-top:1px solid ' + barLine +
      ';backdrop-filter:blur(14px);box-sizing:border-box;min-width:0}' +
    '#cmPBar .cm-cov{flex:0 0 auto;width:44px;height:44px;border-radius:10px;display:flex;' +
      'align-items:center;justify-content:center;font-size:20px;color:#1b1109;' +
      'background:linear-gradient(160deg,' + accent + ',#c8763c)}' +
    '#cmPBar .cm-meta{flex:1 1 auto;min-width:0;overflow:hidden;cursor:pointer}' +
    '#cmPBar .cm-title{font-size:13.5px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
    '#cmPBar .cm-sub{font-size:11px;line-height:1.4;color:' + barSub +
      ';white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
    '#cmPBar .cm-tag{flex:0 0 auto;font-size:10px;padding:2px 7px;border-radius:99px;' +
      'border:1px solid ' + barLine + ';color:' + barSub + ';white-space:nowrap}' +
    '#cmPBar .cm-btns{flex:0 0 auto;display:flex;align-items:center;gap:4px}' +
    '#cmPBar button{flex:0 0 auto;width:36px;height:36px;border:none;border-radius:50%;cursor:pointer;' +
      'background:transparent;color:inherit;display:flex;align-items:center;justify-content:center;padding:0}' +
    '#cmPBar button:active{background:rgba(127,127,127,.18)}' +
    '#cmPBar button.cm-play{width:44px;height:44px;background:' + accent + ';color:#1b1109}' +
    '#cmPBar button:disabled{opacity:.4;cursor:default}' +
    '#cmPBar svg{display:block}' +
    '#cmPBar .cm-row{display:flex;align-items:center;gap:10px;min-width:0}' +
    /* 第二行：当前播放时间 + 进度条 + 剩余时间 */
    '#cmPBar .cm-seek{display:flex;align-items:center;gap:8px;min-width:0;' +
      'font-size:10.5px;color:' + barSub + ';font-variant-numeric:tabular-nums}' +
    '#cmPBar .cm-cur,#cmPBar .cm-left{flex:0 0 auto;min-width:38px;white-space:nowrap}' +
    '#cmPBar .cm-left{text-align:right}' +
    '#cmPBar .cm-prog{flex:1 1 auto;height:4px;border-radius:99px;overflow:hidden;' +
      'background:rgba(127,127,127,.3)}' +
    '#cmPBar .cm-prog i{display:block;height:100%;width:0%;border-radius:99px;background:' + accent + '}' +
    '#cmPBar .cm-resume{position:absolute;right:12px;top:-38px;font-size:11.5px;padding:6px 12px;' +
      'border-radius:99px;background:' + accent + ';color:#1b1109;font-weight:600;cursor:pointer;' +
      'box-shadow:0 8px 22px rgba(0,0,0,.35);display:none}' +
    '#cmPBar .cm-more{width:32px !important;height:32px !important;font-size:15px;opacity:.75}' +
    /* ---- 桌面 + 手机统一：全站只有底部这一条常驻播放条 ---- */
    '#cmPBar{display:flex}' +
    /* 底部留白由播放条实测高度驱动（--cm-bar-h 在下方 JS 里测量写入），
       加时间行 / 字体缩放 / 横竖屏变化都不会再遮住列表底部。
       条本身的 padding 已含 safe-area，这里不再重复叠加 env()。 */
    'body.cm-player-on{padding-bottom:calc(var(--cm-bar-h, 104px) + 16px) !important}' +
    /* 只隐藏「音乐播放台 #player」「收音机播放条 #player」；带 id 精确选择器，
       避免误伤云电视同名的 .stage 视频容器（其 id 是 stage 不是 player） */
    'body.cm-player-on .stage#player,body.cm-player-on .player#player{display:none !important}' +
    /* 点「⚙」浮出原生面板，保留 EPG / 收藏 / 线路 / 音量 / 定时 / 循环 / 随机 / 歌词 */
    'body.cm-player-on.cm-native-open .stage#player,' +
    'body.cm-player-on.cm-native-open .player#player{display:block !important;' +
      'position:fixed !important;left:8px;right:8px;top:auto !important;' +
      'bottom:calc(var(--cm-bar-h, 104px) + 8px) !important;z-index:9997;' +
      'max-height:60vh;overflow:auto;border-radius:14px;' +
      'box-shadow:0 14px 40px rgba(0,0,0,.5)}' +
    'body.cm-player-on .toast{bottom:calc(var(--cm-bar-h, 104px) + 16px) !important}' +
    /* 沉浸式播放页打开时共享条让位；同时强制收起「⚙」浮出的原生播放台，
       否则它会以 z9997 盖在 z60 的播放页进度/控制区上，造成卡片重叠 */
    'body.cm-np-open #cmPBar{display:none !important}' +
    'body.cm-np-open{padding-bottom:0 !important}' +
    'body.cm-np-open.cm-player-on .stage#player,' +
    'body.cm-np-open.cm-player-on .player#player{display:none !important}' +
    'body.cm-np-open.cm-player-on.cm-native-open .stage#player,' +
    'body.cm-np-open.cm-player-on.cm-native-open .player#player{display:none !important}' +
    /* 音乐页桌面端原本是「列表 + 播放台」两栏，播放台隐藏后第二列会留空 → 改单列 */
    (here === 'music'
      ? 'body.cm-player-on .layout{grid-template-columns:minmax(0,1fr) !important;' +
          'max-width:920px}' : '') +
    '@media(max-width:380px){#cmPBar .cm-tag{display:none}}';
  document.head.appendChild(css);
  document.body.classList.add('cm-player-on');

  // ---------------- DOM ----------------
  var bar = document.createElement('div');
  bar.id = 'cmPBar';
  bar.innerHTML =
    '<div class="cm-resume">点按继续播放 ▶</div>' +
    '<div class="cm-row">' +
      '<div class="cm-cov">♪</div>' +
      '<div class="cm-meta"><div class="cm-title"></div><div class="cm-sub"></div></div>' +
      '<span class="cm-tag"></span>' +
      '<div class="cm-btns">' +
        '<button class="cm-prev" title="上一首" aria-label="上一首"></button>' +
        '<button class="cm-play" title="播放/暂停" aria-label="播放/暂停"></button>' +
        '<button class="cm-next" title="下一首" aria-label="下一首"></button>' +
        (here === 'radio' || here === 'music'
          ? '<button class="cm-more" title="更多播放设置" aria-label="更多播放设置">⚙</button>' : '') +
      '</div>' +
    '</div>' +
    '<div class="cm-seek">' +
      '<span class="cm-cur" title="已播放时间">0:00</span>' +
      '<div class="cm-prog"><i></i></div>' +
      '<span class="cm-left" title="剩余时间">--:--</span>' +
    '</div>';
  document.body.appendChild(bar);

  // 播放条实测高度 → --cm-bar-h，驱动页面底部留白 / ⚙ 浮层 / 提示条。
  // ResizeObserver 跟踪内容变化（加时间行、字体缩放、横竖屏），旧浏览器回落 resize 事件。
  function syncBarHeight() {
    try {
      var h = bar.getBoundingClientRect && bar.getBoundingClientRect().height;
      var root = document.documentElement;
      if (h > 0 && root && root.style && root.style.setProperty) {
        root.style.setProperty('--cm-bar-h', Math.ceil(h) + 'px');
      }
    } catch (_) { /* 测量失败时用 CSS 里的 104px 兜底 */ }
  }
  syncBarHeight();
  if (typeof ResizeObserver === 'function') {
    try { new ResizeObserver(syncBarHeight).observe(bar); } catch (_) { /* 忽略 */ }
  }
  window.addEventListener('resize', syncBarHeight);
  window.addEventListener('orientationchange', function () { setTimeout(syncBarHeight, 250); });

  var elTitle = bar.querySelector('.cm-title');
  var elSub = bar.querySelector('.cm-sub');
  var elTag = bar.querySelector('.cm-tag');
  var elCov = bar.querySelector('.cm-cov');
  var elFill = bar.querySelector('.cm-prog i');
  var elCur = bar.querySelector('.cm-cur');
  var elLeft = bar.querySelector('.cm-left');
  var elResume = bar.querySelector('.cm-resume');
  var btnPrev = bar.querySelector('.cm-prev');
  var btnPlay = bar.querySelector('.cm-play');
  var btnNext = bar.querySelector('.cm-next');

  var ICO_PREV = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M7 6v12H5V6h2zm12 0v12l-9-6 9-6z"/></svg>';
  var ICO_NEXT = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M17 6v12h2V6h-2zM5 6v12l9-6-9-6z"/></svg>';
  var ICO_PLAY = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  var ICO_PAUSE = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M7 5h4v14H7zm6 0h4v14h-4z"/></svg>';
  btnPrev.innerHTML = ICO_PREV;
  btnNext.innerHTML = ICO_NEXT;
  btnPlay.innerHTML = ICO_PLAY;

  // 秒 → M:SS / H:MM:SS
  function fmtClock(s, forceH) {
    s = Math.max(0, Math.floor(Number(s) || 0));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var q = s % 60;
    // forceH：时长跨小时时，剩余时间也带上小时位，避免出现 -58:59 这种歧义
    var withH = h > 0 || !!forceH;
    var mm = withH ? String(m).padStart(2, '0') : String(m);
    return (withH ? h + ':' : '') + mm + ':' + String(q).padStart(2, '0');
  }
  // 进度条 + 已播放时间 + 剩余时间（电台直播没有时长，显示「直播」）
  function paintProgress() {
    var dur = (isFinite(st.duration) && st.duration > 0) ? st.duration : 0;
    var cur = Math.max(0, Number(st.position) || 0);
    if (dur && cur > dur) cur = dur;
    var pct = dur ? Math.min(100, cur / dur * 100) : 0;
    elFill.style.width = (st.type === 'radio' ? 0 : pct).toFixed(1) + '%';
    var long = dur >= 3600;
    elCur.textContent = st.type ? fmtClock(cur, long) : '0:00';
    if (st.type === 'radio') elLeft.textContent = '直播';
    else if (!dur) elLeft.textContent = '--:--';
    else elLeft.textContent = '-' + fmtClock(dur - cur, long);
  }

  function render() {
    elTitle.textContent = st.title || '未在播放';
    elSub.textContent = st.sub || (st.type ? '来自云媒中心' : '选择一首歌曲开始');
    elTag.textContent = st.type === 'radio' ? '电台' : (st.type === 'music' ? '音乐' : '');
    elTag.style.display = st.type ? '' : 'none';
    elCov.textContent = st.type === 'radio' ? '📻' : '♪';
    btnPlay.innerHTML = st.playing ? ICO_PAUSE : ICO_PLAY;
    var dis = !st.type || !st.id;
    btnPrev.disabled = dis; btnPlay.disabled = dis; btnNext.disabled = dis;
    paintProgress();
  }
  render();

  // ---------------- 音源状态持久化 ----------------
  function persist(patch) {
    for (var k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) st[k] = patch[k];
    st.ts = Date.now();
    save(st);
    render();
  }
  var saveTid = null;
  audio.addEventListener('timeupdate', function () {
    st.position = audio.currentTime || 0;
    if (isFinite(audio.duration)) st.duration = audio.duration;
    paintProgress();                    // 时间与进度每次都刷新，只节流写盘
    if (saveTid) return;
    saveTid = setTimeout(function () { saveTid = null; save(st); }, 1500);
  });
  audio.addEventListener('loadedmetadata', function () {
    if (isFinite(audio.duration)) { st.duration = audio.duration; save(st); }
    paintProgress();
    if (seekPending != null) {
      try { audio.currentTime = seekPending; } catch (_) { /* 直播流不可 seek */ }
      seekPending = null;
    }
  });
  audio.addEventListener('play', function () { persist({ playing: true }); elResume.style.display = 'none'; });
  audio.addEventListener('pause', function () {
    // 切页/换源时的过渡 pause 不应清除状态，否则下一页无法续播
    if (!unloading && !switching && Date.now() > reportGrace) persist({ playing: false });
  });
  audio.addEventListener('ended', function () {
    // 原生页（音乐/收音机）自己的 app.js 已监听 ended 并切下一首，这里不能重复触发；
    // 只有工作日志 / 电视这类没有原生播放器的页面，才由共享条按列表快照自动接下一首。
    if (!app && st.type === 'music') step(1);
  });

  // ---------------- 对外 API ----------------
  /** 应用上报当前音源。p: {type,id,title,sub,list,index,url,q,streamIdx,hls,playing} */
  function report(p) {
    var next = {
      type: p.type || st.type, id: p.id || '', title: p.title || '', sub: p.sub || '',
      url: p.url || ''
    };
    if (p.list && p.list.length) { next.list = p.list; next.index = (typeof p.index === 'number' ? p.index : -1); }
    if (typeof p.q === 'number') next.q = p.q;
    if (typeof p.streamIdx === 'number') next.streamIdx = p.streamIdx;
    if (typeof p.hls === 'boolean') next.hls = p.hls;
    if (typeof p.playing === 'boolean') next.playing = p.playing;
    next.position = 0; next.duration = 0;
    // 应用接管播放：先拆掉接力用的 HLS 实例，避免两个 Hls 同时 attach 同一个 audio
    teardownHls();
    seekPending = null;
    reportGrace = Date.now() + 1500;
    persist(next);
    elResume.style.display = 'none';
  }
  /** 应用注册控制回调：{type,onToggle,onNext,onPrev,onRestore} */
  function attach(a) { app = a || null; }

  window.CMPlayer = {
    audio: audio,
    report: report,
    attach: attach,
    load: load,
    save: save,
    state: function () { return st; },
    here: here,
    resume: tryResume       // 原生页数据加载完后主动调用，避免过早占用共享音源
  };

  // ---------------- HLS（非原生页播放电台用） ----------------
  function ensureHls(cb) {
    if (window.Hls) { cb(window.Hls); return; }
    if (hlsWait) { hlsWait.push(cb); return; }
    hlsWait = [cb];
    var sc = document.createElement('script');
    sc.src = '/radio/vendor/hls.min.js';
    sc.onload = function () { var a = hlsWait; hlsWait = null; a.forEach(function (f) { f(window.Hls); }); };
    sc.onerror = function () { var a = hlsWait; hlsWait = null; a.forEach(function (f) { f(null); }); };
    document.head.appendChild(sc);
  }
  function teardownHls() {
    if (ownHls) { try { ownHls.destroy(); } catch (_) {} ownHls = null; }
  }

  /** 按类型 + 列表项重建绝对音源 URL */
  function urlFor(type, item) {
    if (!item || !item.id) return '';
    if (type === 'music') {
      return APP_URL.music + 'api/track/' + encodeURIComponent(item.id) + (st.q ? '?q=' + encodeURIComponent(st.q) : '');
    }
    if (type === 'radio') {
      return APP_URL.radio + 'api/stream/' + encodeURIComponent(item.id) + '?i=0';
    }
    return '';
  }

  /** 共享条自己播放（没有原生播放器的页面，或原生页类型不匹配时） */
  function playOwn(s, item) {
    var url = item ? urlFor(s.type, item) : s.url;
    if (!url) { elResume.style.display = 'block'; return; }
    teardownHls();
    switching = true;
    try { audio.pause(); } catch (_) {}
    switching = false;
    seekPending = (s.type === 'music' && s.position) ? s.position : null;
    if (s.hls) {
      ensureHls(function (H) {
        if (!H || !H.isSupported()) { audio.src = url; tryPlay(); return; }
        var h = new H({ enableWorker: true, maxBufferLength: 24 });
        ownHls = h;
        h.loadSource(url);
        h.attachMedia(audio);
        h.on(H.Events.MANIFEST_PARSED, function () { tryPlay(); });
        h.on(H.Events.ERROR, function (_e, d) {
          if (d && d.fatal) { teardownHls(); audio.src = url; tryPlay(); }
        });
      });
    } else {
      audio.src = url;
      tryPlay();
    }
    function tryPlay() {
      var pr = audio.play();
      if (pr && pr.catch) pr.catch(function () { elResume.style.display = 'block'; });
    }
  }

  // ---------------- 自动续播 ----------------
  function tryResume() {
    if (resumed) return;
    resumed = true;
    var s = load();
    if (!s || !s.playing || !s.url || !s.type) return;
    st = s; render();
    // 原生页且音源类型匹配：交给应用自己恢复（保留 HLS / 歌词 / EPG / 队列）
    if (app && app.type === s.type && typeof app.onRestore === 'function') {
      if (app.onRestore(s) !== false) return;
    }
    playOwn(s, null);
  }

  // ---------------- 播放条控制 ----------------
  function delegate(name) {
    if (app && app.type === st.type && typeof app[name] === 'function') { app[name](); return true; }
    return false;
  }
  btnPlay.addEventListener('click', function () {
    if (elResume.style.display === 'block') {
      var pr = audio.play();
      if (pr && pr.catch) pr.catch(function () {});
      elResume.style.display = 'none';
      return;
    }
    if (!st.type || !st.id) return;
    if (delegate('onToggle')) return;
    // 状态接力过来的页面（如工作日志/电视）音源还没建立：按保存的 URL 重建后再播
    if (!audio.getAttribute('src') && !audio.currentSrc) { playOwn(st, null); return; }
    if (audio.paused) {
      var p = audio.play(); if (p && p.catch) p.catch(function () { elResume.style.display = 'block'; });
    } else audio.pause();
  });
  function step(dir) {
    if (!st.type || !st.id) return;
    if (delegate(dir > 0 ? 'onNext' : 'onPrev')) return;
    if (!st.list || !st.list.length) return;
    var i = (st.index < 0 ? 0 : st.index) + dir;
    if (i < 0) i = st.list.length - 1;
    if (i >= st.list.length) i = 0;
    var item = st.list[i];
    persist({
      id: item.id, title: item.title || '', sub: item.sub || '', index: i,
      url: urlFor(st.type, item), position: 0, duration: 0, playing: true
    });
    playOwn(st, item);
  }
  btnNext.addEventListener('click', function () { step(1); });
  btnPrev.addEventListener('click', function () { step(-1); });
  var btnMore = bar.querySelector('.cm-more');
  if (btnMore) btnMore.addEventListener('click', function () {
    document.body.classList.toggle('cm-native-open');
    btnMore.style.opacity = document.body.classList.contains('cm-native-open') ? '1' : '.75';
  });
  elResume.addEventListener('click', function () {
    var p = audio.play(); if (p && p.catch) p.catch(function () {});
    elResume.style.display = 'none';
  });
  // 点曲名 / 封面：音乐 → 展开沉浸式播放页（本页直接开，跨页带 #np 自动开）；
  // 电台等其它类型仍跳回对应应用。打开前收起「⚙」浮出的原生面板，避免与播放页重叠。
  function openNowPlaying() {
    document.body.classList.remove('cm-native-open');
    if (btnMore) btnMore.style.opacity = '.75';
    if (st.type === 'music') {
      if (here === 'music' && typeof window.__cmOpenNp === 'function') {
        window.__cmOpenNp();
      } else {
        location.href = APP_URL.music + '#np';
      }
      return;
    }
    if (st.type && APP_URL[st.type]) location.href = APP_URL[st.type];
  }
  bar.querySelector('.cm-meta').addEventListener('click', openNowPlaying);
  elCov.addEventListener('click', openNowPlaying);
  // 切页前把状态写盘，保证下一页能接力；卸载时的 pause 不改变「播放中」标记
  function leaving() {
    unloading = true;
    // 电视页有独立视频音轨且未介入音频播放，不能覆盖跨页接力状态
    if (here !== 'tv') save(st);
  }
  window.addEventListener('pagehide', leaving);
  window.addEventListener('beforeunload', leaving);
  window.addEventListener('pageshow', function () { unloading = false; });

  // 工作日志页没有原生播放器：自动接力续播。
  // 在线音乐 / 收音机页：等 app.js 数据加载完并 attach 后主动调 CMPlayer.tryResume()，
  // 这样能优先走原生播放逻辑（歌词 / EPG / 队列），不会过早占用共享音源。
  // 在线电视页有视频音轨，不自动抢声音，只显示暂停态，点播放键才恢复。
  if (here === 'tv') {
    // 只把按钮显示成「播放」，不改动持久化的 playing 状态（保证切走后仍能接力）
    btnPlay.innerHTML = ICO_PLAY;
  } else if (here === 'work') {
    var kick = function () { setTimeout(tryResume, 250); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', kick);
    else kick();
  } else {
    // 原生页兜底：若 app.js 未接入或数据加载失败，仍保证能续播不静音
    setTimeout(function () { if (!resumed && !app) tryResume(); }, 2000);
  }
  window.CMPlayer.resume = window.CMPlayer.resume || tryResume;
})();
