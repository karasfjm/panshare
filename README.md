# PanShare

> 115 网盘 strm 生成与播放服务 · 当前版本 **1.23.0**

把 115 网盘里的视频变成 Emby / Jellyfin 能直接播的 `.strm`：播放时本服务换一条 115 直链再 302 给播放器，**视频流量不经过 NAS**。生成、转存、秒传、反代、通知都在一个容器里，不需要 alist / emby2Alist / Caddy。

> 项目原名 `p115-strm`，更名后仓库内模块路径与环境变量名保持不变，老配置可以直接沿用。

## 目录

- [1. 功能一览](#1-功能一览)
- [2. 快速开始](#2-快速开始)
- [3. 网页界面](#3-网页界面)
- [4. 115 助手：账号管理](#4-115-助手账号管理)
- [5. 115 STRM：按目录生成](#5-115-strm按目录生成)
- [6. ed2k STRM](#6-ed2k-strm)
- [7. 115 分享转存](#7-115-分享转存)
- [8. 115 秒传](#8-115-秒传)
- [9. Emby 反代](#9-emby-反代)
- [10. 插件](#10-插件)
- [11. 配置项](#11-配置项)
- [12. 增量同步与限速](#12-增量同步与限速)
- [13. 运维与排错](#13-运维与排错)
- [14. 构建镜像](#14-构建镜像)
- [15. 本地调试](#15-本地调试)
- [16. 已知限制](#16-已知限制)

---

## 1. 功能一览

| 能力 | 说明 | 入口 |
| --- | --- | --- |
| **115 STRM** | BFS 遍历网盘目录，为视频生成 `.strm`；增量只补新增 / 改名，全量重写，全新先删后建 | 工具 → 115 STRM |
| **增量同步** | 优先用 115「生活事件」实时跟随；事件接口被限流时自动降级为「按目录 mtime 的指纹扫描」 | 自动 |
| **ed2k STRM** | 粘贴 ed2k 链接立刻得到 strm，**播放时才转存**；支持导入 / 导出备份 | 工具 → ed2k STRM |
| **115 分享转存** | 别人分享的链接 → 转存成自己的长期分享 → 生成 strm；也可不转存直接指向原分享 | 工具 → 115 分享转存 |
| **115 秒传** | 播放时把文件秒传到第二个账号（按 Emby 用户分配），用该账号直链播放 | 工具 → 115 秒传 |
| **多账号** | 任意多个 115 账号，各自扫描、各自直链；所有账号共用同一个 `STRM_ROOT` | 工具 → 115 助手 |
| **同播复制** | 多设备同时看同一片时，后来者拿一份独立副本、独立直链，不互抢带宽 | 账号设置（账号级） |
| **Emby 302 反代** | 改写 PlaybackInfo 为直链；支持 strm / pickcode / FileId / ed2k / 自定义路径规则 | 媒体与反代 → Emby 反代 |
| **天翼云盘** | 插件。多账号扫码登录、token 自动续期、家庭云，播放时实时换直链 | 工具 → 天翼云盘 |
| **通知与联动** | 插件。企微 / Telegram / Webhook 推送 + 指令；双向联动删除 | 系统 → 通知与联动 |
| **企微可信 IP** | 插件。公网 IP 变了自动写进企微自建应用的可信列表 | 系统 → 企微可信 IP |
| **登录页背景** | 随机 TMDB 热门剧照，网页上配 Key / 代理 / 自定义图 | 运行状态 → 登录页背景 |

---

## 2. 快速开始

### 2.1 docker compose（推荐）

```bash
git clone <this-repo> panshare && cd panshare

mkdir -p config strm
# cookies 可以先不填：服务照常启动，起来后在网页「115 助手」里粘贴即可
# （想省一步就写进这个文件，见 4.1）
# echo 'UID=xxx; CID=xxx; SEID=xxx; KID=xxx' > config/115-cookies.txt

# 编辑 docker-compose.yml：PUBLIC_BASE_URL 改成播放器能访问到的地址
docker compose up -d --build
docker compose logs -f
```

### 2.2 docker run

```bash
docker run -d --name panshare --restart unless-stopped \
  -p 9527:9527 -p 7259:7259 \
  -v $PWD/config:/config \
  -v /your/media/strm:/strm \
  -e PUBLIC_BASE_URL=http://192.168.1.10:9527 \
  -e UI_USERNAME=admin -e UI_PASSWORD=your-password \
  panshare:latest
```

启动后打开 `http://<host>:9527/`，用 `UI_USERNAME` / `UI_PASSWORD` 登录。仓库 compose 里带的是示例值，**部署前务必改成自己的**。

> **首次运行不需要 115 cookies。** 没填 cookies 时服务照常启动，网页会提示「还没有可用的 115 账号」，去「工具 → 115 助手」把 cookies 整串粘进去、点保存就生效（立即校验登录态，不用重启容器）。账号名可以留空，自动叫「115」。

### 2.3 启动后要做的三件事

1. **改 `PUBLIC_BASE_URL`**：这是写进 `.strm` 的地址前缀，必须是**播放器 / Emby 能访问到**的地址。写 `127.0.0.1` 或 `172.17.0.1`（docker 网桥）电视盒子连不上，表现就是播放转圈。
2. **加账号**：「工具 → 115 助手」粘贴 cookies（账号名可留空），保存时自动校验登录态。
3. **加目录**：「工具 → 115 STRM」选账号、选网盘目录、选本地目录，保存后点「增量」开始生成。

默认不监控任何目录（`ROOT_CIDS` 留空），加什么生成什么，不会一上来就扫整个网盘。

---

## 3. 网页界面

登录后是单页管理界面，左侧按四组排列：

| 分组 | 页面 | 作用 |
| --- | --- | --- |
| —— | **运行状态** | 索引 / 扫描 / 增量 / 各账号状态，手动触发全量与增量；登录页背景设置 |
| 工具 | **115 助手** | 增删账号、改显示名、替换 cookies、测 cookie、账号级同播复制 |
| 工具 | **115 分享转存** | 粘分享链接 → 转存 → 生成 strm |
| 工具 | **ed2k STRM** | 粘 ed2k 链接批量生成；转存目录 / 保留时长 / 转存时机；导出导入 |
| 工具 | **115 秒传** | 秒传账号勾选、Emby 用户映射、暂存目录、预缓存、缓存清理 |
| 工具 | **115 STRM** | 生成配置卡片（账号 + 网盘路径 → 本地目录），增量 / 全量 / 全新 |
| 媒体与反代 | **媒体服务器** | 登记多个 Emby（名称 / 地址 / API Key） |
| 媒体与反代 | **Emby 反代** | 上游、监听端口、前缀、缓存、路径规则，改完立即生效 |
| 系统 | **插件** | 天翼云盘 / 通知与联动 / 企微可信 IP 的安装、停用、卸载 |

要点：

- **账号只有一个来源**：115 助手。其它页面的账号选择器都从这里读，选中后保存为该工具的默认账号（存服务端，换浏览器也在）。
- **账号名分两层**：`账号标识` 是内部名，出现在 strm 目录与旧版播放地址里，不能改；「设置」里改的是**显示名**，随便改。
- **登录只保护管理接口**：播放路由（`/p115/…`、`/p189/…`）与 Emby 反代不受登录影响，播放器照常访问。`UI_PASSWORD` 留空则不启用登录。

### 登录页背景（TMDB）

登录页会随机取一张 TMDB 热门影片的剧照当背景。配置在「运行状态 → 登录页背景（TMDB）」里：

| 项 | 说明 |
| --- | --- |
| API Key / Read Token | TMDB v3 Key 或 v4 Read Access Token，二选一 |
| 代理 | 只作用于 TMDB 的片单与图片请求，115 接口不走它 |
| 自定义图片直链 | 填了就用这些图，不再请求 TMDB（逗号分隔） |

保存即生效（存数据库，不用改 compose、不用重建容器），网页上填的值**优先于环境变量**；留空则继续用 `TMDB_API_KEY` / `TMDB_READ_TOKEN` / `TMDB_PROXY` / `LOGIN_BACKDROPS`。旁边的「测试拉取」会直接告诉你拿到了几张、还是失败原因。

> 容器在国内网络下直连 `api.themoviedb.org` / `image.tmdb.org` 会 SSL 握手超时（宿主机的透明代理管不到 docker bridge 的流量），这时在代理一栏填宿主机的代理地址，如 `http://192.168.1.10:7890`。都没有配置时登录页保持原来的渐变背景。

---

## 4. 115 助手：账号管理

### 4.1 获取 115 cookies

1. 浏览器登录 <https://115.com>；
2. F12 → Network → 随便点一个 `webapi.115.com` 请求 → Request Headers → 复制整行 `Cookie`；
3. 只需要以下几个键：

```
UID=<你的 UID>; CID=<一串十六进制>; SEID=<一串十六进制>; KID=<一串十六进制>
```

cookies 过期后日志会出现 `登录校验失败` / `405`，重新导出即可。在网页上替换 cookies 会写回 `P115_COOKIE_FILE` 指向的文件，下次重启继续用；`P115_COOKIES` 环境变量的值只在启动时读一次。

> 可以不落盘：用 `P115_COOKIES` 直接给整串（compose 里的 `environment`）。两者都没有也能启动，见 2.2 的说明。

### 4.2 添加与维护

| 操作 | 说明 |
| --- | --- |
| **添加账号** | 账号名可留空（自动叫「115」/「115-2」…），粘贴 cookies 保存时自动校验登录态 |
| **改显示名** | 「设置」里改，不影响 strm 目录与播放地址 |
| **查看 / 替换 cookies** | 整串粘贴即可替换，先校验再写入，立即生效不重启 |
| **测试 cookie** | 测的是**输入框里的内容**，可以先改再测，不保存也能试 |
| **删除账号** | 只清理本服务的 strm 与索引，**不动网盘文件** |

每个账号的索引库独立（`DATA_DIR/accounts/<账号名>/`），全量 / 增量线程独立，限速也按账号独立计算（WAF 是按账号风控的）。

播放地址不带账号前缀：`/p115/play/<pickcode>/<文件名>/<uid>`，用**该账号自己的 cookies** 换直链；老的 `/p115/a/<账号名>/play/...` 仍然可用。各工具的接口按账号分派到 `/a/<账号名>/api/…`。

### 4.3 同播复制（账号级）

两台设备同时看**同一个文件**时，默认共用同一个 pickcode 和同一条直链 —— 115 对单文件的并发拉流是共享的，结果就是互相抢带宽、来回卡。同播复制会给后来者一份**独立的 115 副本**。

它是**账号级设置**：开关放在「115 助手 → 账号 → 设置 → 同播复制」，**这个账号出去的所有播放**（普通 strm、ed2k，以及任何走该账号的请求）都按同一套规则判断，每个账号的参数互不影响。

| 项 | 默认 | 说明 |
| --- | --- | --- |
| 开关 | 开 | 关掉就始终复用主文件 |
| 保留时长 | 30 分钟 | 从**最后一次播放**算，到点连 115 文件一起删（下限 5 分钟） |
| 每片副本上限 | 3 | 到上限后新设备沿用主文件（上限 10） |

行为：

- 第 1 台设备用原文件，第 2 台起在 115 里复制一份，各自拿独立 pickcode 与直链；
- 副本放在 115 **根目录下的 `PanShare同播副本`**（首次用到时自动创建），扫描器会跳过它，不会把副本当成新片再索引或再生成 strm；
- 账号卡片上显示「同播复制」标签和当前副本数；设置里能看副本数与占用，也能一键「清理过期副本」；
- 复制只是优化：**任何一步失败都静默回退主文件**，不会让播放失败。

判定「另一台设备」用的是**来源 IP + User-Agent**：

- Direct Play 时播放器直接请求本服务，IP / UA 就是设备自己的 —— 能区分，正常工作；
- Emby **服务端转码拉流**时所有请求都来自 Emby 容器，指纹相同 —— 这种情况不会触发复制（服务端确实分不清是谁在看）；
- 同一个播放器反复 seek、重开，指纹不变，不会重复复制。

```bash
# 查 / 改某个账号的同播复制（路径里的 main 指服务自身的主账号，其它账号用账号名）
curl "http://127.0.0.1:9527/api/accounts/main/copies"
curl -X POST "http://127.0.0.1:9527/api/accounts/main/copies?enabled=1&ttl_minutes=30&max_copies=3"
curl -X POST "http://127.0.0.1:9527/api/accounts/main/copies/clean"
```

---

## 5. 115 STRM：按目录生成

### 5.1 一条配置包含什么

一条配置 = 「某个账号的某些网盘目录」→「本地的一些目录」。弹窗字段：

| 字段 | 说明 |
| --- | --- |
| **账号** | 来自 115 助手 |
| **任务名称** | 卡片标题，例如「电影」「国漫」 |
| **网盘路径 → 本地 STRM 目录** | 可以加多组，**一个网盘路径绑一个本地目录**；本地目录默认预填「映射根 + 网盘目录名」，点行里的「选」可改成映射根下任意目录 |
| **STRM 请求头** | 写进 strm 的基地址，留空用全局 `PUBLIC_BASE_URL`；每条配置可以不同 |
| **媒体文件扩展名** | 随配置保存；**实际后缀过滤以全局 `STRM_EXT` / `STRM_ALL_FILES` 为准** |
| **元数据文件扩展名 / 下载元数据** | 随配置保存，当前版本不会下载元数据文件 |
| **事件监控** | 随配置保存；事件监听是全局的（`EVENT_ENABLED`） |
| **定时巡检** | 按间隔自动跑一次**增量**（详见 5.4），这一项是按配置生效的 |

strm 内容：

```
<请求头>/p115/play/<pickcode>/<URL 编码的文件名>/<115 用户 ID>
```

末尾那串数字是该文件所属账号的 **uid**，用来判断这条 strm 属于哪个账号，所以播放地址里不再需要 `/a/<账号名>/`。

### 5.2 映射根与服务设置

115 STRM 页面右上角「服务设置」里有四项，都是**新建配置时的默认值**（已有配置不受影响）：

| 项 | 说明 |
| --- | --- |
| 默认 STRM 请求头 | 新配置的「请求头」预填这个值 |
| **STRM 映射根目录** | 默认 `STRM_ROOT`（如 `/strm`），本地目录只能落在这个根内 —— 选目录、填目录都过这层校验，不会写到别处 |
| 扫描并发 | 新配置的默认 worker 数 |
| 定时巡检间隔（秒） | 新配置打开「定时巡检」时的默认间隔 |

网盘 `/影音/国漫` 默认落到 `映射根/国漫`（即 `/strm/国漫`），也可以改成 `/strm/影音/国漫`。生成前会自动建目录，不用手工 mkdir。

### 5.3 三种生成模式

| 模式 | 行为 | 什么时候用 |
| --- | --- | --- |
| **增量** | 只补新增 / 改名的文件，已有 strm 不重写 | 日常 |
| **全量** | 清索引重扫，**strm 全部重写一遍** | 怀疑索引不对、strm 被部分删掉 |
| **全新** | 先删掉**这条配置生成过**的旧 `.strm`（nfo / 字幕 / 海报保留），再从头生成 | 目录大改、想彻底重来 |

> 「全新」只删这条配置索引里记录过的路径，并且限制在 `STRM_ROOT` 内、只删 `.strm`。手放进目录的 strm、别的配置的 strm 都不会被误删。

点按钮后卡片状态自动刷新（运行中 → 完成）。每条配置还会记录事件日志（生成开始 / 完成 / 重写多少 / 补写多少 / 失败原因），刷新页面不丢，页面底部可以查看和清空。

### 5.4 定时巡检

配置弹窗里可单独打开「定时巡检」并设间隔（分钟）：到点自动跑一次**增量**，只补新增 / 改名，不会自动跑全量 / 全新。

- 最小 1 分钟；
- 同一时刻只跑一条配置，多条同时到点会排队；
- 手动全量正在跑时会让路，避免把 115 刷成 405；
- 卡片上显示「定时 N 分钟」标签；
- 调度器每 15 秒看一次有没有到点的配置，状态可查 `GET /api/strm/scheduler`。

### 5.5 生成结果与命名

本地目录结构与网盘完全一致：

```
/strm/电影/2024/沙丘2.2024.2160p.mkv   ← 网盘上的文件
/strm/电影/2024/沙丘2.2024.2160p.strm  ← 生成的文件，内容是一行 URL
```

同一目录下若出现同名不同后缀（`歌.lrc` 与 `歌.m4a`，或 `a.mkv` 与 `a.mp4`），后一个写成 `歌.m4a.strm`，不会互相覆盖。

**删除配置**只是移除这条配置（不再巡检、不再生成），**不会删掉磁盘上已有的 `.strm`**，也不动网盘文件。想连文件一起清掉，用「全新」或手工删目录。

### 5.6 接口

```bash
curl http://127.0.0.1:9527/api/strm/configs                    # 列出配置
curl -X POST http://127.0.0.1:9527/api/strm/configs \
  -H 'Content-Type: application/json' \
  -d '{"account":"","name":"电影","path_dirs":[{"cid":"3100000000000000001","path":"/电影","dir":"电影"}],"base_url":""}'
curl -X POST http://127.0.0.1:9527/api/strm/configs/<id>/run    # 跑一次（inc / full / rebuild）
curl -X DELETE http://127.0.0.1:9527/api/strm/configs/<id>
curl "http://127.0.0.1:9527/api/115/dirs?cid=0"                 # 浏览网盘目录
curl "http://127.0.0.1:9527/api/local/dirs?path=/strm"          # 浏览映射目录
```

---

## 6. ed2k STRM

### 6.1 只写 strm，播放才转存

粘 ed2k 链接后**立刻写 strm**，是否马上转存由「添加后立即转存」开关决定：

| 开关 | 行为 |
| --- | --- |
| **关闭（默认）** | 不建目录、不提交离线任务，几百条链接也不占网盘空间；**播放到该片时才转存** |
| **打开** | 添加时立即提交 115 离线下载到「转存目录」，首次播放不用等 |

无论哪种，播放时文件不在都会当场转存（等它就位再 302，最多等 `ED2K_PLAY_WAIT` 秒，超时返回 503，播放器重试即可）。

任务状态：

| 状态 | 含义 |
| --- | --- |
| `idle` **待播放转存** | strm 已生成，还没碰过 115 |
| `ready` **就绪** | 文件在转存目录里，可直接播 |
| `pending` **转存中** | 离线任务已提交，等落地 |
| `failed` **失败** | 离线任务失败（源失效 / 配额不足等，列表里会写原因） |
| `cleared` **已清理** | 文件按保留时长删掉了，下次播放重新转存 |

strm 内容是一个**稳定路由**：

```
http://192.168.1.10:9527/p115/ed2k/<ed2k hash>
```

为什么不直接写 pickcode：转存目录会被定期清空，重新转存后 pickcode 会变，写死的地址就 404 了。指向本服务、播放时再解析，strm 永远不用改。

### 6.2 用法

1. 打开「工具 → ed2k STRM」；
2. **选账号**（来自 115 助手），保存后作为默认账号；
3. **转存目录**（三种方式任选）：
   - 点「浏览网盘目录」逐层选，或填目录 id / 粘 `https://115.com/?cid=...` 后点「设为转存目录」；
   - 点「自动创建」在网盘根目录建默认目录；
   - 什么都不设：首次用到时自动在根目录建 `p115-strm-ed2k`；
4. **保留时长 / 检查间隔**：改完点「保存时长/间隔」，即时生效；
5. **转存时机**：「添加后立即转存」开关，改完立即生效；
6. **本地输出目录**：默认 `STRM_ROOT/ed2k`，可改成映射目录下任意子目录；
7. 文本框粘贴链接（**每行一条**，也可以粘一整段带说明的文字，只认 `ed2k://` 行），点「添加（可批量）」。

批量添加会分片提交并显示进度（「提交中… 8/30」）：网页每 8 条一批发给后端，后端逐条提交（每条间隔 0.2 秒，避免 115 对突发请求敏感），最后统一刷一次状态，给出「成功 N / 失败 M」和失败原因。相同 hash 自动去重，单批最多 200 条。

### 6.3 播放与清理

- 播放时文件在 → 直接 302 / 中转；
- 文件已被清理 → 自动重新提交离线下载（115 有缓存时几秒完成）；
- 后台每 `ED2K_CLEAN_INTERVAL` 秒检查一次，把「最后一次播放」超过 `ED2K_TTL_HOURS` 小时的文件从转存目录删掉（`0` = 不自动清理）；
- 「清理过期」立即按保留时长清一次；「清空目录」清空整个转存目录 —— **包括不是本服务放进去的文件**，慎用；
- 删除单条任务会删 strm，也会删掉 115 上那份转存文件，但不动别处的同名文件。

### 6.4 导出 / 导入（换设备）

任务列表存在容器里的 SQLite，换机器不用重新粘链接。

- 「导出备份」→ 下载 `panshare-ed2k-YYYYMMDD-HHMM.json`，含全部任务 + 当前配置（默认带 `file_id` / `pickcode`）；
- 新设备「导入备份」→ 按 ed2k hash 去重合并，已存在的跳过；缺失的 strm **自动重建**；
- 同一个 115 账号换机器：带 id 导入后第一次播放就能直接命中，不用重新转存；
- 换了**另一个账号**也没关系：id 失效的记录会被置成「待播放转存」，播放时自动重新转（导出时用 `?include_files=0` 可以干脆不带 id）。

导入模式（网页只用 `merge`，接口可选）：

| mode | 行为 |
| --- | --- |
| `merge` | 只加新的，已有记录不动 |
| `update` | 覆盖已有记录的链接与名字 |
| `replace` | 先清空本地记录再全量导入（**不删 115 上的文件**） |

### 6.5 注意

- 离线下载消耗 115 的**离线配额**，部分 ed2k 源本身已失效；
- 转存目录建议**专用**，别和日常文件混在一起；
- 并发播放同一部片只会提交一次转存（30 秒内重复请求直接复用），不会连开多个离线任务。

### 6.6 接口

```bash
curl -X POST "http://127.0.0.1:9527/api/ed2k?url=ed2k://|file|xxx.mkv|123|ABCD...|/"
curl -X POST --data-urlencode "urls=<多行 ed2k 链接>" -G "http://127.0.0.1:9527/api/ed2k/batch"
curl "http://127.0.0.1:9527/api/ed2k"                       # 列表 + 当前设置
curl -X POST "http://127.0.0.1:9527/api/ed2k/refresh"       # 立刻刷新转存状态
curl -X POST "http://127.0.0.1:9527/api/ed2k/settings?ttl_hours=12&clean_interval=900"
curl -X POST "http://127.0.0.1:9527/api/ed2k/settings?auto_transfer=false"
curl -X POST "http://127.0.0.1:9527/api/ed2k/settings?dir_cid=3027523183514719412"
curl -X POST "http://127.0.0.1:9527/api/ed2k/clean?force=false"
curl -X DELETE "http://127.0.0.1:9527/api/ed2k/<hash>"

# 导出 / 导入
curl "http://127.0.0.1:9527/api/ed2k/export?include_files=1" -o ed2k-backup.json
curl -X POST "http://127.0.0.1:9527/api/ed2k/import?mode=merge" \
     -H "Content-Type: application/json" --data @ed2k-backup.json
```

---

## 7. 115 分享转存

把别人分享的链接（带提取码、快过期都行）变成自己网盘里可长期播放的 strm。

两种模式：

| 模式 | 行为 | 适合 |
| --- | --- | --- |
| **转存**（默认） | 递归列出分享内容 → 整体转存到「接收目录」→ 用转存好的顶层条目**创建自己的长期分享**（`share_duration=-1`）→ strm 指向 `/p115/share/<我的分享码>/<文件id>` | 想长期保留 |
| **直接生成** | 不转存，strm 直接指向**原分享**，播放时用原分享的提取码解析直链 | 只是临时看、不想占自己网盘 |

为什么转存后还要走自己的分享：播放流量走分享下载通道，不占账号的文件直链，也不把账号 cookies 暴露在播放请求里。

细节：

- 支持 `https://115.com/s/xxx?password=yyy`、`https://115cdn.com/s/xxx#yyy`、`xxx-yyy` 等写法；
- 同一个分享重复添加会幂等跳过转存、复用已有的自己的分享；之前的转存文件进了回收站会自动还原再定位；
- 删除记录时会一并取消自己的分享，并可选删除 115 上的转存文件（「直接生成」的记录不删别人的文件）；
- **内容读不全会说清楚**：115 前面有阿里云 WAF，递归遍历大分享容易被 405 拦，程序会退避重试；万一没遍历完，页面会写「内容读取不完整（已转存 N 个文件…）」，不会假装成功，点「重新定位」可以补齐。

```bash
curl -X POST "http://127.0.0.1:9527/api/share?url=https://115.com/s/xxx?password=yyy"
curl -X POST "http://127.0.0.1:9527/api/share?url=...&direct=true"   # 不转存，直接生成
curl "http://127.0.0.1:9527/api/share"
curl -X POST "http://127.0.0.1:9527/api/share/refresh"
curl -X DELETE "http://127.0.0.1:9527/api/share/<分享码>"
```

---

## 8. 115 秒传

### 8.1 它解决什么

第二个 115 账号（家人号 / 小号）也想用主号的资源：播放时把文件**秒传**到该账号（115 按 sha1 识别文件，不用上传数据），再用那个账号的直链播放。

在「工具 → 115 秒传」页：

1. **勾选秒传账号**：账号直接来自 115 助手，可多选；只有勾选的账号能作为秒传目标（不勾 = 全部可用）。勾选后点右上角「保存」。
2. **映射 Emby 用户**：选用户 → 选秒传账号。下拉里是「已知用户」（Emby 客户端登录过一次就有了，取不到时才退回登录过的用户），也可以手填 Emby 用户 ID。
3. **暂存目录**：默认 `INSTANT_DIR_NAME`（网盘根下的 `p115-strm-instant`），可改成任意网盘目录；可一键清空（下次播放自动重新秒传）。
4. **预缓存**：一键把各账号索引里最近的文件提前秒传过去，播放零等待。

「移除映射」是真删记录，不是清空目标账号。

### 8.2 怎么认出是哪个用户在播

播放请求**不一定带 UserId**，但一定带 `X-Emby-Token`。所以程序在 Emby 登录时记住「登录令牌 → 用户 ID」，播放时用令牌反查。

> 要让映射生效，先让客户端**重新登录一次 Emby**。登录过一次，程序就记住这个用户了。

### 8.3 秒传链路

1. 秒传要带**上传凭据**（userkey），115 会轮换它 —— 被拒时自动换一份再试一次（过期的 userkey 会让每次秒传都失败到重启为止）；
2. 115 返回一段约 1MB 的**二次校验**字节区间，用**源账号直链**按 Range 取那段数据算 sha1 回传，全程只传约 1MB；
3. 源账号 = 文件原本所在的账号，用它的凭据取直链（文件可能在子账号下）；
4. 结果按（账号, sha1）记库，同一文件多人同播只秒传一次；
5. Emby 改写 PlaybackInfo 时就会**顺带预解析**（`INSTANT_PRERESOLVE=1`，默认开），直接把 115 直链交给播放器；卡住时有超时（`INSTANT_PRERESOLVE_TIMEOUT`，默认 25 秒），不会把 PlaybackInfo 一起拖死。

### 8.4 三层缓存

同一条链上三处「本来要发请求 / 读数据」的地方各有一层缓存，目的只有一个：**同一份内容只让源盘被惊动一次**。

| 层 | 存哪 | 命中效果 |
| --- | --- | --- |
| **校验值**（`digest_block`） | SQLite，按 sha1 + 512KB 块索引 | 换用户播同一片时源盘一个字节都不用取 |
| **上传凭据**（`upload_cred`） | SQLite，按账号索引 | 跳过整条链上最慢的一跳 |
| **转存记录**（`instant`） | SQLite，按（账号, sha1）索引 | 文件已在目标账号 → 不重新秒传，只换一条新直链 |

- 校验缓存**按 512KB 块对齐**读取：整块取回来存住，之后 115 再挑这一块里的任何区间都不用碰源盘；
- 缓存**跨用户、跨重启**共享（答案只跟文件内容有关）；
- 源盘换了文件（115 拒绝该 sha1）时会重新问源盘要当前 sha1 再秒传一次；
- 缓存**按内容算大小、只增不减**，所以有预算：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `INSTANT_DIGEST_MAX_MB` | 512 | 字节上限（MB），`0` = 不限（也不清理） |
| `INSTANT_DIGEST_CLEAN_INTERVAL` | 3600 | 每隔多少秒检查是否超限 |
| `INSTANT_DIGEST_POLL_INTERVAL` | 300 | 后台清理线程的检查间隔 |

超限时**先丢最久没用的块**（丢掉的只是「省一次源盘读取」，下次播放重新读一次，不影响能播的文件）。预缓存做完会顺手压回预算内；页面上可以「立即清理」或「清空校验缓存」。

---

## 9. Emby 反代

### 9.1 两种接入方式

| 方式 | 客户端服务器地址 | 说明 |
| --- | --- | --- |
| **独立端口（推荐）** | `http://<本机>:7259` | **不加任何路径前缀**；需要在 compose 里映射同一端口 |
| **本服务端口 + 前缀** | `http://<本机>:9527/emby` | 不用额外端口 |

端口可以在网页「媒体与反代 → Emby 反代 → 监听端口」里改，**保存后立即生效**（旧端口优雅退出，新端口立刻起来，不用重启容器）；但 Docker 的端口映射只能在创建容器时确定，改完要同步改 compose 的 `ports` 并重建。

请求路径**完整透传**给上游：客户端请求 `/emby/xxx`，上游就收到 `/emby/xxx`。这样上游 BaseUrl 配成 `/` 或 `/emby` 都能命中。

**WebSocket 也做了桥接**：Emby 网页客户端登录后必须连 `/embywebsocket`，独立端口、`/emby` 前缀、主服务端口三条入口都会把 upgrade 握手转发给上游（路径 / 查询串 / UA / token 原样带过去）。只做 HTTP 转发的话，网页能打开、能选账号，但登录后连不上服务器、`#/videoosd` 起不来。

### 9.2 播放请求怎么被改写

播放时本服务把 `Items/*/PlaybackInfo` 响应里的 MediaSource 改写成 **115 的最终直链**：改写时就用**发起播放那台设备的 UA** 向 115 取直链，直接发给播放器，播放器零跳转直连 115，Emby 不再中转视频流。同时把 `SupportsDirectPlay` 置真。

预解析失败时（ed2k 还没就绪、限流、需要 Cookie 等）回退成本服务的播放地址，播放器请求它时再 302 / 中转，不影响播放。

直链结果按 `PLAY_CACHE_SECONDS` 缓存；同一文件并发播放只解析一次（配合同播复制）。

**天翼云盘**的 strm 也会被改写：换成「客户端正在访问的那个地址 + `/p189/play/…`」，播放器到反代端口要新鲜直链，再 302 到天翼 CDN。天翼直链带签名、十几分钟过期，所以每次请求现取（服务端有 120 秒缓存），不在改写那一刻固定成一条 CDN 地址 —— 否则拖进度条会 403。

### 9.3 路径识别规则

MediaSource 路径按顺序识别，命中即止：

| 规则 | 匹配 | 说明 |
| --- | --- | --- |
| **strm 路径** | 在 `STRM_ROOT` 下（或 `EMBY_STRM_PREFIX` 前缀） | 查索引拿 pickcode；ed2k / 分享 strm 走各自的稳定路由 |
| **Pickcode** | 路径里带 `pickcode=xxx` | 直接播放 |
| **FileId** | `/115/<id>` 或 `file_id=` | 查 115 拿 pickcode |
| **自定义规则** | `EMBY_PATH_RULES` 每行 `正则=>模板` | `$1..$9` 捕获组、`{base}` 本服务地址；`dbpath:` 开头 = 按 115 相对路径查索引 |

「strm 挂载前缀」（`EMBY_STRM_PREFIX`）是用来**识别哪些 MediaSource 是本服务生成的 strm** 的（Emby 里 strm 目录的挂载路径），**不是客户端地址前缀**。与 `STRM_ROOT` 一致时留空自动识别。

自定义规则示例：把 123 网盘的媒体库映射到 115 索引：

```
^/mnt/123媒体库/(.*)$=>dbpath:$1
```

### 9.4 播放模式

| 模式 | 行为 | 适用 |
| --- | --- | --- |
| `PLAY_MODE=302`（默认） | 容器返回 302，播放器直连 115 CDN | 带宽不经过 NAS，最快 |
| `PLAY_MODE=proxy` | 容器边下边转，支持 Range | 115 对直链做了 UA / 风控限制，或播放器不跟随跳转 |

302 下播放 403 时先换 `proxy` 验证：proxy 正常说明是直链请求头受限，可以继续用 proxy，或用 `PLAY_URL_TEMPLATE` 指向你自己的反代。

```env
PLAY_URL_TEMPLATE=http://alist.local:5244/d/115/{pickcode}
```

占位符：`{base}` `{pickcode}` `{name}`（URL 编码）`{ext}`（不含点）`{token}`。

页面上的「测试连通性」会分别探测上游 Emby 与独立端口的监听状态，直接告诉你通没通。

---

## 10. 插件

### 10.1 插件页

「系统 → 插件」列出三个可选功能，每个一张卡（代码体积、依赖、当前状态），可以**安装 / 卸载 / 停用 / 启用 / 打开**。装完入口自动出现在侧栏，卸载后入口与接口一起消失，不用重启容器。

- **重依赖按需下载**：企微可信 IP 用的 playwright + Chromium（aarch64 下约 730MB）不打进镜像，点「安装」时才下到 `/config/plugins/wxip/` —— 那是配置卷，**容器重建、换镜像都不用重装**；
- **没装就不加载**：插件路由、后台线程、浏览器都只在启用时存在；
- **升级无感**：检查数据库里有没有这些功能的配置，用过的自动补成「已安装」；全新部署则三项都是「未安装」；
- 源码在仓库 `plugins/<id>/`：`plugin.json` 清单 + `backend.py` 路由 + `web.js` 页面，前端脚本由 `/plugin-assets/<id>.js` 按需注入并调 `Shell.registerPlugin()` 注册，核心代码里没有硬编码的插件名单。

### 10.2 天翼云盘

独立的「天翼云盘」工具页，三种登录方式：

| 方式 | 说明 |
| --- | --- |
| **扫码** | 页面出二维码，App 扫，**手机上还要点一下「确认登录」** |
| **Cookie** | 浏览器登录 cloud.189.cn 后复制 `COOKIE_LOGIN_USER`，服务端取里面的 `SESSION_KEY` 换 token |
| **手动 token** | 直接填 `accessToken`（**没有会话密钥，不能续期**） |

- **多账号**：一个页面登录多个天翼账号，账号卡上能设默认、手动续期、开关自动续期、退出、删除；
- **家庭云**：目录树顶层同时有「个人云」和「家庭云」，停在「家庭云」这一层直接确定也算数 —— 所有家庭各落一个子目录；
- **token 自动续期**：天翼没有 refreshToken 接口，只有 `getAccessTokenBySsKey` 能静默换 token，所以扫码 / Cookie 登录会把 `sessionKey` 一起存下来，剩不到 24 小时自动换新，后台每 30 分钟巡检一次；
- **生成 strm**：`<请求头>/p189/play/<账号>/<fileId>`，家庭云是 `/p189/play/<账号>/fam/<家庭id>/<fileId>`；播放时本服务**实时换新鲜直链再 302**（直链带签名与时效、URL 里还有账号凭据，绝不能落盘）；
- 生成模式与 115 STRM 一致（增量 / 全量 / 全新），事件日志落库；
- 扫码没反应时看 `GET /api/cloud189/login/debug`，那里有每次轮询的原始响应。

### 10.3 通知与联动

**通知渠道**：

| 渠道 | 能力 |
| --- | --- |
| 企业微信群机器人 | 只推送 |
| 企微自建应用 | 推送 + 接收指令（回调走 `msg_signature` 校验，AES 解密自带实现） |
| Telegram Bot | 推送 + 长轮询收指令，不需要公网 |
| Webhook | POST 一段 JSON 到任意地址（Emby / n8n / 自研网关） |

可分别开关「任务开始 / 完成 / 失败 / 联动删除 / 扫描异常」。

Webhook 默认 payload 同时带 Emby Webhooks 插件风格（`Event` / `Title` / `Description` / `Date` / `Server`）与消息中心需要的 `Name` / `Description` / `Level`；选了「媒体服务器」会自动带上 `<Emby>/emby/Notifications/Admin` 地址与 `X-Emby-Token`。模板占位符：`{{title}} {{text}} {{event}} {{level}} {{date}} {{version}}`（只替换 JSON 字符串值，正文里有引号也不会把 payload 拼坏）。

**指令**（两个渠道共用）：`/status` `/ls` `/run <配置名|all> [inc|full|rebuild]` `/find <关键词>` `/pending` `/link on|off` `/notify on|off` `/help`。TG 只认配置里的 `chat_id`（或白名单用户）。

**双向联动**：

| 方向 | 行为 |
| --- | --- |
| **本地删 strm → 删网盘原文件** | 在文件管理器 / Emby 里删掉 `.strm`，超过宽限期（默认 5 分钟）后把 115 上对应文件一起删（进回收站，可恢复） |
| **网盘删文件 → 清本地 strm + 刮削文件** | 除了 strm，还会删同名的 `nfo` / 海报 / 字幕（`影片.nfo`、`影片.zh.srt`、`影片-poster.jpg`）；目录级 `poster.jpg`、`影片.2023.nfo` 这类不属于它的不动 |

「本地删 → 删网盘」默认**关闭**，开关有二次确认；「网盘删 → 清本地」默认**开启**。另有：**试运行**（只看不动）、**比例熔断**（一轮缺失超过 20% 且至少 5 个就整体跳过，防 NAS 挂载掉线时把网盘删空）、`STRM_ROOT` 不可用直接跳过、只处理索引里记着的文件。

### 10.4 企微可信 IP

公网 IP 一变就自动改企微自建应用的「企业可信 IP」，否则应用消息 / 回调会因为「IP 不在可信列表」失败。

- 扫码登录企微管理后台（二维码直接显示在网页上），登录态存 `/config/wxip/state.json`，过期重新扫；
- 每 5 分钟（可调）用 6 个公开接口容灾检测公网 IP，变了就更新，并通过通知渠道告诉你；
- **追加而不是覆盖**：读出现有列表再追加（企微用英文分号分隔），写完重新打开弹窗核对 —— 覆盖式写入会把其它服务器的 IP 删光；
- 登录判据看 cookie 里的 `wwrtx.vst`（不只是 URL），容忍扫码后主框架不跳转的情况；
- AgentId 一栏除了数字也能填**应用名**，会去后台列表换成 id；留空则更新全部自建应用；
- 「截图诊断」可以看浏览器停在哪一页，失败时页面会存成 `/config/wxip/last.html`。

---

## 11. 配置项

完整列表见 [`.env.example`](./.env.example)。**大部分设置都能在网页上改并即时生效**（存数据库），环境变量只是初始值。

**基础**

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `P115_COOKIE_FILE` | `/config/115-cookies.txt` | cookies 文件；也可用 `P115_COOKIES` 直接给字符串。**都可以不填**：起服务后在网页里添加 |
| `DATA_DIR` | `/config` | 数据库与账号配置 |
| `STRM_ROOT` | `/strm` | strm 输出根目录（**建议专用**） |
| `PUBLIC_BASE_URL` | —— | **必须改**，写进 strm 的地址前缀 |
| `MODE` | `all` | `all` 服务+增量 / `full` 只跑一次全量 / `watch` 只增量 / `serve` 只提供 HTTP；`full` / `watch` 模式没有网页，必须先配好 cookies |
| `LOG_LEVEL` | `INFO` | |

**扫描与限速**

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ROOT_CIDS` | 空 | 全量扫描的起始根目录（115 目录 id，逗号分隔），`0` = 整个网盘。只在**首次启动**时写入数据库，之后改它不再生效 |
| `INCLUDE_ROOT_NAME` | `1` | 非 0 根目录时是否把目录名作为本地一级目录 |
| `STRM_EXT` | 常见视频后缀 | 只处理这些后缀 |
| `STRM_ALL_FILES` | `0` | `1` = 所有文件都生成 |
| `INCLUDE_REGEX` / `EXCLUDE_REGEX` | 空 | 按文件名的正则过滤 |
| `EXCLUDE_DIRS` | 空 | 跳过目录，大小写不敏感的子串匹配，如 `#recycle,临时` |
| `SCAN_APP` | `web` | 列目录接口，失败自动在 `web`→`aps`→`chrome` 间换 |
| `API_QPS` | `2` | 全局请求上限（次/秒），所有 115 请求共用 |
| `SCAN_WORKERS` | `2` | 同时遍历的目录数 |
| `PAGE_WORKERS` | `0` | 单目录内并发翻页，`0` = 串行 |
| `SCAN_COOLDOWN` | `0` | 每请求额外冷却秒数 |
| `FULL_SCAN_ON_START` | `auto` | `auto` / `always` / `never` |
| `DELETE_ORPHAN` | `1` | 全量后删掉 115 上已不存在的 `.strm` |
| `DELETE_EMPTY_DIRS` | `1` | 清理空目录 |

**增量**

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EVENT_ENABLED` | `1` | 是否开启生活事件同步 |
| `EVENT_APP` | `web` | 事件接口 |
| `EVENT_INTERVAL` | `30` | 事件轮询间隔（秒） |
| `EVENT_COOLDOWN` | `0.6` | 事件请求冷却 |
| `EVENT_BATCH` | `1000` | 单次拉取事件条数 |
| `FALLBACK_INTERVAL` | `600` | 事件不可用时的指纹补扫间隔（秒） |
| `SMART_SCAN` | `1` | 按目录 mtime 跳过没变化的子树 |
| `RECONCILE_HOURS` | `0` | 每 N 小时全量对账一次，`0` 关闭 |

**播放**

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PLAY_MODE` | `302` | `302` / `proxy` |
| `PLAY_URL_TEMPLATE` | 空 | 自定义播放地址模板 |
| `PLAY_URL_EXT` | `1` | 播放地址是否带扩展名 |
| `PLAY_USER_AGENT` | Chrome UA | 向 115 取直链时用的 UA |
| `PLAY_CACHE_SECONDS` | `900` | 直链缓存时长 |
| `AUTH_TOKEN` | 空 | 设置后播放地址需带 `?token=` |

**ed2k**

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ED2K_ENABLED` | `1` | 是否启用 |
| `ED2K_DIR_CID` | `0` | 转存目录 id，`0` = 自动创建 |
| `ED2K_DIR_NAME` | `p115-strm-ed2k` | 自动创建的转存目录名 |
| `ED2K_STRM_DIR` | `ed2k` | strm 输出子目录（相对 `STRM_ROOT`） |
| `ED2K_AUTO_TRANSFER` | `0` | `0` = 播放时转存，`1` = 添加时转存 |
| `ED2K_TTL_HOURS` | `24` | 保留时长，`0` = 不清理 |
| `ED2K_CLEAN_INTERVAL` | `600` | 清理检查间隔（秒） |
| `ED2K_POLL_INTERVAL` | `20` | 转存状态刷新间隔 |
| `ED2K_PLAY_WAIT` | `45` | 播放时最多等多少秒 |

**分享 / 秒传 / 反代 / 界面**

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SHARE_ENABLED` | `1` | 是否启用分享转存 |
| `SHARE_DIR_CID` / `SHARE_DIR_NAME` | `0` / `p115-strm-share` | 接收目录 |
| `SHARE_STRM_DIR` | `share` | strm 子目录 |
| `INSTANT_DIR_NAME` | `p115-strm-instant` | 秒传暂存目录 |
| `INSTANT_CACHE_SECONDS` | `1800` | 秒传直链缓存 |
| `INSTANT_PRERESOLVE` | `1` | 改写 PlaybackInfo 时顺带秒传 |
| `INSTANT_PRERESOLVE_TIMEOUT` | `25` | 预解析最长等待（秒） |
| `INSTANT_DIGEST_MAX_MB` | `512` | 校验缓存上限 |
| `EMBY_ENABLED` | `0` | 是否启用反代 |
| `EMBY_URL` | 空 | 上游 Emby 地址，如 `http://emby:8096` |
| `EMBY_PROXY_PORT` | `0` | 独立端口，`0` = 用 `/emby` 前缀 |
| `EMBY_STRM_PREFIX` | 空 | strm 在 Emby 里的挂载前缀（识别用） |
| `EMBY_PATH_RULES` | 空 | 自定义路径规则，每行 `正则=>模板` |
| `WEB_PORT` | `9527` | 服务端口 |
| `UI_USERNAME` / `UI_PASSWORD` | `admin` / `password` | 网页登录；密码留空 = 不启用登录 |
| `UI_SITE_NAME` | `PanShare` | 站点名（登录页与标题栏）。历史上拼错的变量名，为兼容老配置保留 |
| `UI_SHOW_HINT` | `1` | 登录页是否提示默认账号 |
| `UI_SESSION_TTL` | `604800` | 会话有效期（秒） |
| `TZ` | `Asia/Shanghai` | 时区 |
| `PUID` / `PGID` | —— | 修正 `/config`、`/strm` 属主（填数字，不要求容器内有同名用户） |
| `CHOWN_RECURSIVE` | `0` | `1` 才会递归改 `/strm` 已有文件的属主 |
| `TMDB_API_KEY` / `TMDB_READ_TOKEN` / `TMDB_PROXY` / `LOGIN_BACKDROPS` | 空 | 登录页背景（网页上也能配，优先） |

---

## 12. 增量同步与限速

### 12.1 生活事件（接口可用时走这条）

读取 115「生活事件」，事件 id 存 SQLite 作游标：

| 事件 | 处理 |
| --- | --- |
| 上传文件/目录、接收文件 | 查节点信息 → 写 strm；目录则递归扫该子树 |
| 移动、复制、新建目录 | 重算路径 → 移动本地 strm / 扫描新目录 |
| 文件改名、目录改名 | 整棵子树 `os.replace` + 批量改写索引 |
| 删除 | 删对应 strm（目录则整棵子树） |
| 浏览、星标、标签 | 忽略 |

同一份文件重复事件是幂等的：strm 内容一致时不会重写。

> 增量能力受 115 限制：事件流里**没有「回收站还原」这类事件**，第三方客户端上传有时也不产生上传事件。建议开启 `RECONCILE_HOURS` 定期对账。

### 12.2 指纹增量（事件不可用时的兜底）

1. 每 `FALLBACK_INTERVAL`（默认 600 秒）从监控根开始遍历；
2. 每个目录用它在父列表里的 **mtime + 名字** 做指纹（存 `dir_stamps` 表）；
3. 指纹没变 → 整棵子树跳过，一个请求都不发；
4. 指纹变了 → 重列该目录、更新 strm，再递归子目录。

「在子目录里新建条目」会更新该目录在父列表里的 mtime，所以父级能感知深层变化；一轮通常是「根目录 1 次 + 变化路径若干次」请求。

### 12.3 WAF 与退避

115 前面是一层阿里云 WAF：**请求太快就按账号返回 405 拦截页**，实测 20 QPS 持续十几秒就会中招，之后连根目录都列不出来。注意它是**按域名**封的：`webapi` 被封时 `aps` 还能用。

- 默认配置很保守：`API_QPS=2`、`SCAN_WORKERS=2`、`PAGE_WORKERS=0`，所有请求共用一个全局令牌桶，调大 worker 也不会叠加成更高 QPS；
- 被拦时先换入口（`web` → `aps` → `chrome`），换到没被封的立刻通；
- 全被封时逐级退避（分钟级，最多约 27 分钟），期间不发请求，成功后自动恢复；
- **不要反复重试** —— 每重试一次，封禁时间往后延。

扫大网盘会慢：2 QPS 下 2000 个目录约 20 分钟。可以先试 `API_QPS=3`，一旦日志出现 405 就改回 `1`。

---

## 13. 运维与排错

### 13.1 常用命令

```bash
docker compose logs -f

# 管理接口需要登录（浏览器或脚本带 Cookie）
curl -c cookie.txt -X POST http://127.0.0.1:9527/api/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"你的密码"}'
curl -b cookie.txt http://127.0.0.1:9527/api/status

curl -X POST http://127.0.0.1:9527/api/scan?full=true    # 手动全量
curl -X POST http://127.0.0.1:9527/api/scan?full=false   # 手动增量
curl -X POST http://127.0.0.1:9527/api/poll              # 立刻拉一次事件
curl http://127.0.0.1:9527/api/status                    # 状态
curl http://127.0.0.1:9527/api/strm/scheduler            # 定时巡检状态
curl http://127.0.0.1:9527/healthz                       # 健康检查
```

设了 `AUTH_TOKEN` 时，以上接口也可以带 `?token=...`（或 `X-Token` 头）代替登录。

### 13.2 常见问题

**目录一直「加载中」/ 添加不上？**

- `列目录失败…: [Errno 20]` = 请求的目录 id 在 115 上不存在。最常见的原因不是网络，而是 **19 位 cid 被 JS 四舍五入**（`3027523183514719412` → `3027523183514719000`）。现在所有 cid 都以字符串在前后端传递；旧页面硬刷新（Ctrl+F5）即可。若仍报错，说明目录确实被删了；
- `HTTP Error 405: Method Not Allowed` = WAF 限流，不是接口挂了。判断方法：同一接口**不带** cookies 请求能返回正常 JSON（「请先登录」），带上才 405；
- 提示「已在『整个网盘』的监控范围内」：起始根目录里有 `0`（整个网盘），它会覆盖所有子目录。旧版本默认会写进去，需要清掉才能按目录精细控制。

**播放转圈 / 直链预解析失败？**

先看 `PUBLIC_BASE_URL` 是不是播放器能访问的地址，再确认 `PLAY_MODE` 是否要换 `proxy`。

**容器重建后网页改动没了？**

网页上的配置存在 `/config` 里的 SQLite，只要 `/config` 是卷就不会丢。用 `docker cp` 临时拷进容器的**代码**改动才会在重建后丢 —— 要么 `docker commit` 成新镜像，要么把源码目录挂进容器。

**改了环境变量不生效？**

`docker restart` 不会重新读环境变量，要 `docker compose up -d` 重建容器。能用网页改的优先在网页改（存数据库，不用重建）。

### 13.3 NAS 权限

```env
PUID=1000
PGID=1000
CHOWN_RECURSIVE=0
```

入口脚本会改 `/config`、`/strm` 属主后降权运行（默认不递归，避免几十万 strm 文件拖慢启动）。降权用 `os.setuid` 实现，**不要求容器内存在同名用户**，填 NAS 上 `id 用户名` 查到的数字即可。

### 13.4 数据在哪

| 内容 | 位置 |
| --- | --- |
| 主库（配置 / 索引 / 任务 / 事件） | `/config/p115-strm.db`（SQLite） |
| 各账号索引 | `/config/accounts/<账号名>/` |
| 账号登记 | `/config/accounts.json` |
| 插件（含重依赖） | `/config/plugins/<id>/` |
| 企微登录态 | `/config/wxip/state.json` |
| strm 与元数据 | `/strm`（`STRM_ROOT`） |

想彻底重来：停容器 → 删 db 和 `/strm` 下的 `.strm` → 启动。

---

## 14. 构建镜像

镜像基于 `python:3.12-slim`，**构建过程中不做任何 `apt-get`**：时区、健康检查、PUID/PGID 降权全部由基础镜像或 Python 标准库实现（`scripts/healthcheck.py`、`scripts/runas.py`），只要能从 pip 源装依赖即可。

### 14.1 脚本一键构建（推荐）

```bash
chmod +x build.sh
./build.sh                                  # 本机架构 → panshare:latest
./build.sh -p linux/amd64                   # NAS / x86 服务器
./build.sh -o panshare-amd64.tar            # 额外导出 tar
./build.sh -i https://pypi.tuna.tsinghua.edu.cn/simple
./build.sh -b docker.m.daocloud.io/library/python:3.12-slim
```

Windows 用 `.\build.ps1`，参数同名（`-Image` `-Platform` `-Output` `-Push` `-PipIndexUrl` `-BaseImage` `-NoCache`）。

### 14.2 手动 docker build

```bash
docker build -t panshare:latest .

docker build --platform linux/amd64 \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t panshare:latest .

# 多架构（buildx，必须 push 才能保留两个架构）
docker buildx build --platform linux/amd64,linux/arm64 \
  -t yourname/panshare:latest --push .
```

> 群晖 / 威联通 / 绿联等 ARM 机型要 `linux/arm64`；在 x86 上给 ARM NAS 构建需要 QEMU：
> `docker run --privileged --rm tonistiigi/binfmt --install all`。

### 14.3 GitHub Actions 云构建

仓库自带 [`.github/workflows/docker.yml`](./.github/workflows/docker.yml)：

- 推 `main` / `master` 或打 `v*` 标签 → 自动构建 `linux/amd64` + `linux/arm64`，推到 `ghcr.io/<用户名>/<仓库名>:latest`；
- 页面点 **Run workflow** 手动触发；
- 每次构建在 **Artifacts** 里放一个 amd64 镜像包，下载解压后 `docker load`，目标机器不需要能访问 registry。

### 14.4 离线导入

```bash
./build.sh -o panshare-amd64.tar
# 拷到 NAS 后
docker load -i panshare-amd64.tar
```

`docker load` 后镜像名就是构建时的 tag，配合 compose 时把 `build:` 段删掉即可。

### 14.5 构建失败

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `failed to resolve source metadata ... python:3.12-slim` | 拉不到 Docker Hub | `./build.sh -b docker.m.daocloud.io/library/python:3.12-slim`（或 `docker.1ms.run/library/python:3.12-slim`、`hub.rat.dev/library/python:3.12-slim`） |
| `Could not find a version that satisfies ... p115client` | pip 源不通 | `./build.sh -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| pip 也超时 | 构建容器没有出网能力 | `docker build --build-arg HTTPS_PROXY=http://192.168.1.2:7890 --build-arg HTTP_PROXY=... -t panshare:latest .` |
| 卡在 `Building wheel for pydantic-core` | 架构 / Python 版本没有预编译轮子 | 用 `python:3.12-slim`（glibc）而不是 alpine，确认 `--platform` 与设备一致 |
| `exec format error` | 架构不匹配 | `docker inspect 镜像 --format '{{.Architecture}}'` 对照设备 |

> 健康检查用的是 `python /app/scripts/healthcheck.py`，镜像里**没有 curl**；自定义 compose 的 `healthcheck` 时别再用 curl。

---

## 15. 本地调试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=$PWD/config STRM_ROOT=$PWD/strm PUBLIC_BASE_URL=http://127.0.0.1:9527
python -m app              # MODE=all
python -m app scan         # 只跑一次全量
python tests/smoke_test.py # 离线冒烟
pytest                     # 全量测试
```

---

## 16. 已知限制

- **115 的事件流里没有「回收站还原」这类事件**，第三方客户端上传有时也不产生上传事件，这些变化靠定期对账或指纹扫描兜底。
- **指纹增量依赖 115 老实更新目录 mtime**。想更保险，把 `RECONCILE_HOURS` 设成 `24`，每天强制全量重列一次。
- **同播复制认设备靠 IP + UA**：Emby 服务端转码拉流时所有请求同源，不会触发复制 —— 这是服务端确实分不清谁在看，不是 bug。
- **秒传需要源账号凭据**：文件在哪个账号下，就要用那个账号去取校验数据；「秒传账号」名单只决定「秒传到哪」，不影响从哪读。
- **天翼手动填的 token 不能续期**（没有会话密钥）；扫码 / Cookie 登录的才能自动续。
- **115 WAF 的限速是按账号的**：多账号各自独立，但同一个账号的大规模操作（全量 + 秒传预缓存同时跑）仍可能互相挤占配额。
- **播放地址必须能被播放器访问**：`PUBLIC_BASE_URL` 写成 docker 网桥地址是最常见的「能生成不能播」原因。

---

## 免责声明

仅供个人备份与媒体管理使用。请遵守 115 的服务条款，控制请求频率，因使用本工具导致的账号风控或封禁与作者无关。
