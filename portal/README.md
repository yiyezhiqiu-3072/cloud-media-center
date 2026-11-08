# 云媒中心 · 统一入口（合并到现有 nginx）

门户页 + 自定义登录页 + 两个后端反向代理，整合进服务器上已有的 `nginx.conf`。
原来的两个站点（8080 的 `html`、8081 的 `html2`）**一行未改**。

**登录体验**：打开首页不需要登录 → 点「进入收听 / 进入观看」弹出自定义登录页
（左侧当前位置天气，右侧用户名 / 密码 / 登录 / 取消）→ 登录成功后进入对应页面。
不再使用浏览器自带的 Basic Auth 弹框。

```
浏览器 ─► nginx :80 ─┬─ /            门户首页（公开）
                     ├─ /login.html  自定义登录页（公开）
                     ├─ /auth/    → 127.0.0.1:8092  CloudAuth 统一登录
                     ├─ /radio/   → 127.0.0.1:8090  云收音机
                     ├─ /tv/      → 127.0.0.1:8091  云电视
                     ├─ /music/   → 127.0.0.1:8093  云音乐（上传+播放列表）
                     ├─ /work/    → 127.0.0.1:8094  工作日志（记录+甘特图）
                     └─ /status   运行状态页

          nginx :8080 → html       【原有站点，未改动】
          nginx :8081 → html2      【原有站点，未改动】
```

## 一、端口怎么分的

关键点：**你原来的 nginx 已经占用了 8080 和 8081**，所以两个程序不能用默认端口。

| 服务 | 监听 | 谁在用 |
| --- | --- | --- |
| 门户 + 反代 | nginx `0.0.0.0:80` | 对外唯一入口 |
| 云收音机 | `127.0.0.1:8090` | 仅本机，由 nginx 转发 |
| 云电视 | `127.0.0.1:8091` | 仅本机，由 nginx 转发 |
| 云音乐 | `127.0.0.1:8093` | 仅本机，由 nginx 转发 |
| 工作日志 | `127.0.0.1:8094` | 仅本机，由 nginx 转发 |
| **统一登录 CloudAuth** | `127.0.0.1:8092` | **本次新增**，仅本机 |
| 原有站点一 | nginx `0.0.0.0:8080` | 不变 |
| 原有站点二 | nginx `0.0.0.0:8081` | 不变 |

## 二、目录结构

```
nginx/
├─ conf/
│  ├─ nginx.conf          ← 用本目录的 nginx.conf 内容替换
│  ├─ htpasswd            ← 账号密码，双击 make-htpasswd.bat 生成
│  ├─ make-htpasswd.bat   ← 生成/修改登录账号，双击运行
│  ├─ make-htpasswd.ps1   ← 上面那个 bat 调用的脚本，必须放同一目录
│  └─ check-nginx.bat     ← 登录不了/502 时双击，一次列出所有关键信息
├─ html/
│  └─ index.html          【原有】8080 站点
├─ html2/
│  └─ index.html          【原有】8081 站点
└─ html_portal/           ← 门户目录
   ├─ index.html          ← 门户页
   ├─ login.html          ← 【本次新增】自定义登录页
   ├─ status.html         ← 运行状态监控页
   └─ version.js          ← 各页面版本号组件（门户页共用，缺了页脚版本号不显示）

CloudAuth/                ← 【本次新增】放到任意目录，例如 C:\CloudAuth\
   ├─ CloudAuth.exe
   ├─ config.json         ← htpasswd 路径、端口、会话天数
   ├─ start.bat           ← 双击启动
   ├─ data\secret.key     ← 首次启动自动生成，会话签名密钥
   └─ logs\               ← 登录日志
```

门户页**不能**放进 `html/`，那是 8080 站点的根目录，会和原有站点抢首页。

## 三、五步上线

**1. 建目录、放门户页和登录页**

```bat
cd C:\nginx
mkdir html_portal
copy C:\path\to\portal\index.html  html_portal\index.html
copy C:\path\to\portal\login.html  html_portal\login.html
copy C:\path\to\portal\status.html html_portal\status.html
copy C:\path\to\portal\version.js html_portal\version.js
```

