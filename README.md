# PanShare

115 网盘 → STRM → Emby 的全链路工具。**Go + SQLite 单二进制后端，Vue3 + TypeScript 前端，Docker 一把梭**。

生成 strm 只是起点：播放时由 PanShare 用账号 cookie 现场换取 115 直链再跳转，
strm 里不落任何会过期的链接；同时把分享转存、ed2k、秒传、TG 通知、
双向联动删除这些周边动作都收进同一个控制台。

---

## 功能一览

| 模块 | 能力 |
| --- | --- |
| **115 助手** | 扫码 / Cookie 两种方式添加账号，多账号并存，自动取 115 用户 ID 与昵称，可改备注名、设默认账号；**同播复制**开关（同账号两台设备同时看同一文件时，给后来者复制一份，各走各的直链） |
| **115 STRM** | 按「账号 + 网盘路径 → 本地目录」生成 strm，**一条配置可挂多条映射**；每条配置可单独设请求头、媒体 / 元数据扩展名；**增量** / **全量** / **全新** 三种语义；定时巡检；**事件监控**读 115 操作事件，新增即生成、删除即清理（接口不可用时自动退回目录指纹探测） |
| **分享转存** | 粘分享链接 → 转存到接收目录 → 用转存结果建自己的长期分享 → strm 指向自己的分享；**不转存直接生成**开关用于「分享本来就是自己的」；账号 / 接收目录 / 本地目录选过一次就记住 |
| **ed2k STRM** | ed2k 链接接入 115 离线下载并生成 strm；默认**播放时才转存**，也可改为添加时立刻转存；账号 / 转存目录 / 本地目录固定沿用上次选择；支持导入 / 导出 |
| **115 秒传** | 指定「被秒传账号」+ 落盘目录，把 Emby 用户映射到账号；该用户播放时先秒传到映射账号目录，再用该目录的资源播放 |
| **Emby 服务器** | 登记多台 Emby（名称 / 地址 / API Key，可测连通、读用户列表）；秒传用户映射与联动删除都用这里的服务器 |
| **Emby 反代** | 独立端口整站反代第一台启用的 Emby：媒体路由直接 **302 到 115 直链**（流量不过本服务），PlaybackInfo 强制 DirectPlay，HTML/JS 去掉 crossOrigin 以避开 CORS |
| **TG 通知** | 任务开始 / 完成 / 失败推 Telegram；同一 bot 可直接在聊天里查看状态、按配置增量 / 全量生成、**发 ed2k 或分享链接直接生成 strm**（指令表会注册成 TG 菜单） |
| **联动删除** | strm 消失超宽限期 → 删 115 对应文件；115 删文件 → 清本地 strm（连同 nfo / 字幕 / 海报 / `影片-poster.jpg`）；两个方向独立开关，待办可撤销 |
| **天翼云盘** | 插件形态，需要时再装（内置，未安装不启动、不占端口） |

---

## 架构

```
浏览器 ──► :9527  Vue3 控制台（SPA）
             │
             ├─ /api/*        控制台 REST API（HMAC 签名 cookie 会话）
             ├─ /p115/play/*  播放入口 → 302 到 115 直链
             ├─ /p115/share/* 分享通道入口 → 302
             ├─ /p115/ed2k/*  ed2k 入口（文件被清理会自动重新转存）
             └─ /api/plugins/{id}/proxy/*  插件反代

Emby 客户端 ──► :7259  Emby 反代（PS_EMBY_PROXY_PORT）
             ├─ /Videos|/Audio|/Items/{id}/(stream|download|file) → 302 到 115 直链
             ├─ /Items/{id}/PlaybackInfo  → 强制 DirectPlay，改写 DirectStreamUrl
             ├─ /System/Info              → 端口改写成 7259
             └─ 其余（含 WebSocket）       → 透传到 Emby

            SQLite（WAL）：账号 / 配置 / strm 索引 / 任务 / 事件 / 联动待办
            映射根：/strm 下的纯文本 .strm（内容只有一个 URL）
```

后端按职责分包，互不越界：

```
backend/
  cmd/panshare/            入口（-web 指定前端目录）
  internal/config/         环境变量
  internal/model/          Account / StrmConfig / Settings …
  internal/db/             SQLite schema + 全部 DAO
  internal/p115/           115 Web API 客户端（限流、WAF 退避、扫码、分享、离线、秒传）
  internal/strm/          映射根安全读写、扩展名匹配、同目录元数据
  internal/service/        业务编排（生成 / 分享 / ed2k / 秒传 / 复制 / 联动 / TG / Emby / 插件）
  internal/server/         路由、登录态、静态文件、播放 302
  plugins/cloud189/        天翼云盘插件（独立进程 + 独立页面）
frontend/
  src/views/               12 个页面（首页 / 账号 / STRM / 分享 / ed2k / 秒传 / 媒体 / 通知 / 联动 / 插件 / 设置 / 登录）
  src/lib/                 api 封装、类型、toast、设置缓存
  src/components/          AppModal、DirPicker（网盘目录选择器）
```

**为什么 strm 索引进数据库**：十万级文件用 JSON 全量读写会卡死，「增量只补新增」也没法做差集。

