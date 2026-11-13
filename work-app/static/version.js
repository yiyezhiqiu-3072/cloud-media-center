/* 页面版本号 + 版本记录
 *
 * 用法（任意页面放一个节点即可）：
 *   <span class="ver-chip" id="appVersion"
 *         data-version-url="api/meta" data-version-label="云音乐"
 *         data-app-key="music" hidden></span>
 *   <script src="version.js"></script>
 *
 * - data-version-url   : 版本接口，默认 api/meta；门户页传 auth/health
 * - data-version-label : 悬浮/浮层里的服务名
 * - data-app-key       : 台账里的应用键 music/radio/tv/work/auth。
 *                        不传时按 label 自动推断（兼容未更新的旧页面）
 * - data-assets-url    : 可选，轮询静态文件指纹，变化时提示「点击刷新」；
 *                        只提示不强制刷新，避免打断播放 / 上传 / 编辑歌词
 *
 * 版本号来自服务端 /api/meta（各 app.py 里的 VERSION 常量）；
 * 「本次更新内容 + 发版时间（精确到秒）」来自本文件内嵌的 CM_RELEASES 台账，
 * 由 portal/bump_version.py 在每次发版时自动追加。点击版本号即可查看。
 * 台账为严格 JSON，改动母版 portal/version.js 后需同步到 4 个应用 static/。
 * 取不到版本（未登录 / 服务未就绪）时标签保持隐藏，不影响页面其它功能。
 */
