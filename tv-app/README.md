# 云电视 · CloudTV

和前面那套收音机同一套思路：**后端自己跑在服务器上，负责源管理和探活，前端只负责播**。单端口、无广告、无统计脚本、hls.js 本地化，Windows 上双击 exe 即用。

---

## 一、先说清楚合规边界

这个程序**不内置任何影视内容**，它是一个「播放器 + 源自检器」：

| 会做的 | 不会做的 |
| --- | --- |
| 播放你配置的 HLS / 直连流地址 | 抓取或内置盗播聚合列表 |
| 探活、标记可用性、自动切线路 | 绕过 DRM、鉴权、地域限制 |
| 导入你手上的 M3U 播放列表 | 转播、分发、对外提供公开服务 |

预置了什么：

1. **1 个开源示例流**（Big Buck Bunny，CC-BY 授权），用来确认播放链路正常——打开就能看到画面。
2. **18 个央视频道条目**，地址取自央视网网页播放器使用的官方 CDN 模板。**实测这些地址目前对直连拉流做了限制**（我这台机器和你服务器上都返回 403 / 域名不可达），所以探活后大概率显示「离线」。这不是程序问题，是央视收紧了防盗链。

为什么不再往里塞更多「能用的」电视台：国内电视台的官方流基本都带鉴权或防盗链，绕过它就是不当使用；网上流传的 IPTV 列表绝大多数是盗播，个人装在自己服务器上还无所谓，一旦对外就是另一回事。所以这个工具的选择是：**能力给足，源由你决定**。

你可以在「源管理」里接的东西：自己或单位有授权的直播源、内网转码流、自己搭的摄像头/推流、公共领域或 CC 授权的视频流。仅个人观看，不要公开转播或商用。

---

## 二、启动

```
dist/
├─ CloudTV.exe      ← 单文件，约 10MB
├─ config.json
└─ start.bat
```

双击 `CloudTV.exe`，浏览器打开 `http://127.0.0.1:8081`（默认端口 8081，跟你那套收音机的 8080 不冲突）。

```bat
CloudTV.exe --port 9000     :: 换端口
set PORT=9000 && CloudTV.exe
```

首次启动会自动写入一张频道表到 `data/channels.local.json`，并立刻探活一遍（19 个条目，约十秒）。

## 三、配置 config.json

```json
{
  "host": "0.0.0.0",
  "port": 8081,
  "admin_token": "",
  "probe_on_start": true,
  "direct_play": true
}
```

| 字段 | 说明 |
| --- | --- |
| `host` | `0.0.0.0` 允许局域网访问；只给自己用可填 `127.0.0.1` |
| `port` | 服务端口 |
| `admin_token` | 留空表示打开页面的人都能改频道表；填写后增删改需带 `X-Admin-Token` 请求头 |
| `probe_on_start` | 启动时是否自动探活 |
| `direct_play` | 直连播放：源站允许跨域时浏览器直连源站拉流，**视频流量不占服务器带宽**；直连失败自动回退服务器代理。设为 `false` 则一律走服务器代理（默认 `true`） |
| `log_dir` | 日志目录，默认 `logs` |
| `log_days` | 日志保留天数，默认 14 |
| `host` | 放在 nginx 后面时改成 `127.0.0.1`，只接受本机连接 |

### 播放通道（直连 / 代理）说明

- **直连**：探活时会顺带检测源站是否返回 `Access-Control-Allow-Origin: *`。允许的源，浏览器直接向源站拉流（列表、分片全部本地↔源站），服务器只出页面和频道表那点流量。播放器右下角闪电按钮点亮为绿色即直连模式。
- **代理**：源站不允许跨域、有防盗链、或直连失败时，自动改走服务器中转（原行为），保证能播。
- 播放器上的闪电按钮可手动锁定代理（担心服务器 IP 被源站屏蔽等情况），再点一次恢复自动。
- 注意：直连模式下观看者的 IP 会直接暴露给源站 CDN（代理模式则暴露的是服务器 IP）。

## 四、怎么加频道

**方式一：界面（推荐）**

右上角「源管理」→「新建频道」，填名称、分组、源地址（每行一个，会自动按顺序做备用线路）。保存后立即探活，能连上的排到列表前面。

**方式二：导入 M3U**