**2. 生成账号密码**

把 `make-htpasswd.bat` 和 `make-htpasswd.ps1` **一起**放到 `C:\nginx\conf\` 下，双击 `make-htpasswd.bat`，按提示输入用户名和密码。

- 密码留空直接回车 → 自动生成 12 位随机密码，并显示在窗口里
- 同一个用户名再跑一次 = 改密码；换个用户名 = 加账号
- **不需要安装 Python**，只用 Windows 自带的 powershell
- 账号文件仍是 `C:\nginx\conf\htpasswd`，格式仍是 `{SHA}`，**用法完全没变**；
  区别只是现在由 CloudAuth 读取校验，不再由 nginx 的 `auth_basic` 读取

```
admin:{SHA}bFWAPW8dehd6DbPrSzQ7DVD5wRE=
```

**3. 放好统一登录服务**

把 `portal\auth-service\dist\` 整个目录复制到服务器（例如 `C:\CloudAuth\`），
编辑里面的 `config.json`，确认 `htpasswd` 指向你的账号文件：

```json
{
  "host": "127.0.0.1",
  "port": 8092,
  "htpasswd": "C:/nginx/conf/htpasswd",
  "session_days": 7
}
```

双击 `start.bat` 启动。登录日志写到 `logs\auth-YYYY-MM-DD.txt`，
`data\secret.key` 首次启动自动生成（删掉它 = 所有人重新登录）。

**4. 替换 nginx.conf 并升级后端**

用本目录 `nginx.conf` 整体替换 `C:\nginx\conf\nginx.conf`（里面已包含原来的两个站点），
并把服务器上的 `CloudRadio.exe`、`CloudTV.exe`、`CloudMusic.exe` 换成新版，
新增的 **`CloudWork.exe`**（工作日志，v1.0.0）和新的 **`CloudAuth.exe`**（v1.1.3）放到
与它们相同的目录（如 `C:\CloudMedia\`）—— 新版会校验门户签发的会话 Cookie，
伪造 Cookie 进不去；CloudAuth 这次多了 8094 探活与 `/work` 跳转白名单，务必一起换。

各后端的 `config.json` 保持原样即可，新增的 `portal_auth_url` 留空会用默认值
`http://127.0.0.1:8092/auth/check`：

```json
{ "host": "127.0.0.1", "port": 8090, "trust_proxy_auth": true }   // 收音机
{ "host": "127.0.0.1", "port": 8091 }                            // 电视
{ "host": "127.0.0.1", "port": 8093, "trust_proxy_auth": true }  // 云音乐
{ "host": "127.0.0.1", "port": 8094, "trust_proxy_auth": true }  // 工作日志
```

工作日志的数据在 `CloudWork.exe` 同级的 `data\items.json`，纯 JSON、可直接备份。

云音乐的上传走 base64 + JSON，单首上限由 `config.json` 的 `max_upload_mb` 控制（默认 200MB），
nginx 侧已把 `/music/` 的 `client_max_body_size` 放宽到 280m（上传走 base64，请求体会膨胀约 4/3，
200MB 的文件约 267MB 请求体，必须留余量，否则会在 nginx 层直接 413）。
上传时页面会显示实时进度（读取文件 → 网络上传 → 服务端写入），支持多文件排队和随时取消。

- `host` 必须是 `127.0.0.1`：只接受本机连接，外网无法绕过 nginx 直连
- 独立运行（不挂 nginx、没有 `X-Forwarded-Prefix`）时，两个程序仍沿用原有逻辑，
  完全不依赖 CloudAuth

**云音乐的文件存在哪里**

上传的音频按「内部 ID + 扩展名」存盘，默认在 exe 同级的 `data\uploads\`：

```text
C:\CloudMedia\
├─ CloudMusic.exe
├─ config.json
└─ data\
   ├─ uploads\            ← 音乐文件本体（如 a1b2c3d4e5f60718.mp3）
   ├─ tracks.json         ← 曲目元数据（标题 / 歌手 / 时长 / 大小）
   ├─ playlists.json      ← 播放列表
   └─ sessions.json       ← 登录会话