(function () {
  'use strict';
  var el = document.getElementById('appVersion');
  if (!el) return;

  var label = el.getAttribute('data-version-label') || '服务';
  var LABEL_KEY = {
    '云音乐': 'music', '云收音机': 'radio', '云电视': 'tv',
    '工作日志': 'work', '登录服务': 'auth'
  };
  var appKey = el.getAttribute('data-app-key') || LABEL_KEY[label] || '';
  var url = el.getAttribute('data-version-url') || 'api/meta';
  var assetsUrl = el.getAttribute('data-assets-url');

  /* ---- 版本台账（严格 JSON，由 bump_version.py 维护，勿手工改格式） ---- */
  var CM_RELEASES = {
  "music": {
    "label": "云音乐",
    "releases": [
      {
        "v": "1.2.1",
        "at": "2026-11-13 19:43:55",
        "items": [
          "云音乐顶栏补上一键回首页按钮：与在线收音机 / 电视一致，采用房子图标 + 「首页」文字的胶囊按钮，与切换器 / 天气 / 版本号同排显示",
          "修复手机端看不到该入口的问题：旧 CSS 在 max-width:620px 把 .home-link 隐藏了，现改为常驻显示并压缩内边距；≤380px 只留房子图标，避免挤压顶栏",
          "纯前端改动，后端接口、电台时间轴与数据结构不变；只需替换 CloudMusic.exe，其余 4 个 exe 不用动"
        ]
      },
      {
        "v": "1.2.0",
        "at": "2026-11-11 22:49:30",
        "items": [
          "新增服务端电台时钟：后端按真实时间持续推进曲目与进度并持久化到 data/radio.json，无人在线时时间轴照常推进；任意页面打开自动接入当前曲目与当前秒数",
          "GET /api/radio 返回当前曲目、position、duration、队列与 server_time；曲库增删改或「随机重排」后自动重建队列，保持正在播放的曲目与已播放秒数",
          "全站接入：门户下 5 个应用页的共享播放条都接入同一电台；每 25 秒与服务端对表（漂移超过 3 秒才 seek），曲目结束按服务端时间轴接下一首",
          "支持临时自由播放：点歌 / 暂停 / 切歌 / 拖进度会转入个人模式并停止校准，播放条右上角「↻ 回到电台」可一键重新接入；接口不可用或曲库为空时回退原有 localStorage 状态接力",
          "音乐页跟随电台切歌同步曲名、歌词与队列高亮；电台模式下 ended 不再本地切歌，避免双重切歌；修复定时校准误判换源导致每 25 秒重新加载音频的问题"
        ]
      },
      {
        "v": "1.1.3",
        "at": "2026-11-08 00:06:12",
        "items": [
          "修复播放条加时间行后页面底部（导出按钮等）被遮挡：改为实测播放条高度写入 --cm-bar-h，页面留白/浮层/提示条自动跟随",
          "横竖屏切换、字体缩放、内容变化都会重新测量，不再用固定像素估算"
        ]
      },
      {
        "v": "1.1.2",
        "at": "2026-11-07 23:50:52",
        "items": [
          "共享播放条新增进度时间行：左侧已播放时间、右侧剩余时间（-M:SS）、中间圆角进度条",
          "修复进度刷新被写盘节流挡住（原来每 1.5 秒才跳一次）；电台直播显示「直播」，时长未知显示 --:--",
          "同步加大页面底部留白、浮层与提示条的偏移，避免加行后遮挡列表"
        ]
      },
      {
        "v": "1.1.1",
        "at": "2026-11-07 23:33:31",
        "items": [
          "修复点共享播放条的歌曲名/封面不再弹出播放页：音乐页直接展开，从其它应用点则自动跳转并展开",
          "修复沉浸式播放页与「⚙」浮出的原生播放台重叠：打开播放页时强制收起原生面板并隐藏共享条"
        ]
      },
      {
        "v": "1.1.0",
        "at": "2026-11-07 22:27:42",
        "items": [
          "新增服务端自动转码：ffmpeg 后台把 wav/flac/aiff/caf/mp4 转成通用 mp3，历史曲目启动自动补转",
          "修复上传的 wav 无法播放：按扩展名规范化 MIME，并加固 Range 分块传输",
          "新增跨应用常驻播放条：切到工作表仍保留最后播放状态，可切歌 / 切电台",
          "桌面端与手机端统一为一条播放条，⚙ 可浮出歌词 / EPG 等原生面板",
          "随机重排：每次点击重写全部曲目的上传时间并随机排序",
          "顶栏窄屏改两排；上传区默认收缩，上传时自动收起只显示进度",
          "修复手机端播放台定位、横向溢出与上传收起后的空白",
          "版本号支持点击查看本次更新内容与发版时间"
        ]
      }
    ]
  },
  "radio": {
    "label": "云收音机",
    "releases": [
      {
        "v": "1.5.12",
        "at": "2026-11-11 22:49:30",
        "items": [
          "共享常驻播放条接入服务端音乐电台（全站同听）；切到收音机直播时自动转入个人模式，电台校准不会抢回直播流"
        ]
      },
      {
        "v": "1.5.11",
        "at": "2026-11-08 00:06:12",
        "items": [
          "修复播放条加时间行后页面底部（导出按钮等）被遮挡：改为实测播放条高度写入 --cm-bar-h，页面留白/浮层/提示条自动跟随",
          "横竖屏切换、字体缩放、内容变化都会重新测量，不再用固定像素估算"
        ]
      },
      {
        "v": "1.5.10",
        "at": "2026-11-07 23:50:52",
        "items": [
          "共享播放条新增进度时间行：左侧已播放时间、右侧剩余时间（-M:SS）、中间圆角进度条",
          "修复进度刷新被写盘节流挡住（原来每 1.5 秒才跳一次）；电台直播显示「直播」，时长未知显示 --:--",
          "同步加大页面底部留白、浮层与提示条的偏移，避免加行后遮挡列表"
        ]
      },
      {
        "v": "1.5.9",
        "at": "2026-11-07 23:33:31",
        "items": [
          "共享播放条：点曲名/封面可展开音乐播放页（跨页自动展开），并修复播放页与原生面板重叠"
        ]
      },
      {
        "v": "1.5.8",
        "at": "2026-11-07 22:27:42",
        "items": [
          "新增跨应用常驻播放条：点电台自动播放，切到工作表仍保留播放状态并可切换电台",
          "桌面端与手机端统一为一条播放条，⚙ 可浮出 EPG / 收藏 / 线路 / 音量 / 定时",
          "修复切换应用下拉菜单溢出屏幕、遮挡搜索框",
          "版本号支持点击查看本次更新内容与发版时间"
        ]
      }
    ]
  },
  "tv": {
    "label": "云电视",
    "releases": [
      {
        "v": "1.3.10",
        "at": "2026-11-11 22:49:30",
        "items": [
          "共享常驻播放条接入服务端音乐电台，打开页面自动接入当前曲目与进度"
        ]
      },
      {
        "v": "1.3.9",
        "at": "2026-11-08 00:06:12",
        "items": [
          "修复播放条加时间行后页面底部（导出按钮等）被遮挡：改为实测播放条高度写入 --cm-bar-h，页面留白/浮层/提示条自动跟随",
          "横竖屏切换、字体缩放、内容变化都会重新测量，不再用固定像素估算"
        ]
      },
      {
        "v": "1.3.8",
        "at": "2026-11-07 23:50:52",
        "items": [
          "共享播放条新增进度时间行：左侧已播放时间、右侧剩余时间（-M:SS）、中间圆角进度条",
          "修复进度刷新被写盘节流挡住（原来每 1.5 秒才跳一次）；电台直播显示「直播」，时长未知显示 --:--",
          "同步加大页面底部留白、浮层与提示条的偏移，避免加行后遮挡列表"
        ]
      },
      {
        "v": "1.3.7",
        "at": "2026-11-07 23:33:31",
        "items": [
          "共享播放条：点曲名/封面可展开音乐播放页（跨页自动展开），并修复播放页与原生面板重叠"
        ]
      },
      {
        "v": "1.3.6",
        "at": "2026-11-07 22:27:42",
        "items": [
          "新增跨应用常驻播放条：电视页显示最后一次播放状态",
          "桌面端与手机端统一为一条播放条",
          "修复切换应用下拉菜单溢出遮挡",
          "版本号支持点击查看本次更新内容与发版时间"
        ]
      }
    ]
  },
  "work": {
    "label": "工作日志",
    "releases": [
      {
        "v": "1.1.7",
        "at": "2026-11-11 22:49:30",
        "items": [
          "共享常驻播放条接入服务端音乐电台，打开页面自动接入当前曲目与进度"
        ]
      },
      {
        "v": "1.1.6",
        "at": "2026-11-10 22:43:09",
        "items": [
          "「当日事项」面板头新增常驻的「＋ 新增一条」按钮：即使正在编辑某条记录，也能一键清空表单回到新增态，解决界面上只有「保存修改 / 取消编辑」、看不到新增入口的问题",
          "表单头编辑态按钮由「取消编辑」改为「＋ 新增一条」，与列表头入口共用同一动作；两者都会清空标题 / 内容 / 结果 / 照片、状态复位为「将要发生」、日期保持当前查看的这一天",
          "新增时自动聚焦并平滑滚动到标题输入框，手机端也能立刻开始输入；保存成功后仍自动回到新增态，可连续补记当天多件事",
          "新增按钮使用主题强调色与列表头统计条并排，窄屏自动换行不挤压；纯前端改动，后端接口与数据结构不变"
        ]
      },
      {
        "v": "1.1.5",
        "at": "2026-11-10 20:15:36",
        "items": [
          "甘特图改为全屏独立层：点「查看甘特图」直接整屏显示并从顶部开始，不再依赖 hidden 隐藏记录页或页面滚动，彻底修复「点了不跳转 / 表单与甘特图并存」",
          "甘特图支持多天计划回顾：默认近 7 天，可切今天 / 近 30 天 / 本月 / 全部，或自定义起止日期",
          "按天分组展示每天的手机时间条与桌面表格，统一时间轴便于横向比较；范围汇总显示有记录天数、总件数、各状态与总时长",
          "后端 /api/items 与 /api/export 新增 from/to/all 范围查询（含端点、非法日期安全忽略），单日查询与导出保持兼容",
          "甘特图内可直接导出所选范围的 CSV / JSON；点色块仍可返回记录页编辑该事项"
        ]
      },
      {
        "v": "1.1.4",
        "at": "2026-11-08 11:08:59",
        "items": [
          "修复点「查看甘特图」仍停留在记录页：.layout 的 display:grid 覆盖了 [hidden]，记录页从未真正隐藏，甘特图被挤在下方",
          "补全局 [hidden]{display:none!important} 规则，并在 setView 里用 inline display 做双保险；切换后自动回到顶部才真正生效"
        ]
      },
      {
        "v": "1.1.3",
        "at": "2026-11-08 00:06:12",
        "items": [
          "修复播放条加时间行后页面底部（导出按钮等）被遮挡：改为实测播放条高度写入 --cm-bar-h，页面留白/浮层/提示条自动跟随",
          "横竖屏切换、字体缩放、内容变化都会重新测量，不再用固定像素估算",
          "点「查看甘特图」自动定位到甘特图顶部，不再停在页面中间；切回记录页同样回到顶部",
          "手机端压缩甘特图顶部间距，时间条卡片首屏即可见；导出按钮行增加底部保险间距"
        ]
      },
      {
        "v": "1.1.2",
        "at": "2026-11-07 23:50:52",
        "items": [
          "共享播放条新增进度时间行：左侧已播放时间、右侧剩余时间（-M:SS）、中间圆角进度条",
          "修复进度刷新被写盘节流挡住（原来每 1.5 秒才跳一次）；电台直播显示「直播」，时长未知显示 --:--",
          "同步加大页面底部留白、浮层与提示条的偏移，避免加行后遮挡列表"
        ]
      },
      {
        "v": "1.1.1",
        "at": "2026-11-07 23:33:31",
        "items": [
          "修复手机甘特图色块内时间被裁成「08:1 / 23:3」：完整起止时间移到独立的跟随标签，不再被 overflow 裁切",
          "色块改为纯色 + 白色描边更醒目；时间刻度由 3 个加密到 5 个；顶部状态与时间文字加深加粗"
        ]
      },
      {
        "v": "1.1.0",
        "at": "2026-11-07 23:07:30",
        "items": [
          "标题提示改简练，标题下方直接写「记录内容」，不用再往下翻找输入框",
          "当日事项卡片重做：时间/状态/标题/内容/结果/照片分区展示，消除窄列挤压与字号混乱",
          "甘特图新增手机端时间条视图（整行宽度、无需左右拖动），时间窗口按当天事项自适应；可切换表格",
          "新增相机拍照记录：照片自动叠加「时间（精确到秒）+ 地点」水印，支持定位、手填地点、地点记忆、相册选取与大图查看",
          "后端新增照片上传/读取接口与事项照片字段，编辑或删除事项自动清理照片，CSV 导出含水印信息"
        ]
      },
      {
        "v": "1.0.1",
        "at": "2026-11-07 22:27:42",
        "items": [
          "新增跨应用常驻播放条：保留最后播放状态，音乐可切歌、收音机可切台",
          "桌面端与手机端统一为一条播放条",
          "修复切换应用下拉菜单溢出遮挡",
          "版本号支持点击查看本次更新内容与发版时间"
        ]
      }
    ]
  },
  "auth": {
    "label": "登录服务",
    "releases": [
      {
        "v": "1.1.3",
        "at": "2026-11-07 22:27:42",
        "items": [
          "前端：版本号支持点击查看各应用的更新内容与发版时间"
        ]
      }
    ]
  },
  "device": {
    "label": "客户分切设备管理",
    "releases": [
      {
        "v": "1.0.1",
        "at": "2026-11-11 22:49:30",
        "items": [
          "修复手机系统返回键在录入表单里直接退出应用：视图切换由 replaceState 改为 pushState，返回键改为逐级返回上级视图",
          "退到主页后 2.5 秒内再按一次返回才明确退出到门户，并提示「已是主页 · 再按一次返回键退出」，避免录单时误触退出",
          "共享常驻播放条接入服务端音乐电台，打开页面自动接入当前曲目与进度"
        ]
      },
      {
        "v": "1.0.0",
        "at": "2026-11-10 21:41:41",
        "items": [
          "新增「客户分切设备管理」应用：门户第 5 个入口，独立后端 CloudDevice.exe（8095）与 /device/ 反代",
          "复刻功能截图主页宫格：工厂基础数据 / 设备基础数据 / 视觉相关 / 视觉相关序时簿",
          "8 类基础数据字典维护（基地、车间、机台、相机型号、软件版本、加密狗版本、加密狗安装方式、工控机CPU）",
          "5 类业务单据（分切机基本信息表、F品视觉防呆、软件与工控机、卷芯居中、设备维护表）：按字段模板渲染，支持下拉/日期/数字/开关/文本域/照片",
          "单据保存与审核（制单人/审核人、精确到秒的时间）、照片上传压缩与预览、序时簿筛选与 CSV 导出、分切机总览",
          "共享切换器与常驻播放条接入第 5 个应用，门户首页与运行状态页增加对应状态检测"
        ]
      }
    ]
  }
};
  /* ---- 台账结束 ---- */

  var css =
    '.ver-chip{display:inline-flex;align-items:center;white-space:nowrap;' +
      'font-size:11px;line-height:1.5;color:inherit;opacity:.6;cursor:pointer;' +
      'padding:2px 8px;border-radius:99px;border:1px solid rgba(127,127,127,.32);' +
      'background:rgba(127,127,127,.07);font-variant-numeric:tabular-nums;user-select:all}' +
    '.ver-chip[hidden]{display:none}' +
    '.ver-chip:hover{opacity:.95}' +
    '.ver-chip:focus-visible{outline:2px solid #e0a458;outline-offset:2px}' +
    '@media (max-width:620px){.ver-chip{font-size:10px;padding:1px 6px}}' +
    '#cmVerMask{position:fixed;top:0;left:0;right:0;bottom:0;z-index:10000;' +
      'background:rgba(0,0,0,.55);display:none}' +
    '#cmVerMask.on{display:block}' +
    '#cmVerPanel{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);' +
      'width:430px;max-width:calc(100vw - 24px);max-height:74vh;overflow-y:auto;' +
      'box-sizing:border-box;z-index:10001;display:none;padding:18px 18px 14px;' +
      'border-radius:16px;background:#211b15;color:#f3ece3;' +
      'border:1px solid rgba(255,255,255,.13);box-shadow:0 18px 50px rgba(0,0,0,.55);' +
      'font-size:13px;line-height:1.65;-webkit-overflow-scrolling:touch}' +
    '#cmVerPanel.on{display:block}' +
    '#cmVerPanel .cmv-head{display:flex;align-items:flex-start;gap:10px;' +
      'padding-bottom:12px;border-bottom:1px solid rgba(255,255,255,.12)}' +
    '#cmVerPanel .cmv-head>div{flex:1;min-width:0}' +
    '#cmVerPanel .cmv-label{font-size:14px;font-weight:700}' +
    '#cmVerPanel .cmv-sub{font-size:11.5px;color:#b9ad9f;margin-top:2px}' +
    '#cmVerPanel .cmv-close{flex:0 0 auto;width:28px;height:28px;border:none;cursor:pointer;' +
      'border-radius:8px;background:rgba(255,255,255,.08);color:inherit;font-size:18px;line-height:1}' +
    '#cmVerPanel .cmv-close:hover{background:rgba(255,255,255,.16)}' +
    '#cmVerPanel .cmv-sec{margin-top:14px}' +
    '#cmVerPanel .cmv-badge{display:inline-block;padding:1px 8px;border-radius:99px;' +
      'background:#e0a458;color:#1b1109;font-size:11px;font-weight:700;margin-left:6px;' +
      'vertical-align:1px;font-variant-numeric:tabular-nums}' +
    '#cmVerPanel .cmv-time{font-size:12px;color:#e0a458;margin:4px 0 8px;' +
      'font-variant-numeric:tabular-nums;letter-spacing:.2px}' +
    '#cmVerPanel .cmv-items{margin:0;padding-left:20px}' +
    '#cmVerPanel .cmv-items li{margin:4px 0;word-break:break-word}' +
    '#cmVerPanel .cmv-warn{margin-top:10px;padding:8px 10px;border-radius:9px;' +
      'background:rgba(224,164,88,.12);color:#f0c98a;font-size:11.5px}' +
    '#cmVerPanel .cmv-empty{color:#b9ad9f;font-size:12.5px;padding:8px 0}' +
    '#cmVerPanel .cmv-hist{margin-top:16px}' +
    '#cmVerPanel .cmv-hist h4{margin:0 0 6px;font-size:12px;color:#b9ad9f;font-weight:600}' +
    '#cmVerPanel details{border-top:1px solid rgba(255,255,255,.09);padding:7px 0}' +
    '#cmVerPanel summary{cursor:pointer;font-size:12px;color:#ded3c5;' +
      'font-variant-numeric:tabular-nums}' +
    '#cmVerPanel details .cmv-items{margin-top:6px;font-size:12px;color:#cfc3b5}' +
    '#cmVerPanel .cmv-foot{margin-top:14px;padding-top:10px;font-size:11px;' +
      'color:#9d9286;border-top:1px solid rgba(255,255,255,.09)}';
  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  var curVersion = '';
  var mask = null, panel = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function itemsHtml(items) {
    if (!items || !items.length) return '<div class="cmv-empty">（无详细条目）</div>';
    return '<ul class="cmv-items">' + items.map(function (x) {
      return '<li>' + esc(x) + '</li>';
    }).join('') + '</ul>';
  }
  function ensurePanel() {
    if (panel) return;
    mask = document.createElement('div');
    mask.id = 'cmVerMask';
    panel = document.createElement('div');
    panel.id = 'cmVerPanel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    document.body.appendChild(mask);
    document.body.appendChild(panel);
    mask.addEventListener('click', closePanel);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.keyCode === 27) closePanel();
    });
    panel.addEventListener('click', function (e) {
      var cn = e.target && e.target.className;
      if (typeof cn === 'string' && cn.indexOf('cmv-close') >= 0) closePanel();
    });
  }
  function closePanel() {
    if (mask) mask.classList.remove('on');
    if (panel) panel.classList.remove('on');
  }
  function panelHtml() {
    var app = CM_RELEASES[appKey] || null;
    var rels = (app && app.releases) || [];
    var idx = -1, i;
    for (i = 0; i < rels.length; i++) {
      if (rels[i] && rels[i].v === curVersion) { idx = i; break; }
    }
    var mismatch = !!(curVersion && rels.length && idx < 0);
    var mainIdx = idx >= 0 ? idx : 0;
    var main = rels[mainIdx];
    var head =
      '<div class="cmv-head"><div>' +
        '<div class="cmv-label">' + esc(label) + ' · 版本记录</div>' +
        '<div class="cmv-sub">服务端版本 ' +
          (curVersion ? 'v' + esc(curVersion) : '读取中…') + '</div>' +
      '</div><button class="cmv-close" type="button" aria-label="关闭">×</button></div>';

    var body;
    if (!main) {
      body = '<div class="cmv-sec"><div class="cmv-empty">该应用暂无版本更新记录。</div></div>';
    } else {
      body = '<div class="cmv-sec"><div>' +
        (mismatch ? '最近记录' : '本次更新') +
        '<span class="cmv-badge">v' + esc(main.v) + '</span></div>' +
        '<div class="cmv-time">' + esc(main.at) + '</div>' +
        itemsHtml(main.items) + '</div>';
    }
    if (mismatch) {
      body += '<div class="cmv-warn">服务端版本 v' + esc(curVersion) +
        ' 与本地记录不一致：可能刚换了 exe 但浏览器仍用旧页面，请硬刷新（清缓存）；' +
        '若仍不一致，说明该版本漏了登记。</div>';
    }

    var hist = rels.filter(function (r, n) { return n !== mainIdx && r && r.v; });
    if (hist.length) {
      var h = '<div class="cmv-hist"><h4>历史版本（' + hist.length + '）</h4>';
      hist.forEach(function (r) {
        h += '<details><summary>v' + esc(r.v) + ' · ' + esc(r.at) + ' · ' +
          ((r.items || []).length) + ' 项</summary>' + itemsHtml(r.items) + '</details>';
      });
      body += h + '</div>';
    }
    var foot = '<div class="cmv-foot">版本号来自服务端 ' + esc(url) +
      '；更新内容与发版时间随前端文件热更新。点击空白处或按 Esc 关闭。</div>';
    return head + body + foot;
  }
  function openPanel() {
    ensurePanel();
    panel.innerHTML = panelHtml();
    mask.classList.add('on');
    panel.classList.add('on');
  }

  el.setAttribute('role', 'button');
  el.setAttribute('tabindex', '0');
  el.addEventListener('click', openPanel);
  el.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ' || e.keyCode === 13 || e.keyCode === 32) {
      e.preventDefault(); openPanel();
    }
  });

  fetch(url, { cache: 'no-store', credentials: 'same-origin' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      var v = d && d.version;
      if (typeof v !== 'string' || !v) return;   // 拿不到就保持隐藏
      curVersion = v;
      el.textContent = 'v' + v;
      el.title = '点击查看 ' + label + ' v' + v + ' 的更新内容与发版时间';
      el.setAttribute('aria-label', el.title);
      el.hidden = false;
    })
    .catch(function () { /* 服务未就绪时静默 */ });

  /* ---- 热更新感知 ---- */
  if (!assetsUrl) return;
  var baseline = null;
  function showUpdateTip() {
    if (document.getElementById('cmUpdateTip')) return;
    var tip = document.createElement('div');
    tip.id = 'cmUpdateTip';
    tip.textContent = '页面已更新 · 点击刷新';
    tip.title = '服务器上的前端文件已变化，刷新后生效';
    tip.style.cssText = 'position:fixed;right:16px;bottom:100px;z-index:9999;cursor:pointer;' +
      'background:#e0a458;color:#1b1109;padding:10px 16px;border-radius:99px;' +
      'font-size:12px;font-weight:600;box-shadow:0 10px 28px rgba(0,0,0,.4)';
    tip.onclick = function () { window.location.reload(); };
    document.body.appendChild(tip);
  }
  function pollAssets() {
    fetch(assetsUrl, { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.fingerprint) return;
        if (baseline === null) { baseline = d.fingerprint; return; }
        if (d.fingerprint !== baseline) { baseline = d.fingerprint; showUpdateTip(); }
      })
      .catch(function () { /* 服务未就绪时静默 */ });
  }
  pollAssets();
  setInterval(pollAssets, 30000);
})();
