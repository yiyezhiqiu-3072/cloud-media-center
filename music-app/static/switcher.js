/* switcher.js —— 各功能页共用的「跨应用切换」+「上传中跳转保护」
 *
 * 1. 顶栏注入一个下拉，直接切到收音机 / 电视 / 音乐 / 工作日志，不必先回首页。
 *    只在挂在门户子路径（/radio/ /tv/ /music/ /work/ /device/）时显示；独立运行不显示，
 *    避免 ../radio/ 之类相对链接变成死链。
 * 2. 音乐上传进行中，点任何会离开本页的链接都会先弹确认：确认后用**新标签页**
 *    打开目标，当前音乐页保持不动，上传继续；只有关掉这个音乐页 / 整个浏览器，
 *    上传才会停止。上传中关闭或刷新页面还会触发浏览器原生的离开提示。
 *
 * 上传状态由 music-app 的 app.js 暴露：window.__cmUploadActive() -> boolean。
 * 样式自带并按页面背景亮度自动适配深浅主题，不依赖各应用的 CSS 变量。
 */
(function () {
  'use strict';

  var APPS = [
    { key: 'radio', name: '在线收音机', url: '../radio/' },
    { key: 'tv',    name: '在线电视',   url: '../tv/' },
    { key: 'music', name: '在线音乐',   url: '../music/' },
    { key: 'work',  name: '工作日志',   url: '../work/' },
    { key: 'device', name: '客户分切设备管理', url: '../device/' },
  ];
  var NAMES = {};
  APPS.forEach(function (a) { NAMES[a.key] = a.name; });

  var m = location.pathname.match(/\/(radio|tv|music|work|device)\//);
  if (!m) return;                       // 独立运行：不显示切换器
  var current = m[1];
  var bar = document.querySelector('.topbar-right');
  if (!bar) return;

  // ---- 主题：按 body 背景亮度选一套配色 ----
  function isDark() {
    try {
      var bg = getComputedStyle(document.body).backgroundColor || '';
      var c = bg.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
      if (!c) {
        // 有些环境返回 #rrggbb / #rgb，一并支持
        var h = bg.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
        if (h) {
          var s = h[1].length === 3
            ? h[1].split('').map(function (x) { return x + x; }).join('')
            : h[1];
          c = [null, parseInt(s.slice(0, 2), 16),
               parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)];
        }
      }
      if (!c) return false;
      return (0.299 * (+c[1]) + 0.587 * (+c[2]) + 0.114 * (+c[3])) < 128;
    } catch (_) { return false; }
  }
  var dark = isDark();
  var panelBg = dark ? '#241d17' : '#fffcf7';
  var panelFg = dark ? '#f4ede2' : '#1f1b18';
  var panelLine = dark ? 'rgba(255,255,255,.14)' : 'rgba(60,44,28,.16)';
  var hoverBg = dark ? 'rgba(255,255,255,.09)' : 'rgba(60,44,28,.07)';

  var style = document.createElement('style');
  style.textContent =
    '.cm-switch{position:relative;display:inline-flex;flex:0 0 auto}' +
    '.cm-switch>button{display:inline-flex;align-items:center;gap:6px;cursor:pointer;' +
      'font-size:12px;line-height:1.4;padding:6px 12px;border-radius:99px;color:inherit;' +
      'background:rgba(127,127,127,.10);border:1px solid rgba(127,127,127,.32);white-space:nowrap}' +
    '.cm-switch>button:hover{background:rgba(127,127,127,.22)}' +
    /* fixed + 挂到 body：不受顶栏/父容器 overflow:hidden|clip 裁剪；left/top 由 JS 按按钮位置计算并夹在视口内 */
    '.cm-switch-menu{position:fixed;margin:0;z-index:100;box-sizing:border-box;' +
      'min-width:172px;max-width:calc(100vw - 16px);padding:6px;border-radius:12px;background:' + panelBg +
      ';color:' + panelFg + ';border:1px solid ' + panelLine +
      ';box-shadow:0 14px 36px rgba(0,0,0,.28)}' +
    '.cm-switch-menu[hidden]{display:none}' +
    '.cm-switch-menu a{display:block;padding:8px 10px;border-radius:8px;' +
      'font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;' +
      'text-decoration:none;color:inherit}' +
    '.cm-switch-menu a:hover{background:' + hoverBg + '}' +
    '.cm-switch-menu a.on{font-weight:700;background:' + hoverBg + '}' +
    '.cm-switch-menu a.on::after{content:" · 当前";font-weight:400;opacity:.65;font-size:11px}' +
    '.cm-overlay{position:fixed;inset:0;z-index:110;display:flex;align-items:center;' +
      'justify-content:center;padding:20px;background:rgba(10,7,4,.62);backdrop-filter:blur(3px)}' +
    '.cm-overlay[hidden]{display:none}' +
    '.cm-dialog{width:min(430px,100%);padding:22px;border-radius:16px;background:' + panelBg +
      ';color:' + panelFg + ';border:1px solid ' + panelLine +
      ';box-shadow:0 30px 80px rgba(0,0,0,.4)}' +
    '.cm-dialog h3{margin:0 0 10px;font-size:16px}' +
    '.cm-dialog p{margin:0 0 18px;font-size:12.5px;line-height:1.85;opacity:.86}' +
    '.cm-dialog p b{font-weight:700;opacity:1}' +
    '.cm-ops{display:flex;gap:9px;justify-content:flex-end;flex-wrap:wrap}' +
    '.cm-ops button{padding:8px 15px;border-radius:9px;cursor:pointer;font-size:12.5px;' +
      'border:1px solid ' + panelLine + ';background:transparent;color:inherit}' +
    '.cm-ops button.go{border-color:transparent;background:#b65c33;color:#fff;font-weight:600}' +
    '@media(max-width:620px){.cm-switch>button{font-size:11px;padding:5px 9px}}';
  document.head.appendChild(style);

  // ---- 顶栏下拉 ----
  var wrap = document.createElement('div');
  wrap.className = 'cm-switch';
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = NAMES[current] + ' ▾';
  btn.title = '切换到其他功能（不必先回首页）';
  btn.setAttribute('aria-haspopup', 'true');
  var menu = document.createElement('div');
  menu.className = 'cm-switch-menu';
  menu.hidden = true;
  APPS.forEach(function (a) {
    var link = document.createElement('a');
    link.href = a.url;
    link.textContent = a.name;
    if (a.key === current) {
      link.className = 'on';
      link.setAttribute('aria-current', 'page');
    }
    menu.appendChild(link);
  });
  wrap.appendChild(btn);
  bar.insertBefore(wrap, bar.firstChild);
  // 菜单挂到 body：脱离顶栏/父容器，避免被 overflow:hidden|clip 裁剪或被 transform 改变定位基准
  document.body.appendChild(menu);

  // 按按钮实际位置定位，并把菜单完整夹在视口内（左右都不溢出、下方不够时翻到上方）
  function placeMenu() {
    var r = btn.getBoundingClientRect();
    var w = menu.offsetWidth || 172;
    var h = menu.offsetHeight || 180;
    var vw = window.innerWidth, vh = window.innerHeight;
    var left = Math.min(Math.max(8, r.left), Math.max(8, vw - w - 8));
    var top = r.bottom + 8;
    if (top + h > vh - 8) {
      var above = r.top - h - 8;
      top = above >= 8 ? above : Math.max(8, vh - h - 8);
    }
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
    menu.style.right = 'auto';
  }
  function closeMenu() { menu.hidden = true; }

  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    if (menu.hidden) { menu.hidden = false; placeMenu(); }
    else closeMenu();
  });
  document.addEventListener('click', function (e) {
    if (!wrap.contains(e.target) && !menu.contains(e.target)) closeMenu();
  });
  window.addEventListener('resize', function () { if (!menu.hidden) placeMenu(); });
  window.addEventListener('scroll', function () { if (!menu.hidden) closeMenu(); }, true);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenu(); });

  // ---- 上传状态 / 确认弹窗 ----
  function uploadActive() {
    try {
      return typeof window.__cmUploadActive === 'function' && !!window.__cmUploadActive();
    } catch (_) { return false; }
  }
  var overlay = document.createElement('div');
  overlay.className = 'cm-overlay';
  overlay.hidden = true;
  var dialog = document.createElement('div');
  dialog.className = 'cm-dialog';
  var h3 = document.createElement('h3');
  h3.textContent = '正在上传音乐';
  var p = document.createElement('p');
  var ops = document.createElement('div');
  ops.className = 'cm-ops';
  var stay = document.createElement('button');
  stay.type = 'button'; stay.textContent = '留在本页';
  var go = document.createElement('button');
  go.type = 'button'; go.className = 'go'; go.textContent = '在新标签页打开';
  ops.appendChild(stay); ops.appendChild(go);
  dialog.appendChild(h3); dialog.appendChild(p); dialog.appendChild(ops);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  var pendingUrl = '';
  function targetName(url) {
    var mm = String(url).match(/\/(radio|tv|music|work|device)\//);
    return mm ? NAMES[mm[1]] : '目标页面';
  }
  stay.addEventListener('click', function () { overlay.hidden = true; pendingUrl = ''; });
  go.addEventListener('click', function () {
    if (pendingUrl) window.open(pendingUrl, '_blank', 'noopener');
    overlay.hidden = true;
    pendingUrl = '';
  });
  overlay.addEventListener('click', function (e) {
    if (e.target === overlay) { overlay.hidden = true; pendingUrl = ''; }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !overlay.hidden) { overlay.hidden = true; pendingUrl = ''; }
  });

  // 上传中：拦截所有会离开本页的 <a>（含顶栏「门户」「状态」和切换器菜单）
  // 捕获阶段执行；确认后改为新标签页打开，本页继续上传。
  document.addEventListener('click', function (e) {
    if (!uploadActive()) return;
    var t = e.target;
    var a = t && t.closest ? t.closest('a[href]') : null;
    if (!a || a.target === '_blank' || a.hasAttribute('download')) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || /^(javascript|mailto|tel|data):/i.test(href)) return;
    e.preventDefault();
    e.stopPropagation();
    menu.hidden = true;
    pendingUrl = a.href;
    var name = targetName(pendingUrl);
    p.innerHTML = '切换到 <b></b> 会在新标签页打开，当前音乐页面保持不动，' +
      '<b>上传继续在后台进行</b>。只有关闭这个音乐页面或整个浏览器，上传才会停止。';
    p.querySelector('b').textContent = name;
    overlay.hidden = false;
  }, true);

  // 上传中关闭 / 刷新页面时给一次浏览器原生确认
  window.addEventListener('beforeunload', function (e) {
    if (!uploadActive()) return;
    e.preventDefault();
    e.returnValue = '';
  });
})();