```

启动日志会打印 `音乐文件保存目录：...`，页面上传区也直接显示这个路径（登录后才可见）。
磁盘上不保留原始文件名（避免重名与路径问题），原始标题和歌手记在 `tracks.json`。

**改到别的盘 / 目录**：在 `config.json` 加 `data_dir`，重启 CloudMusic.exe 生效：

```json
{ "host": "127.0.0.1", "port": 8093, "trust_proxy_auth": true, "data_dir": "D:\\CloudMedia\\music" }
```

- 绝对路径直接生效；相对路径按 exe 所在目录解析（`"storage"` → `C:\CloudMedia\storage\`）
- 音乐写到 `<data_dir>\uploads\`，三个 json 也一起移到 `<data_dir>\` 下
- 备份 / 迁移：停掉 exe，整体复制 `data` 目录（或 `data_dir` 指向的目录）即可；
  换机后把 `data_dir` 指过去，曲目与播放列表原样恢复
- 删除曲目会同时删磁盘文件和播放列表引用；直接手工删 `uploads` 里的文件会留下坏条目

**5. 检查并重载**

```bat
cd C:\nginx
nginx -t
nginx -s reload
:: 或者直接双击 restart-nginx.bat all（会同时重启三个后端）
```

想开机自启，把 `CloudAuth.exe` 也按另外两个程序的方式注册成服务（NSSM 等）。

## 四、登录是怎么校验的

```
点卡片 → 弹出 login.html（iframe）
   │
   ├─ 左侧：按访问者公网 IP 取天气，数据源**全部在国内**
   │        （太平洋IP库 / ipip.net 定位 → 中国天气网实况与今日高低温）
   │        由 CloudAuth 的 /auth/weather 代理；取不到就显示「天气信息暂不可用」
   └─ 右侧：用户名 / 密码 / 登录 / 取消（没有修改密码入口）
              │
              ▼ POST /auth/login
        CloudAuth 读 htpasswd 校验 {SHA}
              │ 通过
              ▼ Set-Cookie: cm_session=<HMAC签名>; HttpOnly; SameSite=Lax
        浏览器进入 /radio/ 或 /tv/
              │
              ▼ nginx 转发（Cookie 一起带上）
        后端调用 /auth/check 验证签名、有效期、账号是否仍存在
              │ 通过 → 正常播放
              └ 失败 → 跳回 /login.html?next=...
```

**安全要点**：
- Cookie 是 HMAC-SHA256 签名的，伪造无效（已实测）
- 签名里包含账号文件里的密码散列，**改密码后所有旧会话立即失效**
- 登录失败 5 次锁定该 IP 60 秒，日志有记录；**锁定期内输入正确密码仍可登录**（v1.1.2 起限流只挡错误尝试，不会误伤同出口 IP 下密码正确的人）
- nginx 层用「有无 Cookie」做快速跳转；真正的校验在后端，两层都过不了伪造 Cookie
- 会话默认 7 天（`session_days` 可改），首页页脚有「退出登录」

**本机诊断接口（v1.1.2+）**：在服务器本机浏览器或命令行访问
`http://127.0.0.1:8092/auth/diag`，可看到版本号、运行时间、当前线程数、空闲连接超时、
登录失败记录与活跃锁定数、天气缓存数、账号文件路径/大小/修改时间、账号数、密钥指纹。
该接口**只允许服务器本机访问**（nginx 反代会带 `X-Real-IP`，外部 IP 一律 403），
排查登录问题时不用再重启服务看现象。

## 四点五、天气数据源（全部国内）

登录页左侧、收音机与电视右上角的天气，**不使用任何境外接口**，全部走国内：

| 步骤 | 数据源 | 说明 |
| --- | --- | --- |
| 1. 定位 | `whois.pconline.com.cn`（太平洋 IP 库），失败回退 `myip.ipip.net` | 按 nginx 传来的 `X-Real-IP` 得到中文省市 |
| 2. 城市 ID | `toy1.weather.com.cn` | 中国天气网城市检索，取 9 位城市编码 |
| 3. 实况 | `d1.weather.com.cn/sk_2d/` | 温度、湿度、风速、中文天气描述 |
| 4. 今日 | `d1.weather.com.cn/dingzhi/` | 最高 / 最低温 |