---

## 快速开始（Docker）

```bash
cd panshare-go
# 先改一下要写进 strm 的地址，必须是播放器能访问到的
export PS_PUBLIC_BASE_URL=http://192.168.1.10:9527
docker compose up -d --build
```

打开 `http://<你的IP>:9527`，用 `admin / password` 登录（账号密码由 compose 的
`PS_USERNAME` / `PS_PASSWORD` 决定，部署后请改掉）。登录页背景随机取自 TMDB 热门影视，
不需要额外申请 Key（填 `PS_TMDB_API_KEY` 可改走官方 API）。

| 端口 | 用途 |
| --- | --- |
| 9527 | 控制台 + 播放 302 |
| 7259 | Emby 反代（`PS_EMBY_PROXY_PORT`，设 0 关闭） |

| 卷 | 用途 |
| --- | --- |
| `./config` | SQLite、已安装插件 |
| `./strm` | 映射根，Emby 里把这个目录挂进去当媒体库 |

> Emby 容器要能读到 `./strm`，建议两边用同一个宿主机路径挂载（比如都挂 `/strm`），
> 这样 strm 文件里写的路径在 Emby 那边也解析得到。

### 本地开发

```bash
# 后端（另一个终端里先跑前端 dev server 也行）
cd backend && go build ./cmd/panshare && ./panshare -web ../frontend/dist

# 前端
cd frontend && npm install && npm run dev   # 5173，vite 已代理 /api /p115 /emby 到 9527
```

### 自测

不碰网络也能跑的那部分已经写了测试，改完代码先跑一遍：

```bash
cd backend && go vet ./... && go test ./...
```

覆盖的是最容易坏的三块：分享链接 / ed2k 解析（各种粘贴形态）、
映射根的安全写入与连带删除（越界、同内容跳过、nfo 海报一起清）、
播放路由的 302 / 中转判定与 Range 转发（`f=1` 走 302、`f=3` 走中转）。

---

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PS_ADDR` | `:9527` | 监听地址 |
| `PS_DATA_DIR` | `./data` | 数据目录（SQLite、已装插件） |
| `PS_STRM_ROOT` | `/strm` | 映射根，strm 只能写它下面；也可在「服务设置」里热切换 |
| `PS_PUBLIC_BASE_URL` | `http://127.0.0.1:9527` | **写进 strm 的请求头**，必须是播放器能访问的地址。**不能写 `172.17.0.1` 这类容器内网地址**，电视/手机解析不到 |
| `PS_USERNAME` / `PS_PASSWORD` | `admin` / `password` | 控制台登录，由 compose 决定 |
| `PS_PROXY` | 空 | Telegram 与 TMDB 登录背景共用的代理；留空则跟随 `HTTPS_PROXY` |
| `PS_TMDB_API_KEY` | 空 | 可选；填了走 TMDB 官方 API，不填则从 TMDB 热门页取横幅，效果一样 |
| `PS_SESSION_KEY` | 空 | 会话签名密钥，空则用密码派生；改密码会让旧会话失效 |
| `PS_DISABLE_AUTH` | 空 | `1` = 关掉登录（仅内网调试） |
| `PS_PLUGIN_DIR` | `./plugins` | 内置插件来源目录（安装时复制到数据目录） |
| `PS_TZ` | `Asia/Shanghai` | 时区 |
| `PS_API_TIMEOUT` | `30` | 115 接口超时秒数 |
| `PS_VERSION` | `dev` | 版本号，只用于展示 |

需要随手改的开关（同播复制、联动删除、TG、定时巡检、映射根、扩展名……）都在数据库 `settings` 表，
走「服务设置」页面，不用重启容器。

### 登录页背景

登录页随机挑一张 TMDB 热门影视的 backdrop 做全屏背景，点「换一张背景」可即时换图。
填了 `PS_TMDB_API_KEY` 就走官方 `trending/movie/week` 接口；不填则从 TMDB 首页的热门横幅里取，
不需要额外申请 Key。Key 只在后端使用，浏览器拿不到；取图走「通知」页配的共享代理。
代理不通或 TMDB 暂时不可用时，页面自动回落到渐变背景，不影响登录。

---

## 使用流程

### 1. 先加账号

「115 助手」→ 扫码（推荐）或粘 cookie。加完会立刻拉一次用户信息，拿到 UID 和昵称。

- **同播复制**：给账号开这个开关后，同账号下两台设备同时播同一个文件，后到的设备会拿到
  一份**副本**的直链。副本按原目录复制、文件名不变，缓存 40 分钟；对 strm / ed2k 播放都生效。
  不开的话两台设备共用一个直链，会互相抢带宽。
- 备注名随便改，strm 的账号归属、目录选择器、秒传映射都按 ID 走，改名不影响已有配置。

### 2. 选目录 → 生成 STRM

「115 STRM」→ 新建配置：

