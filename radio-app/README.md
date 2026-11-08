# 云收音机 · CloudRadio

一款**没有广告**的网络收音机。后端在你自己的 Windows 服务器上运行，定时从公开电台目录抓取全国广播频道、逐个校验流媒体地址是否可播放，前端只做一件事：把声音放出来。

- 频道库自动更新（默认每 6 小时一轮）
- 播放页与接口由同一个程序托管，单端口、免 nginx
- 内置流代理：解决 http 混流、跨域、防盗链 Referer 问题
- 前端零广告、零统计脚本、零第三方字体/CDN，hls.js 已本地化
- 支持收藏、最近播放、备用线路切换、定时关闭、系统媒体键、PWA 安装
- **节目表**：每个电台可查看昨天 / 今天 / 明天的节目单，自动定位并高亮「正在播出」
- **在线收听面板**：看当前有哪些登录会话在听、听的是哪个台、听了多久，同时写进日志

---

## 一、拿到什么

```
radio-app/
├─ dist/
│  ├─ CloudRadio.exe      ← Windows 可执行文件（约 12MB，单文件）
│  ├─ config.json         ← 配置文件
│  └─ start.bat           ← 双击启动
├─ app.py                 ← HTTP 服务 + 流代理
├─ crawler.py            ← 抓取、去重、分类、探活
├─ static/                ← 播放页（已打进 exe）
├─ seed/channels.json     ← 内置种子频道库（首次启动即有内容）
└─ build/                 ← 打包脚本
```

## 二、启动

把 `CloudRadio.exe` 和 `config.json` 放在同一个目录（例如 `D:\CloudRadio\`），双击 `CloudRadio.exe`，浏览器打开：

```
http://127.0.0.1:8080
```

局域网内其它设备访问：`http://<服务器IP>:8080`。

首次启动会载入内置种子频道库（234 个频道，立即可听），随后在后台自动抓取校验一遍最新地址。

命令行参数：

```bat
CloudRadio.exe                  :: 默认 0.0.0.0:8080
CloudRadio.exe --port 9000      :: 改端口
set PORT=9000 && CloudRadio.exe :: 或用环境变量
```

## 三、配置 config.json

```json
{
  "host": "0.0.0.0",
  "port": 8080,
  "refresh_hours": 6,
  "admin_token": "",
  "crawl_on_start": true
}
```

| 字段 | 说明 |
| --- | --- |
| `host` | 监听地址，`0.0.0.0` 表示允许局域网访问 |
| `port` | 端口，被占用时启动日志会提示 |
| `refresh_hours` | 自动刷新频道库的间隔（小时），最小 0.2 |
| `admin_token` | 留空表示任何能打开页面的人都能点「更新频道库」；填写后需带 `X-Admin-Token` 请求头才能触发 |
| `crawl_on_start` | 启动时是否自动抓取一轮 |
| `username` / `password` | 网页登录账号密码，明文；详见下一节 |
| `session_days` | 登录保持天数 |
| `log_dir` | 日志目录，默认 `logs`（相对 exe 所在目录） |
| `log_days` | 日志保留天数，默认 14 |
| `trust_proxy_auth` | 放在 nginx 后面时设为 `true`：信任反代传来的 `X-Auth-User`，不再要求网页登录（仅采信 127.0.0.1 的请求） |

环境变量 `PORT` / `HOST` / `REFRESH_HOURS` 优先级高于配置文件。

## 四、登录与密码

打开网页会先要求登录，未登录时首页会跳转、接口和音频流一律返回 401——端口被扫到也只听不到声音。

**账号密码明文保存在 `config.json`**，SSH 上去直接改，改完重启生效：

```json
{
  "username": "admin",
  "password": "换成你自己的密码",
  "session_days": 30
}
```

| 字段 | 说明 |
| --- | --- |
| `username` | 登录用户名 |
| `password` | 登录密码，明文。**留空**则下次启动自动生成随机密码并写回本文件 |
| `session_days` | 登录保持天数，默认 30；到期需重新登录 |

行为说明：

- 密码明文是为了"改起来方便"，也意味着**谁能读到这个文件谁就能登录**，注意服务器文件权限。
- 连续输错 8 次会锁定该 IP 15 分钟，防止被脚本暴力破解。
- 登录态存在 `data/sessions.json`（只存 token 的 SHA-256，不存密码），重启不掉登录。
- **忘记密码**：把 `password` 改成空字符串，重启后会自动生成新密码并打印在控制台。
- 页面右上角有「退出」按钮，退出即刻失效当前 token。
- 登录保护对所有内容生效，包括 `/_selftest.html` 自检页；只有登录页和 `/api/health` 是公开的。