**为什么走服务端**：中国天气网 `d1` 接口校验 `Referer` 必须是 `www.weather.com.cn`，
浏览器无法伪造，前端直连会 403；由 CloudAuth 代取后前端只请求同源 `/auth/weather`，
也顺带避开了跨域问题。结果按访客 IP 缓存 10 分钟（`weather_ttl` 可调），
`?refresh=1` 可强制刷新（点天气组件即触发）。

配置开关在 `CloudAuth\config.json`：

```json
{ "weather_enabled": true, "weather_ttl": 600 }
```

设 `weather_enabled: false` 可完全关掉天气请求，页面会显示占位。
天气数据仅用于展示，获取失败**不影响**登录、播放等任何功能。

> 独立运行（不挂 nginx、没有 CloudAuth）时天气会显示占位，这是预期行为。

## 四点六、屏幕常亮

进入收音机 / 电视播放界面后**默认自动保持屏幕常亮**，避免手机自动息屏、锁屏或桌面进入屏保。

| 环境 | 生效方式 |
| --- | --- |
| HTTPS / localhost | Screen Wake Lock API（系统级，最省电可靠） |
| `http://IP`（当前部署） | 隐藏的静音循环视频兜底（NoSleep.js 方案，MIT 许可），浏览器播放视频时会阻止息屏 |

- 播放器上的**太阳按钮**是开关：点亮 = 已生效，点一下可关闭以省电，偏好存浏览器本地
- 鼠标悬浮会显示当前模式：`系统级常亮` / `兼容模式常亮` / `浏览器不支持`
- 页面切到后台再回来、或系统因省电释放锁时，会**自动重新申请**
- 拿到锁失败（低电量 / 省电模式 / 浏览器限制）时不影响播放，只是屏幕可能仍会熄灭

> Wake Lock API 只在高安全上下文可用，所以当前 HTTP 访问走兼容模式。
> 将来上了 HTTPS，会自动切换到系统级方案，无需改代码。

## 四点七、云音乐播放器

**播放页面**：音乐库 / 播放列表里点**歌曲名称只开始播放，不会跳转**；想看全屏播放页，
点**底部播放条上的歌曲名**（悬浮会变金色，提示「点击查看播放页」），也可点播放条封面或展开按钮。
`Esc` 或左上角收起按钮返回列表，音乐继续播放。播放页含大封面、进度条、播放控制、循环 / 随机、
码率选择、歌词与播放队列（右上角队列按钮切换）。

**自动滚动歌词**：播放页点「添加 / 编辑」，可粘贴标准 LRC 文本或直接选 `.lrc` 文件，服务端持久化到
`tracks.json`。播放时按时间轴自动高亮当前句并平滑滚动居中，点某句歌词可跳到对应时间；「清除」可移除。
支持一行多个时间标签、`[offset:±毫秒]` 整体校准、元数据行自动忽略。

| 模式 | 行为 |
| --- | --- |
| 顺序播放 | 按列表依次播放，播完即停 |
| 列表循环 | 播完最后一首回到第一首，无限轮询 |
| 单曲循环 | 反复播放当前曲目；手动点「下一首」仍可跳过 |
| 随机轮询 | 整队洗牌后**依次播放，一轮内不重复不漏播**；播完自动重新洗牌再轮一轮 |

循环按钮在「关 → 列表循环 → 单曲循环」三态间轮转，单曲循环时按钮带 `1` 角标。
右上角队列面板会列出接下来播放的顺序，可直接点某首跳转。

**码率 / 音质**：上传时后端按文件大小与时长算出平均码率，音乐库和播放页都会显示（如 `320 kbps`）。
播放页的「音质 / 码率」下拉可切换档位，只提供低于原始码率的选项，切换时保持播放进度。