| 字段 | 说明 |
| --- | --- |
| 账号 + 任务名称 | 账号来自「115 助手」；名称只是页面上的显示名 |
| 网盘路径 → 本地 STRM 目录 | **可以挂多条映射**（已整理 / 原盘 合成一个任务）。网盘点左边 📁 选；本地默认按「映射根 + 网盘目录名」预填，点「选」可以改成映射根下任意目录 |
| STRM 请求头 | 写进 strm 的基地址，留空用「服务设置」里的全局值；家里走 `192.168.x`、外面走域名的可以按配置分开 |
| STRM 内容预览 | 直接展示这条配置生成出来的一行 URL，参与拼接的就是下面四个部分 |
| 媒体 / 元数据扩展名 | 前者决定哪些文件生成 strm，后者决定哪些文件当元数据下载；英文逗号分隔 |
| 下载元数据 | 顺带把同目录的 nfo / 海报 / 字幕拉到本地 strm 旁边 |
| 事件监控 + 间隔 | 每 N 秒（默认 60，最小 15）拉一次 115 操作事件，**新增文件立刻生成、删除立刻清理** |
| 定时巡检 + 间隔 | 每条配置独立开关，按自己设的分钟数跑增量，兜底用 |

### 事件监控到底怎么工作

跟 p115strmhelper 一个思路：**不扫目录，读 115 的操作事件**（网盘的「生活事件」）。
接口是 `webapi.115.com/behavior/detail`，按账号一条时间线往回翻。

1. 每 `watch_interval` 秒（默认 60，最小 15）用游标 `(update_time, 事件 id)` 往后拉
   一段事件，游标存在库里（`life_cursor:<账号>`），重启不丢；
2. 事件只带 `parent_id`，所以要先把目录树拼出来：配置里的监控目录登记成「锚点」，
   列目录时顺手把子目录记进缓存，路径就是「锚点 + 实际目录名」；
3. 按类型分发：新增 / 接收 / 复制 → **马上写这一个文件的 strm**；删除 → 按 file_id
   找到索引行，删 strm 和索引；改名 / 移动 → 旧位置的 strm 收掉，新位置补一条；
   新建 / 复制目录 → 子文件不一定有事件，补跑一次增量；
   > 删除事件不需要父目录路径（115 删目录后 `parent_id` 就查不到了），所以「路径拼不出来」
   > 只在**非删除**事件上才算异常；一条拼不出来的删除事件不会再触发整轮目录扫描。
4. 事件接口不可用（被限流 / 接口改名）时，自动退回「目录指纹探测」：列一遍目录算
   `相对路径 + 文件 id + 大小 + sha1` 的哈希，跟库里 `last_sig` 比，变了才跑增量。
   连续失败按 1 → 2 → 4 … 分钟退避，最多 30 分钟。

好处是新片出现是「秒级」，而不是「60 秒扫一遍整棵树」。**全量 / 全新**仍然只有手动点。

三种生成语义（页面上各自一个按钮）：

- **增量**：只补新增文件，已有 strm 不动。
- **全量**：清索引重扫，strm 全部重写（内容没变不落盘）；顺带清理云端已删的残留。
- **全新**：先按索引删掉**这条配置生成过的**旧 strm（别人的文件不碰），再从头生成。

同名文件（不同季不同版本同名）会自动加 ` [pickcode后6位]` 后缀区分。

strm 内容就一行 URL：

```
http://192.168.1.10:9527/p115/play/<pickcode>/<文件名>/<115用户ID>
```

请求头 + pickcode + name + 115 用户 ID 都在里面，所以后续换盘、换账号（同 UID）仍然能解析。

### 3. 分享转存

「分享转存」→ 粘链接（支持 `?password=`、`#code`、`swXXXX-abcd` 三种写法）→ 选接收目录 → 选本地目录。

**账号、接收目录、本地目录这三项会记住**：提交过一次之后，下次打开页面（换浏览器、重启
服务都算）还是上次那套，不用每次重选。本地目录点「选」直接挑，不用手打相对路径。

流程：转存到接收目录 → 用转存结果建自己的长期分享（`share_send` + `share_update(duration=-1)`）
→ strm 指向**自己的分享**，播放时走分享通道解析直链。

- **不转存直接生成**：勾上就跳过转存与重建分享，直接用源分享。适合「本来就是我自己的分享」。
- 自己的分享被举报 / 失效时，重新跑一次即可换新分享码，strm 不用改（分享码记在配置里）。

### 4. ed2k

「ed2k」→ 粘链接（可批量，一行一条）→ 选账号、离线目录、本地目录。

账号 / 转存目录 / 本地 STRM 目录同样固定沿用上次选择（本地目录点「选」挑），批量导入也按这套默认值走。

- 添加时**默认只写 strm**，真正转存在**第一次播放时**触发（`/p115/ed2k/<hash>` 里做兜底：
  先按文件名找不到就提交离线并等最多 75 秒）。
- 想改成添加时立刻转存：页面上勾「添加时转存」，或改「服务设置」里的默认值。
- 支持导出为文本（含状态）与从文本导入。

### 5. 秒传

「秒传」两步：

1. 配置**被秒传账号**：账号 + 落盘目录（可多个）。
2. 配置**映射**：Emby 用户 → 某个被秒传账号。