「源管理」→「导入 M3U」，粘贴播放列表文本。程序会解析 `#EXTINF` 里的 `tvg-name` / `group-title`，逐条探活。

**方式三：直接改文件**

`data/channels.local.json`，改完点一下「探活」：

```json
{
  "channels": [
    {
      "id": "my-channel",
      "name": "我的频道",
      "group": "自定义",
      "official": "https://example.com/live/",
      "desc": "",
      "origin": "custom",
      "sources": [{ "url": "https://example.com/live/index.m3u8", "label": "主线路", "hls": true }]
    }
  ]
}
```

`official` 会被当作 Referer 发给上游，对防盗链的源有用。

## 五、接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 存活、频道数、在线数 |
| GET | `/api/meta` | 分组统计、上次探活时间、是否正在探活 |
| GET | `/api/channels?group=&q=&online=1` | 频道列表 |
| GET | `/api/channels/{id}` | 单个频道，含全部线路与探活结果 |
| GET | `/api/stream/{id}?i=0` | 流代理；HLS 播放列表、分片、密钥全部重写走本地 |
| POST | `/api/channels` | 新增 / 更新 `{name, group, official, sources:[{url}]}` |
| DELETE | `/api/channels/{id}` | 删除 |
| POST | `/api/import` | 导入 `{m3u: "..."}` |
| POST | `/api/probe` | 测单个地址 `{url}` |
| POST | `/api/refresh` | 重新探活全部 |

## 六、日志

控制台的输出同时写进 txt，按天分文件：`logs/tv-2026-11-04.txt`，保留天数由 `log_days` 控制（默认 14 天），启动时自动清理过期文件。

```
[2026-11-04 17:20:11] INFO  云电视 v1.1.0 启动
[2026-11-04 17:20:11] INFO  日志文件：...\logs\tv-2026-11-04.txt（保留 14 天）
[2026-11-04 17:20:12] INFO  开始探活频道源
[2026-11-04 17:20:19] INFO  探活完成：3/19 个频道可用
[2026-11-04 17:21:40] WARN  拉流失败：某某台（连接超时）
[2026-11-04 17:22:02] INFO  保存频道：测试台（ch-48213），1 个源，新增
[2026-11-04 17:22:40] INFO  导入 M3U：解析 12 条，新增 10 个频道
```

- 拉流失败按频道 5 分钟只记一条，不会被坏源刷满磁盘。
- 记录频道的新增/编辑/删除与 M3U 导入，出问题时能回溯是谁改的。
- 所有频道都探活失败时会额外记一条 WARN，方便判断是不是整台服务器出网有问题。

## 七、部署到 Windows Server

> 和收音机共用一个入口（单端口 + 统一登录）的部署方式，见 `../portal/README.md`。


```powershell
New-NetFirewallRule -DisplayName "CloudTV 8081" -Direction Inbound -Protocol TCP -LocalPort 8081 -Action Allow

# 开机自启（NSSM）
nssm install CloudTV "D:\CloudTV\CloudTV.exe"
nssm set CloudTV AppDirectory "D:\CloudTV"
nssm set CloudTV Start SERVICE_AUTO_START
nssm start CloudTV
```

想用域名 + HTTPS，在 IIS/Nginx 里反代到 `http://127.0.0.1:8081` 即可：视频是经 `/api/stream/` 由程序转发，页面走 HTTPS 也不会被混合内容拦。

## 八、常见问题

| 现象 | 处理 |
| --- | --- |
| 央视频道全显示离线、提示 403 | 官方 CDN 鉴权所致，属预期。换成你有授权的源即可 |
| 加了源显示可用但画面不动 | 源可能是「暖场片」或编码不被浏览器支持；先点「测试」看返回是否为 `#EXTM3U` |
| 提示需要 token | `config.json` 里配了 `admin_token`，请求时带上 `X-Admin-Token` |
| 手机端锁屏暂停 | 浏览器策略，属于预期行为 |
| 想临时验证播放器 | 频道表里的「播放测试 · 开源示例流」就是干这个的 |

## 九、重新打包

```bat
build\build.bat
```

等价命令：

```bat
python -m PyInstaller --onefile --console --name CloudTV ^
  --workpath ".pyi\build" --specpath ".pyi" --add-data "static;static" app.py
```