> 真正切换码率需要服务器有 **ffmpeg**（本次环境没有，因此默认只有「原码率」可用，界面会提示）。
> 启用方法：装好 ffmpeg 后在 `config.json` 填 `"ffmpeg_path": "C:\\ffmpeg\\bin\\ffmpeg.exe"`
> （或把 ffmpeg 加进 PATH，留空自动查找），重启 `CloudMusic.exe`。首次选择某档位会按需转码，
> 结果缓存到 `<data_dir>\transcodes\`，之后直接命中缓存；删除曲目时缓存一并清理。
> 转码失败会自动回退原文件，不影响播放。

## 四点八、各页面版本号

点首页卡片进入的**每个界面都会显示当前版本号**，不用再登录服务器查：

| 界面 | 显示位置 | 版本号来源 |
| --- | --- | --- |
| 云收音机 `/radio/` | 顶栏右侧 | 自身 `/api/meta`（如 `v1.5.7`） |
| 云电视 `/tv/` | 顶栏右侧 | 自身 `/api/meta`（如 `v1.3.5`） |
| 云音乐 `/music/` | 顶栏右侧 | 自身 `/api/meta`（如 `v1.0.0`） |
| 工作日志 `/work/` | 顶栏右侧 | 自身 `/api/meta`（如 `v1.0.0`） |
| 门户首页 / 登录页 / 状态页 | 页脚 | CloudAuth `/auth/health`（如 `v1.1.3`） |

- 版本号**由服务端动态返回**，没有写死在 HTML 里：页面加载后由共用的 `version.js`
  请求版本接口填入标签，鼠标悬浮会显示「哪个服务 · 版本 X」。
- 取不到版本（未登录 / 服务未启动）时标签自动隐藏，不影响页面其它功能。
- `version.js` 自带样式，不依赖各页面 CSS；深浅主题都适配，窄屏自动缩小。
- 门户目录要多放一个文件 `version.js`；三个应用的 `version.js` 已打包在各自 exe 内，无需单独放。
- 升级某个应用后，页面上显示的版本号会跟着新 exe 一起变，不用改门户文件。

## 四点九、热更新（改文件不用重启 exe）

以前改一行前端代码就要停 exe → 重新 PyInstaller 打包 → 再启动。现在分三种情况：

**① 改前端文件（app.js / index.html / styles.css / version.js …）—— 不用重启，也不用重新打包**

把改好的文件放到 **exe 同级的 `static\` 目录**，文件名保持一致：

```text
C:\CloudMedia\
├─ CloudRadio.exe
├─ CloudMusic.exe
├─ config.json
└─ static\              ← 放要覆盖的文件，只放改过的那些就行
   ├─ app.js
   └─ styles.css
```

- 服务**每次请求都实时读磁盘**，按**文件**回退：外部有的用外部，没有的仍用 exe 内置资源，
  所以不必把整个 static 目录搬出来。
- 外部文件返回 `Cache-Control: no-store`，刷新浏览器（建议 `Ctrl+F5`）立即生效。
- 应用页每 30 秒检查一次资源指纹，发现文件被改动会右下角弹出**「页面已更新 · 点击刷新」**；
  只提示不强制刷新，避免打断正在播放 / 上传 / 编辑歌词。
- 门户首页、登录页、状态页、门户的 `version.js` 本来就是 nginx `html_portal\` 里的静态文件，
  直接覆盖 + 刷新即可，跟 exe 完全无关。

**② 改 config.json —— 不用重启**

直接编辑保存，下一个请求进来时会自动重新载入并记录日志，例如改 `max_upload_mb`、
`ffmpeg_path`、`weather_enabled`、`session_days`、`portal_auth_url` 等。
例外（改了**必须重启 exe** 才生效，日志会明确提示）：`host`、`port`、`data_dir`、`log_dir` ——
这些是启动时就定好的监听地址和数据/日志目录。

**③ 改 Python 后端逻辑（app.py）—— 仍需重新打包 exe**

单文件 exe 里的 Python 代码是编译进去的，无法在运行中替换。这一条没有变。
但绝大多数日常迭代（改界面、改样式、改文案、调配置）都属于 ①②，不用再碰打包。

**诊断**：`curl http://127.0.0.1:8093/api/assets` 返回外部文件列表与指纹；
`/api/meta`（三个应用）和 `/auth/health`（CloudAuth）里都有 `hot_reload` 字段，
显示热更新目录、外部文件数、配置修改时间和「需重启的键」。启动日志也会打印热更新目录。