该用户播放时，若文件不在映射账号里，先走 `4.0/initupload` 秒传（带 `sign_check` 二次校验，
失败就放弃，绝不降级成真上传），再取 pickcode 播放。sha1 命中过的文件会缓存，下次直接过。

**接入点在 Emby 反代的 PlaybackInfo 改写**（`rewritePlaybackInfo`）：把 MediaSource 的
`Path` 从源账号的 `/p115/play/<pickcode>/…` 换成秒传路由

```
https://<原 host>/p115/instant/<sha1>.<ext>?account=<账号 id>&size=..&name=..&source=<源账号>
```

只换路径、**保留原 scheme/host**（这个 host 来自 strm，客户端本来就能访问）。PlaybackInfo
立即返回（实测 26ms），同时起一个后台 goroutine 预热，等播放器真要数据时文件通常已经在
目标账号里了（实测 stream 388ms 就 302 到 CDN）。

这个预热可以在「115 秒传」页顶部的**预秒传**开关里关掉（存 `instant_prewarm_off`）。
关掉后 PlaybackInfo 不再提前动手，等播放器来拉 `/p115/instant` 时才同步秒传 —— 功能一样，
只是首播多等两三秒；好处是误点一下播放不会在目标账号的目录里留下副本。

`/p115/instant/{sha1}` 是新增的播放路由，秒传后取目标账号直链 302，不用中转。
`/p115/play` 直连（不经反代）没有 Emby 用户身份，不触发秒传；302 分支里还留着
`ApplyInstantForEmby` 作为兜底（老客户端缓存了改写前的 PlaybackInfo 时仍能秒传）。

秒传本身有三个坑，都写在 `internal/p115/uploadec.go` 与 `internal/instant.go` 里：

- `4.0/initupload.php` 的 body 要 AES-128-CBC 加密、`k_ec` 放 query、响应要解密 + LZ4 分帧；
- 这里的「ECDH」是**固定密钥对**——AES key/iv 和 `k_ec` 里的公钥都是写死的常量，
  自己现生成一对 P-224 密钥反而会被判 `sig invalid`（服务端用固定私钥算出的共享密钥对不上）；
- `t` 用**秒**，且必须与 `k_ec` 里编码的时间戳一致，空值参数（`appid` / `sign_key` / `sign_val`）不要出现。

`statuscode=701 + status=7` 不是错误：那是 115 在要第二轮区间校验（`sign_key` / `sign_check`），
按区间去源直链读数据算 sha1 当 `sign_val` 再提交一次即可。

几个踩出来的细节：

- 秒传成功的响应里**直接带 `pickcode`**，别再列目录轮询确认——115 建文件是异步的，轮询会让
  首播白等十几秒；
- 校验区间**只读 115 真正要的那一小段**（通常 100~200KB），不要按 1MB 整块读：115 的 CDN 对
  这种 Range 请求限速明显（实测读 1MB 要 10~20 秒），整块读反而把首播拖到 12 秒；
  只有恰好覆盖整块时才顺手写进 `instant_digest` 缓存；
- 秒传被拒先当**凭据轮换**处理（丢 `uploadKey` 换一份再试一次）；还不成就重新问源盘要
  sha1，不一样就按新 sha1 再秒传一次（源盘换过文件时这一集照样能看）。

### 6. 多 Emby 服务器

「Emby 服务器」→ 添加（名称 / 地址 / API Key，可点「测试」验证）。

这里登记的服务器有三个用处：**秒传用户映射**（读 Emby 用户列表）、**联动删除**（判断条目是否还在）、
以及**反代上游**。第一台启用的服务器作为默认值，接口里没传 `server_id` 时就用它。

### 7. Emby 反代（:7259）

不想让 Emby 直接对外、或者希望播放流量全部走 302 直连 115 时，用反代：

```
Emby 客户端 ──► http://<NAS IP>:7259 ──► 第一台启用的 Emby
```

- 端口有两个来源：**「媒体」页面手填**优先，留空则用 compose 里的 `PS_EMBY_PROXY_PORT`（默认 7259）。
  页面改端口是**热重绑**，不用重启容器；但新端口必须同时被容器映射出去（compose 的 `ports`），
  否则只改了容器内监听，局域网还是访问不到。
- 页面右上角的**开关**是反代总闸（存 `emby_proxy_off`）：关掉就停监听，端口留着，下次打开还是这个端口。
  接口是 `POST /api/emby/proxy`，`{"port":7259,"enabled":true}` 两项都可单独传。
- **媒体路由 302**：`/videos/{id}/stream`、`/audio/{id}/stream`、`/items/{id}/download|file`、
  `/sync/jobitems/{id}/file` 会先问 Emby 要 `MediaSources[].Path`（即 strm 里的 `/p115/play/...`），
  HEAD 跟一跳拿到 115 直链，再 302 给播放器。视频字节不经过本服务。
  解析结果缓存 90 秒，同一集拖动进度不会反复取链。
- **PlaybackInfo 强制 DirectPlay**：strm 源会把 `SupportsTranscoding` 关掉、删掉 `TranscodingUrl`，
  并把 `DirectStreamUrl` 改成指向反代的相对路径。不然浏览器按编码能力选 HLS 转码，302 永远不生效。