## 五、日志

所有输出同时打到控制台和 txt 文件，进程被 kill 也不会丢：

```
CloudRadio.exe 所在目录/
└─ logs/
   ├─ radio-2026-11-04.txt   ← 当天
   └─ radio-2026-11-03.txt   ← 前一天
```

格式是固定列宽，方便查找或导入表格：

```
[2026-11-04 17:07:40] INFO  服务已就绪：http://127.0.0.1:8080
[2026-11-04 17:09:12] WARN  登录失败：203.0.113.7 尝试用户名 'admin'
[2026-11-04 17:12:03] INFO  开始抓取频道库（触发方式：定时任务）
[2026-11-04 17:13:35] INFO  频道库更新完成：238 个可用频道
```

记录的内容：

| 级别 | 什么情况 |
| --- | --- |
| INFO | 启动/停止、监听地址、载入频道数、抓取开始与结束、登录成功、手动更新、频道保存、开始收听/切台/停止收听 |
| WARN | 登录失败、未登录访问被拒（同 IP 每小时合并成一条，带累计次数与路径示例）、拉流失败（同频道 5 分钟记一次）、抓取源不可用 |
| ERROR | 抓取异常（带堆栈）、未捕获异常、线程崩溃 |

几点说明：

- 每个文件一天一个，启动时自动删除超过 `log_days` 天的旧文件。
- 同一条"拉流失败"不会刷屏，按电台 5 分钟一次；未登录访问按 IP 每小时合并一条，形如 `未登录访问被拒：1.2.3.4 累计 37 次，路径示例：/api/channels、/api/epg/r0053`。
- **随机生成的密码只打印在控制台，不写进日志文件**，避免日志外传时泄露。
- 日志里有访问者 IP，如果要把日志发给别人排查，记得先脱敏。
- 直接双击 `logs` 目录下当天的 txt 用记事本看即可，编码是 UTF-8。

## 六、在线收听

顶栏的「在线 N」按钮点开就是当前收听面板：哪个登录用户、从哪个 IP、在听哪个电台、几点开始、已经听了多久。每 10 秒自动刷新一次。

```
admin（我）   辽宁之声
127.0.0.1    新闻 · 17:57:30 开始        0:06
```

实现上不靠页面轮询上报，而是**只在"开始播放 / 切台"时上报一次**：

- 浏览器 `playing` 事件触发 → `POST /api/playing {station_id}`，服务端记下这个会话正在听什么；
- 暂停后有 1.5 秒缓冲再上报停止，避免弱网断流重连时刷出一堆"停止/开始"；
- 关页面时用 `sendBeacon` 补一条停止，不阻塞关闭；
- 记录按登录会话存（同一浏览器多个标签页算一个人），服务重启后清空；
- 超过 12 小时没动静的记录自动失效，不会挂着幽灵用户。

日志里同时留下三条痕迹（详见下一节）：

```
[17:57:30] INFO  播放：admin (127.0.0.1) 开始收听 辽宁之声
[17:57:40] INFO  切换电台：admin (127.0.0.1) 辽宁之声 → 经济之声
[17:57:47] INFO  停止收听：admin (127.0.0.1) 经济之声，本次 不到 1 分钟
```

## 七、部署到 Windows Server

> 想用一个端口 + 统一登录把收音机和电视都挂上去，见 `../portal/README.md`（门户页 + nginx 反代）。
> 那种部署方式下，本程序的 `host` 要改成 `127.0.0.1`，并打开 `trust_proxy_auth`。


### 1. 放行端口

```powershell
New-NetFirewallRule -DisplayName "CloudRadio 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

### 2. 注册为开机自启服务（推荐 NSSM）

```powershell
# 下载 nssm.exe 后
nssm install CloudRadio "D:\CloudRadio\CloudRadio.exe"
nssm set CloudRadio AppDirectory "D:\CloudRadio"
nssm set CloudRadio AppStdout "D:\CloudRadio\logs\out.log"
nssm set CloudRadio AppStderr "D:\CloudRadio\logs\err.log"
nssm set CloudRadio Start SERVICE_AUTO_START
nssm start CloudRadio
```

不想装 NSSM，用任务计划程序也可以：触发器「计算机启动时」，操作指向 `CloudRadio.exe`，勾选「不管用户是否登录都要运行」。

### 3. 用域名 + HTTPS 暴露到公网？（可选）

在 IIS / Nginx 里把 `https://radio.example.com` 反代到 `http://127.0.0.1:8080` 即可。**因为音频是经由本程序的 `/api/stream/` 代理转发，页面走 HTTPS 也不会出现混合内容拦截**，上游是 http 的电台同样能播。