> ⚠️ **升级 exe 时注意**：外部 `static\` 里的旧文件会**继续覆盖**新版 exe 内置的资源。
> 换了新 exe 后如果页面看起来没变化，先清空（或同步更新）那个 `static\` 目录。
> 想确认某个请求用的是哪份文件，看响应头 `X-Asset-Source: external | bundled`。

## 四点十、工作日志与日程甘特图

首页第 4 张卡片「工作日志」进入 `/work/`（`CloudWork.exe`，8094）。用来记录一天里的事，
再用甘特图看它们落在哪个时间段。

**四种状态**（列表里可直接用下拉切换流转）：

| 状态 | 含义 | 甘特图颜色 |
| --- | --- | --- |
| 将要发生 planned | 计划要做、还没开始的事 | 蓝 |
| 正在发生 doing | 当前进行中的事 | 橙 |
| 待跟踪结果 followup | 事情已经发生，但结果 / 结论还没记下来 | 紫（未填结果时色块带 `!` 标记） |
| 已闭环 done | 结果已记录、跟踪结束 | 绿 |

**记录页（默认）**：左侧表单填标题、日期、起止时间（结束留空默认 1 小时）、状态、备注、
「结果 / 跟踪结论」；右侧按时间排序列出当日事项，可按状态筛选、编辑、删除、导出
CSV（带 BOM，Excel 直接打开）或 JSON。顶部日期选择器可切换任意一天。

**甘特图页**：点顶栏「查看甘特图 →」进入（也可直接用 `/work/#gantt`）。
零外部依赖、纯 HTML/CSS/JS 自绘，不需要 CDN：

- 横轴按小时刻度，默认 06:00–22:00，会按当天事项的最早 / 最晚时间**自动向外扩展**，
  至少 8 小时、最多覆盖 0–24 点
- 每条事项一个色块，按开始 / 结束时间定位；**点击色块跳回记录页并预填编辑**
- 查看今天时有一条红色虚线标出「现在」，每分钟自动更新
- 顶部显示四类数量与合计时长；待跟踪但没写结果的事项有明显标记
- 「← 返回记录」回到列表，浏览器前进 / 后退也能切换两个视图

数据存在 `CloudWork.exe` 同级 `data\items.json`，改 `config.json` 的 `data_dir` 可挪到别的盘。
与另外三个应用一样，已接入统一登录、版本号显示与热更新（见「四点八」「四点九」）。

## 四点十一、跨功能切换与上传保护

**不用再回首页转一道**：收音机 / 电视 / 音乐 / 工作日志四个页面顶栏都有一个应用切换下拉
（显示当前应用名，点开列出四个入口，当前项高亮）。点一下直接跳到另一个功能页，
相对路径 `../radio/` 这类写法在 nginx 子路径下都正确。

- 切换器由共享的 `switcher.js` 注入（和 `version.js` 一样，四个应用各打包一份），
  样式自带并按页面背景亮度自动适配深浅主题，不依赖各应用的 CSS 变量。
  `portal/switcher.js` 只是源码母版，**门户 `html_portal\` 目录不需要放这个文件**；
  升级时替换四个应用 exe 即可（或用热更新把新 `switcher.js` 放进各 exe 同级的 `static\`）。
- **独立运行（直接访问 8090/8091/8093/8094 端口）时切换器自动隐藏**，
  因为那时 `../radio/` 会是死链；只有挂在门户子路径下才显示。

**上传音乐时切走不会中断**：浏览器在**同一个标签页**里导航会直接中止正在进行的上传请求，
所以用「后台继续传」的原生办法做不到。这里的处理是：

1. 上传进行中，点任何会离开音乐页的链接（切换器菜单、「← 门户」、状态页链接等）都会先弹确认；
2. 确认后**用新标签页打开目标功能**，音乐页保持原样，上传在原来的标签页里继续跑；
3. 只有**关闭这个音乐标签页（或整个浏览器）**，上传才会停止；
4. 上传中直接关闭 / 刷新音乐页，浏览器还会再给一次原生的离开确认，避免误关。

> 实现上，`music-app/app.js` 暴露 `window.__cmUploadActive()` 实时反映上传状态，
> `switcher.js` 在捕获阶段拦截 `<a>` 点击；确认弹窗的「留在本页」可取消，
> 什么都不做就继续留在音乐页。没有上传时所有链接行为完全不变。

## 五、运行状态页

`http://服务器IP/status` 是实时监控页，需要已登录（未登录会跳登录页）。