- **去 crossOrigin**：HTML 壳里注入拦截脚本，同时修补 `basehtmlplayer.js` / `plugin.js` 里的
  `crossOrigin` 赋值。302 的目标是 115 CDN，属跨域，带上 `crossOrigin="anonymous"`
  浏览器就会做 CORS 校验，115 不回 ACAO 直接播不了。
- `/System/Info` 会把 `WebSocketPortNumber` 改写成反代端口，客户端才会连回代理；
  WebSocket 与其余请求（元数据、图片、字幕）原样透传。

> 反代只认 `.strm` 里是 http 地址的条目。strm 内容指向本服务的 `/p115/play/...` 即可，
> 不需要为了反代去改 strm。

### 8. TG 通知与指令

「通知」→ 填 bot token 与 chat id → 点「测试」。任务开始 / 完成 / 失败都会推一条。

开启「允许聊天窗口发指令」后就能在同一个窗口里操作。**指令表会注册到 Telegram**，
输入框敲 `/` 直接弹菜单，不用背：

| 指令 | 作用 |
| --- | --- |
| `/help` | 全部指令，**并列出每条配置的专属指令** |
| `/status` | 运行状态、账号数、索引文件数 |
| `/accounts` | 账号列表与状态 |
| `/strm` | 各配置的文件数、上次结果与专属指令 |
| `/inc <配置或 all>` | 增量生成（只补新文件，快） |
| `/full <配置或 all>` | 全量生成（对账，会清掉已消失的） |
| `/inc_<代号>` / `/full_<代号>` | 单条配置的增量 / 全量快捷指令 |
| `/ed2k <链接>` | 添加 ed2k 任务并生成 strm |
| `/share <链接>` | 转存 115 分享并生成 strm |
| `/find <关键字>` | 搜 strm 索引 |
| `/link` | 联动删除对账 |

**直接发链接也行**，不用加指令：消息里只要有 `ed2k://` 或 115 分享链接
（`115.com/s/…`、`115cdn.com/s/…`），就按「ed2k」「分享转存」页面里的账号 / 目录设置
建任务、生成 strm，多条可以分多行一次发。普通聊天不会被回复。

每条配置都有一对专属指令，代号由配置名生成：ASCII 部分小写去符号（`LTX-VR` → `ltxvr`），
纯中文按顺序给 `c1`、`c2`（「日韩剧」→ `/inc_c1`）。代号在「115 STRM」列表、`/strm`、
`/help` 里都能看到；改了配置名代号会变，菜单也会自动重新注册。

只响应配置里的 chat id，别人给 bot 发消息不理。

**国内机器必须填代理。** 服务器直连 `api.telegram.org` 一定失败，而且域名会被污染成
`208.43.170.231` 这类假地址，表现就是「测试消息点了没反应、发指令也没回」。在「通知」页
的**共享代理**里填一个 HTTP 代理即可，例如 `http://192.168.1.34:7890`（mihomo / clash 的混合端口就是
HTTP 代理，socks5 也支持写成 `socks5://IP:1080`）。这个代理同时给 Telegram 和登录页的
TMDB 背景用，填一次即可；留空则依次回落到 compose 的 `PS_PROXY`、容器的 `HTTPS_PROXY`。

判断方法：

- 页面「通知」卡片下方会显示**最近一次失败原因**（连接超时 / 401 / 409 等原话）与最近成功时间；
- 发指令没反应时先看这里：`terminated by other getUpdates` 表示有别的程序在用同一个 bot token 拉消息；
- bot 之前设过 webhook 时 `getUpdates` 会 409，程序检测到 `webhook` 字样的报错会自动 `deleteWebhook` 一次。

### 9. 微信通知（企业微信 / 群机器人 / 服务号）

「通知」页有三条微信通道，填了哪个发哪个，全填就全发：

| 通道 | 需要的凭证 | 适用 |
|---|---|---|
| 企业微信自建应用 | CorpID + AgentID + Secret + 成员 | 企业内通知，要维护「可信 IP」 |
| 企业微信群机器人 | Webhook 或 `key=` | 最省事，拉个群就行 |
| 微信服务号（公众号） | AppID + AppSecret + 模板ID + 接收 OpenID | 直接推到个人微信，需要认证服务号 |

**服务号的两个额外前提：**

1. **IP 白名单**：公众号后台「设置与开发 → 基本配置 → IP 白名单」要加上服务器出口 IP，
   否则取 token 时报 `40164`。家宽出口会变，可以用「动态企微可信IP」插件那套思路手动维护。
2. **模板消息权限**：只有认证服务号有，订阅号 / 未认证服务号会报 `48001`。

**公网回调（可选，但强烈建议开）：**

模板消息必须按 OpenID 发，而拿 OpenID 最省事的办法就是让收件人给公众号发一条消息。
在「通知」页填好**公网地址**后，把下面这个地址填进公众号后台「服务器配置」的 URL：

```
https://<你的公网域名>/api/wechat/mp/callback
```

Token 自己定一个字符串，两边填成一样；消息加解密方式选「安全模式」时再填 43 位 EncodingAESKey
（明文模式留空即可，两种模式都支持）。保存时微信会立刻请求这个地址做校验，校验通过才算配置成功。