## 八、接口（如需自己做客户端）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 存活状态、频道数、运行时长 |
| GET | `/api/meta` | 最后更新时间、分类/地区统计、是否正在刷新 |
| GET | `/api/channels?q=&cat=&region=&limit=&offset=` | 频道列表（默认按推荐排序） |
| GET | `/api/channels/{id}` | 单频道详情，含全部可用线路 |
| GET | `/api/epg/{id}?day=today` | 节目单：`yesterday` / `today` / `tomorrow` |
| GET | `/api/stream/{id}?i=0` | 流代理；HLS 播放列表会自动改写为代理地址 |
| POST | `/api/refresh` | 触发一次抓取（需已登录） |
| POST | `/api/login` | 登录 `{username, password}`，成功后写 HttpOnly Cookie |
| GET | `/api/session` | 校验登录态，返回当前用户名 |
| POST | `/api/logout` | 退出登录 |
| POST | `/api/playing` | 上报正在收听 `{station_id}`（同台重复上报只更新时间） |
| POST | `/api/playing/stop` | 停止收听 |
| GET | `/api/listening` | 当前收听列表：用户、IP、电台、开始时间、已听秒数 |

示例：

```bash
curl http://127.0.0.1:8080/api/channels?cat=音乐&limit=5
```

## 九、节目表是怎么来的

- 数据源：蜻蜓 FM 公开的频道节目单索引（移动站页面内嵌的 `ChannelStore.playBill`，含昨天 / 今天 / 明天三天），程序只读解析，不下载、不转存音频。
- 匹配逻辑：先看电台流地址里带的频道 id，用频道名校验；对不上就按名字搜索，再用「地名前缀 + 频率数字 + 最长公共子串占比」三重确认，宁可没有也不串台（例如「宜春交通音乐广播」不会被匹配成「伊犁交通音乐广播」）。
- 缓存：节目单 3 小时过期；电台与频道的对应关系存在 `data/epg_map.json`，重启不用重新匹配。
- 覆盖：抽查 30 个频道约 47% 有节目单（地方台、纯网络台多数没有收录）。没有的显示「这个电台还没有节目单」，不影响收听。
- 播放条副标题显示「正在播：节目名（起止时间）」；点播放条上的列表图标或按 `E` 打开完整节目表。

## 十、自检与排错

浏览器打开 `http://127.0.0.1:8080/_selftest.html`，它会依次验证：频道接口 → 播放列表代理 → 分片加载 → 实际出声。**部署到服务器后先跑这个页面**，能快速区分是「服务器出网问题」还是「某个电台本身挂了」。

常见情况：

| 现象 | 处理 |
| --- | --- |
| 启动日志报「所有抓取源均不可用」 | 服务器访问不了公开目录，检查出网/DNS/代理；此时会继续用上一次的频道库 |
| 频道列表空、状态一直「正在更新」 | 抓取需要走外网，首次约 1–3 分钟；看日志进度 |
| 某个电台放不出来 | 页面会自动切备用线路；仍失败说明该台临时停播，点「更新频道库」后重试 |
| 大部分电台都放不出来（旧版本） | 已修复：流代理曾把浏览器的 `Range` 头转发给上游，导致直播流被当作分段文件返回 206，浏览器判定为格式错误。升级到 1.1 后不再转发 Range，一律按实时流处理 |
| 端口被占用 | 改 `config.json` 的 `port`，或用 `--port` |
| 杀毒软件拦截 exe | PyInstaller 单文件程序常被误报，加白名单即可 |
| 手机浏览器后台暂停播放 | 移动端浏览器策略所致，锁屏后用系统媒体控件恢复 |

## 十一、自己重新打包

源码改完后，双击 `build\build.bat`。它会安装/更新 PyInstaller 并生成 `dist\CloudRadio.exe`（约 1 分钟）。

手动等价命令：

```bat
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --console --name CloudRadio ^
  --add-data "static;static" --add-data "seed;seed" app.py
```

## 十二、数据与版权

- 频道与流地址来自公开电台目录（radio-browser 社区数据库），程序每轮会重新探活，失效条目自动剔除。
- 音频版权归各广播电台所有，本程序仅做地址聚合与转发，供个人收听使用，请勿用于商业转播。
- 程序不收集任何用户数据，不做埋点统计。