把 `status.html` 和另外两个页面一起放进 `nginx\html_portal\`。

| 区块 | 内容 |
| --- | --- |
| 四个指标 | 活跃连接、每秒请求、累计请求、累计连接 |
| 趋势图 | 近 2 分钟的每秒请求与活跃连接双线，峰值标注 |
| nginx | 版本、Reading / Writing / Waiting、处理成功率 |
| 服务卡片 | 云收音机、云电视的在线状态、版本、运行时长 |

每 2 秒刷新一次，切到后台标签页会自动暂停采集。

**原理**：nginx 内置 `stub_status` 模块，配置里加了这两段：

```nginx
location = /status      { return 302 /status.html; }
location = /nginx_status { stub_status; access_log off; }
```

**前提**：nginx 编译时带了 `--with-http_stub_status_module`。官网 Windows 版默认带；
不确定就跑一次 `check-nginx.bat`。万一没有，注释掉 `location = /nginx_status`。

> 本次改造**不需要** `auth_request`、`lua`、`njs` 等额外模块，只用 nginx 核心指令，
> 所以任何官方 Windows 版 nginx 都能直接跑。

## 六、验证

```bat
curl -I http://127.0.0.1/                    :: 应返回 200（首页公开）
curl -I http://127.0.0.1/radio/              :: 应返回 302 -> /login.html?next=/radio/
curl -I http://127.0.0.1/auth/health         :: 应返回 200 JSON
curl -I http://127.0.0.1:8092/auth/ping      :: 登录服务活着
```

浏览器打开 `http://服务器IP/` → 直接看到首页 → 点「进入收听」→ 弹出登录页 →
输入账号密码 → 进入收音机。再开一个标签点「进入观看」不用重复登录。

日志里能看到真实访问 IP（nginx 通过 `X-Real-IP` 传给后端）。

## 七、防火墙

只放行 80：

```powershell
New-NetFirewallRule -DisplayName "CloudMedia 80" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
```

如果之前给 8080/8081 加过放行规则，那是**原有站点**的，别删。
**不要**给 8090 / 8091 / 8092 加放行规则。

## 八、常见问题