配好之后，任何人给公众号发消息，页面「微信服务号」卡片里就会出现
**最近收到消息的 OpenID**，点「填入接收人」即可，不用去后台翻。

服务号推送用模板消息（`api.weixin.qq.com`，国内直连，不走 TG 那个海外代理），
模板字段建议 `first`（标题）/ `keyword1`（时间）/ `keyword2`（正文）/ `remark`（备注），
模板里没有的字段微信会忽略，所以换模板不用改代码；正文超过 200 字会截断保留前半部分。

### 10. 联动删除

「联动删除」→ 先开总开关，再挑方向：

- **本地消失 → 删云端**：strm 不见了，超过宽限期（分钟）才动云端，防止媒体库扫描把手抖的误删放大。
- **云端删除 → 清本地**：115 里删了文件，本地 strm 跟着清。
- **连带删元数据**：同名 nfo / srt / ass / jpg，以及 `影片-poster.jpg`。

宽限期内的待办列在页面上，可以「确认」立即执行或「撤销」。删除操作会写事件日志。

### 11. 天翼云盘插件

「插件」→ 安装 → 启动。安装后：

- 独立进程、独立端口（自动挑空闲口），崩了不影响主服务，主服务只做反代转发。
- **装好后左侧菜单直接多出「天翼云盘」**，点开就是站内页面（`/plugin/cloud189`，
  用 iframe 内嵌插件自己的 UI），不用再跳独立页面；卸载后菜单项自动消失。
- 页面按「账号 / 配置 / 事件」三块组织：顶部四个统计卡（在线账号、配置数、已生成
  strm、最近 token 到期），账号卡可设为默认 / 立即续期 / 开关自动续期 / 退出 / 删除，
  配置卡支持增量 / 全量 / 全新三种生成，下面是事件日志（登录、续期、配置变更、生成结果）。
- 「添加配置」弹层里可以单独指定 **STRM 请求头**（留空用全局地址，内外网切换不用改每条配置）、
  **云端目录**（个人云 / 家庭云，带面包屑和目录 id）、**本地输出目录**（只在映射根里选）、
  **媒体文件扩展名**，或打开 **不过滤** 把云盘里所有文件都生成 strm；下方实时显示 STRM 内容预览。
- 配置里新增 **下载元数据文件** 开关：打开后，命中「元数据扩展名」的文件（默认
  `nfo,jpg,jpeg,png,srt,ass,ssa,sub,idx,vtt,txt`）**不生成 strm，而是直接下载到该 strm 所在的
  本地目录**；已存在且大小一致就跳过，生成结果里显示「N 个 strm · M 个元数据」。
- 生成出来的 strm 形如 `<请求头>/api/plugins/cloud189/proxy/play/<账号>[/fam/<家庭id>]/<文件id>/<文件名>`，
  末尾的文件名只给播放器认容器，取链只用 fileId，所以老地址（不带文件名）继续有效。
- 三种登录方式：扫码（本地出二维码）、Cookie（`COOKIE_LOGIN_USER` / `SESSION_KEY`）、
  手动填 accessToken；后两种没有会话密钥时不能自动续期。
- 自动续期：启动 20 秒后扫一遍、之后每 30 分钟一次，token 剩不到 24 小时就用会话密钥
  换新的；关闭开关的账号不参与。
- 登录二维码在插件本地算矩阵、直接出 SVG（`plugins/cloud189/qr.go`），不依赖任何
  外部二维码服务，内网/断网也能扫；矩阵与参考实现逐位一致。
- 播放：`.strm` 指向插件的 `/play/...`，插件现取直链、跟一跳 302 拿到 CDN 真实地址后
  再 302 给播放器，直链缓存 120 秒（一次起播的多个 Range 只换一次链）。
- 未安装时不占端口、不跑进程。
- 卸载只删数据目录里的副本，内置插件源还在，随时能重装。

---

## API 速查

登录用 `/api/login`（拿 `ps_session` cookie），其余接口除了播放入口都要会话。

```
POST   /api/login                          POST /api/logout            GET /api/me
GET    /api/accounts                       POST /api/accounts/qr       GET /api/accounts/qr/{uid}
POST   /api/accounts/cookie                PATCH/DELETE /api/accounts/{id}
POST   /api/accounts/{id}/refresh          POST /api/accounts/{id}/default
GET    /api/dirs?account_id=&cid=           （目录选择器）

GET/POST /api/strm/configs                 PATCH/DELETE /api/strm/configs/{id}
POST   /api/strm/configs/{id}/run?mode=inc|full|rebuild
GET    /api/strm/configs/{id}/files        GET /api/files?q=

GET/POST /api/shares                       DELETE /api/shares/{id}
GET/POST /api/ed2k                         DELETE /api/ed2k/{id}
GET    /api/ed2k/export                    POST /api/ed2k/import
GET/POST /api/tasks/defaults                （分享 / ed2k 上次用的账号与目录）
GET/POST /api/instant/targets              DELETE /api/instant/targets/{id}
POST   /api/instant/mappings               DELETE /api/instant/mappings/{id}

GET/POST /api/emby/servers                 DELETE /api/emby/servers/{id}
POST   /api/emby/servers/test              GET /api/emby/users
GET/POST /api/settings                     GET /api/stats
GET/DELETE /api/events
GET    /api/link/deletions                 POST /api/link/scan
POST   /api/link/deletions/{id}/confirm    DELETE /api/link/deletions/{id}
POST   /api/tg/test                         GET /api/tg/status
POST   /api/wechat/test[?only=mp]           GET /api/wechat/status
GET/POST /api/wechat/mp/callback            （公开，公众号后台回调，靠签名校验）
GET    /api/plugins                        POST /api/plugins/{id}/install|start|stop
DELETE /api/plugins/{id}                   ANY  /api/plugins/{id}/proxy/*

GET    /p115/play/{pickcode}/{name}/{uid}  （302 或中转，无需登录）
GET    /p115/share/{code}/{file}           （302 或中转）
GET    /p115/ed2k/{hash}                   （302 或中转）
ANY    /emby/*
```

---

## 常见问题

**`docker compose build` 报 `process "/bin/sh -c go mod download" did not complete successfully: exit code: 1`？**
容器里连不上 `proxy.golang.org`（国内网络必现，`go.sum` 本身是完整的，不用怀疑依赖）。
先用 `docker compose build --progress=plain` 看完整的报错行——默认输出只给一行 exit code，
真正原因是它上面那几行 `dial tcp ... i/o timeout`。
Dockerfile 已默认把 `GOPROXY` 指向 `goproxy.cn`、`GOSUMDB=off`，日志里会先打印一行
`go env GOPROXY GOSUMDB GOTOOLCHAIN`，先确认它显示的是镜像源。还要覆盖就在构建时传：

```bash
# 用别的代理
GOPROXY=https://goproxy.cn,direct docker compose build
docker build --build-arg GOPROXY=https://proxy.golang.org,direct -t panshare .
# npm 拉不动同理
NPM_REGISTRY=https://registry.npmmirror.com docker compose build
```

如果换源后还是超时，多半是容器 DNS 不通：Linux 上加 `--network=host` 构建
（`docker build --network=host ...`），或 Docker Desktop → Settings → Docker Engine 里
配 `"dns": ["223.5.5.5"]` 后重启。

**播放报「解析直链失败：115 没有返回下载地址」/ `50028 文件大小超出限制`？**
115 的明文下载接口（`GET /files/download`）只放行 200MB 以内的文件，视频基本都超，
会返回 50028「请使用115电脑端下载」。PanShare 走的是 115 的**加密通道**
（`POST /files/download`，参数是 RSA 密文），没有这个限制，视频、几十 GB 的 ISO 都能取到。
如果还是失败，多半是账号 cookie 过期或被风控：

```bash
# 在容器里直接跑一遍取直链，能看到每一步的原始响应
docker exec panshare-go /app/apiprobe -db /config/panshare.db -acc 1 -mode creds
# 秒传：一次试四种参数组合，看 115 认哪一种
docker exec panshare-go /app/apiprobe -db /config/panshare.db -acc 1 -mode init -cid <目录 id>
# 秒传：跑完整链路（源账号取校验块 → 目标账号秒传 → 列目录）
docker exec panshare-go /app/apiprobe -db /config/panshare.db -acc 0 -mode instant -pick <源 pickcode> -to 1 -cid <目录 id>
docker exec panshare-go /app/p115probe -db /config/panshare.db -pickcode <pickcode>
```

**为什么有的播放是 302，有的却是「中转」？**
115 的直链带 `f` 参数，这是判定依据：

| f | CDN 校验什么 | 本服务怎么处理 |
| --- | --- | --- |
| `1` | 只校验 UA | **302 直连**，视频流量不经过本机 |
| `3` | 还要「取链接那次响应」下发的 cookie | 302 带不过去，只能代拉字节流（保留 Range，可拖动） |

关键在于**从哪条通道取链接**——同一条视频，走不同通道拿到的 `f` 完全不同：

| 通道 | 请求 | 下发的 f |
| --- | --- | --- |
| 安卓客户端 | `POST proapi.115.com/android/2.0/ufile/download`，`{"pick_code":...}` | `f=1` |
| 电脑/网页端 | `POST proapi.115.com/app/chrome/downurl` 或 `webapi.115.com/files/download` | `f=3` |

PanShare **默认先走安卓通道**（payload 用 `pick_code`，鉴权仍靠账号 Cookie），
拿到的就是 `f=1`，所以视频能真正 302 到 CDN。只有安卓通道失败时才回落到
chrome / web 通道，那时才会变成中转。

实测（24GB 视频）：`/p115/play/...` → `302` → 在另一台机器上带同一个 UA 请求 CDN →
`206 Partial Content`，`Content-Range: bytes 0-2047/24244369009`。

> 注意 UA 必须一致：`f=1` 的链接**只认取链接时那个 UA**（这是 115 的防盗链）。
> PanShare 取链接用的是「播放器自己发来的 UA」，所以正常情况下天然对齐；
> 但如果客户端压根不发 UA，302 过去会被 CDN 回 403，这种情况会自动改走中转。