| 现象 | 原因 |
| --- | --- |
| 点卡片后登录页一直转 / 提示「无法连接登录服务」 | `CloudAuth.exe` 没启动或端口不是 8092。跑 `check-nginx.bat` 看第 4 项，然后双击 `start.bat` |
| 密码明明对却登录不了 | ① `config.json` 的 `htpasswd` 路径不对；② 文件不是 `{SHA}` 格式；③ 账号文件为空。`check-nginx.bat` 第 3 项能看到 |
| 登录后立刻又跳回登录页 | 两个后端还是旧版 exe，没换成 v1.5.7 / v1.3.5；或 CloudAuth 的 `data\secret.key` 被删过 |
| **输入密码后进不去收音机 / 电视，重启 CloudAuth.exe 才正常** | **v1.1.1 及以前的两个稳定性问题**：① nginx 到 CloudAuth 是 HTTP/1.1 keep-alive，旧版不给空闲连接设超时，每个连接永久占一个线程，长期运行后线程堆积，`/auth/check` 变慢并超过后端 2 秒超时 → 后端判定未登录、弹回登录页；② 按 IP 的失败限流会在锁定期内把**密码正确**的请求也 429 掉。v1.1.2 已修：空闲连接 10 秒自动释放、backlog 提到 128、限流只挡错误密码、探活与天气结果做缓存。**换上新版 CloudAuth.exe 即可，不必再靠重启续命** |
| 想不重启就确认登录服务的健康状态 | 服务器本机访问 `http://127.0.0.1:8092/auth/diag`，看线程数、活跃锁定、账号文件、密钥指纹与版本；外部 IP 访问会被 403 |
| 换了新 exe，页面却还是老样子 | exe 同级 `static\` 里的旧文件覆盖了新版内置资源；清空或同步更新该目录。响应头 `X-Asset-Source` 可看出用的是 external 还是 bundled |
| 改了前端文件但刷新没变化 | ① 文件放错目录（要放在 exe 同级的 `static\`，不是 nginx 的 `html_portal\`）；② 文件名不一致；③ 浏览器缓存，用 `Ctrl+F5`；④ 改的是 Python 后端逻辑，那仍需重新打包 |
| 首页显示「未响应」 | 8090 / 8091 / 8093 / 8094 没起，或登录服务没起（首页状态走 `/auth/health`）；换上 v1.1.3 的 CloudAuth.exe 才会探测 8094 |
| 左侧 / 顶栏天气一直「暂不可用」 | ① `CloudAuth.exe` 没启动或版本太旧（天气接口在 v1.1.0+）；② 服务器访问不了 `weather.com.cn` / `pconline.com.cn`。不影响登录与播放。可在服务器执行 `curl http://127.0.0.1:8092/auth/weather` 直接看返回 |
| `/status` 页顶部提示「读不到 nginx 指标」 | nginx 没启用 `stub_status`，或 `location = /nginx_status` 没加 |
| `nginx -t` 报端口被占用 | 8090/8091/8092/8093/8094 上有旧进程，任务管理器结束再启动 |
| 打开 `/radio/` 空白、控制台 404 | 访问时少了结尾斜杠；已配 301 补齐，直接用 `/radio/` 最稳 |
| 页面能开但没声音 | `proxy_buffering off;` 没生效，直播流被缓冲住了 |
| 门户页没变化 | `Ctrl+F5` 强刷；`index.html` / `login.html` 有浏览器缓存 |
| `make-htpasswd.bat` 一闪就退 | 管理员身份打开命令提示符，`cd C:\nginx\conf` 后手动执行 |
| 云音乐上传报「write_failed」/ 传不上去 | ① `data_dir` 指向的目录不存在或没写权限；② 单首超过 `max_upload_mb`（默认 200MB）；③ nginx 的 `client_max_body_size 280m` 被改小了（大文件会在 nginx 层 413）；调整 `max_upload_mb` 时也要同步放大它。看 exe 启动日志里的「音乐文件保存目录」 |
| 云音乐上传的文件在服务器找不到 | 默认在 exe 同级 `data\uploads\`，落盘名是内部 ID（如 `a1b2c3d4.mp3`）而非原始文件名；页面上传区、启动日志、`/api/meta` 的 `upload_dir` 都会给出实际路径 |
| 播放页码率下拉只有「原码率」 | 服务器没装 ffmpeg；按「四点七」配置 `ffmpeg_path` 并重启 `CloudMusic.exe` 就会出现可选档位 |
| 歌词不滚动 / 高亮不对 | 检查 LRC 是否为 `[mm:ss.xx]歌词` 格式；可用 `[offset:500]` 整体延后 0.5 秒校准 |

出问题先双击 **`check-nginx.bat`**，它会一次打出：nginx 版本、`nginx -t` 结果、
账号文件在不在/几行/首个用户名、8090 / 8091 / **8092** 有没有在监听、
最近 12 条错误日志，并给出对照结论。

## 九、日常维护

| 操作 | 命令 |
| --- | --- |
| 改密码 | 双击 `make-htpasswd.bat`，用户名填原来的（改完所有旧会话立即失效，无需重启） |
| 加账号 | 双击 `make-htpasswd.bat`，换个用户名 |
| 退出登录 | 首页页脚「退出登录」，或应用内的退出按钮 |
| 重载配置 | 双击 `restart-nginx.bat`（内部含 `nginx -t`） |
| 重启三个后端 | `restart-nginx.bat all`（记得先在脚本开头填 `AUTH_DIR` / `RADIO_DIR` / `TV_DIR`） |
| 看日志 | `nginx\logs\error.log`；登录日志 `CloudAuth\logs\auth-*.txt`；应用日志在各自 `logs\` |