**视频还是走中转，怎么排查？**
在容器里跑探针，能直接看到当前通道下发的 f 值：

```bash
# 看安卓通道（默认）
docker exec panshare-go /app/p115probe -db /config/panshare.db -pickcode <pickcode>
# 对比电脑端通道
docker exec panshare-go /app/p115probe -db /config/panshare.db -pickcode <pickcode> -api chrome
```

播放日志里出现 `播放：302 直连 115 ← <UA>` 就说明没走中转；
出现 `播放：中转（115 直链要 cookie，f=3）` 说明安卓通道没成功，看一下探针输出的原因。

**strm 末尾的 uid 为什么只有几位数字？**
115 的 UID cookie 是 `2806858_R1_1794486118` 这种复合串，后面是客户端标识和时间戳，
换个客户端登录就变。PanShare 统一只取前面的数字，老库里存成复合串的会在启动时自动剪掉，
旧 strm 里带着复合串也照样能播（解析时会再剪一次）。

**生成到一半大量失败，日志里是 405？**
115 的 WAF 限流。PanShare 默认每秒 2 个请求并带退避重试，还是被打就上「服务设置」把
`api_qps` 降到 1，或把配置拆小一点分几次跑。

**strm 生成了，Emby 里点播 404 / 无权限？**
先确认 `PS_PUBLIC_BASE_URL` 是**播放器视角**能访问的地址（不是容器内网）。用浏览器直接打开
一个 strm 里的链接：能 302 到 115 就说明后端没问题，剩下的就是播放器网络与 Emby 挂载路径。

**映射根改了，已生成的 strm 怎么办？**
「服务设置」改映射根后，旧配置的 `local_dir` 还是相对路径，跑一次**全量**即可在新根下重建。

**删了 115 里的文件，本地没清？**
联动对账 10 分钟一轮，也可以在「联动删除」里点「立即对账」，或 TG 发 `/link`。

**想彻底重来？**
停容器 → 删 `./config/panshare.db*` → 起容器。`./strm` 里的文件需要自己清（用「全新」模式逐条清更安全）。

---

## 说明

- 项目依赖 115 官方 Web 接口，接口变了可能需要跟进；cookie 会过期，过期会在账号卡片上标红。
- 仅供个人备份与自有媒体库使用，请遵守 115 与所在地的相关规定。

### 115 接口变更备忘（2026-11）

| 功能 | 现在走哪 | 说明 |
| --- | --- | --- |
| 提交离线任务 | `POST https://clouddownload.115.com/lixianssp/?ac=add_task_url` | `webapi.115.com/offline/*` 已下架（404）；`115.com/web/lixian` 老网关对第三方客户端只回「Not Found / 错误的链接」。现在跟官方客户端一样走 app 通道：body 只有一个 `data`（m115 编码的 `{ac,app_ver,url,wp_path_id}`），UA 必须是 `Mozilla/5.0 115disk/<ver> 115Browser/<ver> 115wangpan_android/<ver>` |
| 离线任务查询 | `GET https://clouddownload.115.com/web/?ac=get_user_task&info_hash=<hash>` | 单查的 `status` 数字和列表不是一套（同一任务列表 2=完成、单查 1=完成），只看 `display_status` / `status_text` |
| 离线任务列表 | `GET https://clouddownload.115.com/web/?ac=task_lists&page=1&page_size=100&stat=0` | 列表里 `status=2` 才是完成，`3` 失败 |
| 删除离线任务 | `POST https://clouddownload.115.com/web/?ac=task_del` | 表单 `hash[0]=<info_hash>`，只删任务记录，不动已转存的文件 |
| 分享直链 | `POST https://proapi.115.com/app/share/downurl?t=<秒>` | 网页版 `/share/download` 对第三方客户端一律回「服务器开小差了」；body 只有一个 `data`，是 m115 编码（16 字节随机密钥 + XOR + RSA-1024 分段）的 `{share_code,receive_code,file_id}`，且必须用 **115 客户端 UA**（`Mozilla/5.0 115Browser/27.0.5.7`），返回值同样要 m115 解码 |
| 分享目录 | `https://webapi.115.com/share/snap` | 暂未变，仍然可用 |
| 秒传初始化 | `POST https://uplb.115.com/4.0/initupload.php?k_ec=<token>` | 路径必须带 `.php`（少了就是 nginx 404）。body 是 AES-128-CBC 密文，响应要解密 + LZ4 分帧；key/iv 与 `k_ec` 里的公钥都是写死的常量；`t` 用秒；`statuscode=701 + status=7` 表示要第二轮区间校验，不是失败 |
| 上传凭据 | `GET https://webapi.115.com/files/uploadinfo`（同 `POST https://proapi.115.com/app/uploadinfo`） | 两者返回的 `userkey` 一致；`uplb.115.com/3.0/getuploadinfo.php` 只有 endpoint，没有 userkey |

m115 编解码在 `internal/p115/m115`（移植自 SheltonZhu/115driver，MIT），排障可以直接跑
`apiprobe`：`apiprobe -db /config/panshare.db -mode share -share swXXX -rc abcd -file 3566...`。
