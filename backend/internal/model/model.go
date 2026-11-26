// Package model 定义跨层共用的数据结构。
package model

// Account 是一个 115 账号。cookies 永远不会出现在 JSON 响应里。
type Account struct {
	ID         string `json:"id"`
	Name       string `json:"name"`      // 备注名（可改）
	UID        string `json:"uid"`       // 115 用户 ID，strm 末尾写它
	UserName   string `json:"user_name"` // 115 昵称
	Source     string `json:"source"`    // qrcode / cookie
	HasCookies bool   `json:"has_cookies"`
	CopyPlay   bool   `json:"copy_play"` // 同播复制：后播的设备复制一份文件，各走各的直链
	IsDefault  bool   `json:"is_default"`
	Status     string `json:"status"` // ok / expired / error
	LastError  string `json:"last_error"`
	CreatedAt  int64  `json:"created_at"`
	UpdatedAt  int64  `json:"updated_at"`
}

// StrmPath 是一条「网盘目录 → 本地目录」的映射。
// 一条配置可以挂多条映射，方便把 已整理 / 原盘 这些目录合成一个任务。
type StrmPath struct {
	RemoteID   string `json:"remote_id"`   // 115 目录 cid，0 = 根目录
	RemotePath string `json:"remote_path"` // 只用于展示
	LocalDir   string `json:"local_dir"`   // 相对映射根
}

// StrmConfig 是一条「账号 + 网盘路径 → 本地目录」的生成配置。
type StrmConfig struct {
	ID            string     `json:"id"`
	Name          string     `json:"name"`
	AccountID     string     `json:"account_id"`
	AccountName   string     `json:"account_name"`
	Paths         []StrmPath `json:"paths"` // 多条映射；空则回落到下面三个老字段
	RemoteID      string     `json:"remote_id"`
	RemotePath    string     `json:"remote_path"`
	LocalDir      string     `json:"local_dir"`
	Mode          string     `json:"mode"` // pickcode（普通目录）/ share（指向自己的分享）
	IncludeExts   string     `json:"include_exts"`
	MetaExts      string     `json:"meta_exts"` // 元数据扩展名（nfo/jpg/srt…）
	StrmBase      string     `json:"strm_base"` // strm 请求头，留空用全局
	AllFiles      bool       `json:"all_files"`
	WatchEnabled  bool       `json:"watch_enabled"`   // 事件监控（115 生活事件实时增量）
	WatchDelaySec int        `json:"watch_delay_sec"` // 事件延时，等上传任务落盘
	WatchInterval int        `json:"watch_interval"`  // 事件监控轮询间隔（秒），0 = 用全局默认
	LastSig       string     `json:"last_sig"`        // 目录指纹（事件接口不可用时的兜底）
	LastCheckAt   int64      `json:"last_check_at"`   // 上次探测目录的时间
	SchedEnabled  bool       `json:"sched_enabled"`   // 定时增量巡检
	SchedMinutes  int        `json:"sched_minutes"`
	DownloadMeta  bool       `json:"download_meta"` // 下载 nfo/海报等元数据
	LastRunAt     int64      `json:"last_run_at"`
	LastStatus    string     `json:"last_status"`
	LastMessage   string     `json:"last_message"`
	Files         int        `json:"files"`
	Running       bool       `json:"running"`
	CreatedAt     int64      `json:"created_at"`
}

// EffectivePaths 返回真正要扫描的映射：优先用新字段，老配置自动回落。
func (c *StrmConfig) EffectivePaths() []StrmPath {
	out := make([]StrmPath, 0, len(c.Paths))
	for _, p := range c.Paths {
		if p.LocalDir == "" && p.RemoteID == "" && p.RemotePath == "" {
			continue
		}
		if p.RemoteID == "" {
			p.RemoteID = "0"
		}
		out = append(out, p)
	}
	if len(out) > 0 {
		return out
	}
	return []StrmPath{{
		RemoteID:   firstStr(c.RemoteID, "0"),
		RemotePath: c.RemotePath,
		LocalDir:   c.LocalDir,
	}}
}

func firstStr(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

// StrmFile 是生成索引：一个 115 文件对应一个本地 strm。
type StrmFile struct {
	ID        int64  `json:"id"`
	ConfigID  string `json:"config_id"`
	AccountID string `json:"account_id"`
	FileID    string `json:"file_id"`
	Pickcode  string `json:"pickcode"`
	Name      string `json:"name"`
	RelPath   string `json:"rel_path"`  // 相对映射根的媒体路径（不含 .strm）
	StrmPath  string `json:"strm_path"` // 绝对路径
	Size      int64  `json:"size"`
	Sha1      string `json:"sha1"`
	ParentID  string `json:"parent_id"`
	ShareCode string `json:"share_code"` // mode=share 时，指向自己的分享码
	Source    string `json:"source"`     // strm / share / ed2k / instant
	Deleted   bool   `json:"deleted"`
	CreatedAt int64  `json:"created_at"`
}

// ShareTask 是一次「分享链接 → 转存 → 建自己的分享 → 生成 strm」。
type ShareTask struct {
	ID            string `json:"id"`
	AccountID     string `json:"account_id"`
	AccountName   string `json:"account_name"`
	URL           string `json:"url"`
	ShareCode     string `json:"share_code"`
	ReceiveCode   string `json:"receive_code"`
	Title         string `json:"title"`
	DirCID        string `json:"dir_cid"`
	DirName       string `json:"dir_name"`
	LocalDir      string `json:"local_dir"`
	NoTransfer    bool   `json:"no_transfer"` // 不转存，直接用源分享生成
	MyShareCode   string `json:"my_share_code"`
	MyReceiveCode string `json:"my_receive_code"`
	Status        string `json:"status"` // pending / running / done / error
	Message       string `json:"message"`
	Files         int    `json:"files"`
	CreatedAt     int64  `json:"created_at"`
	UpdatedAt     int64  `json:"updated_at"`
}

// Ed2kTask 是一个 ed2k 链接的转存 + strm 任务。
type Ed2kTask struct {
	ID            string `json:"id"` // info hash
	Name          string `json:"name"`
	Size          int64  `json:"size"`
	URL           string `json:"url"`
	AccountID     string `json:"account_id"`
	AccountName   string `json:"account_name"`
	DirCID        string `json:"dir_cid"`
	LocalDir      string `json:"local_dir"`
	TransferOnAdd bool   `json:"transfer_on_add"`
	State         string `json:"state"` // pending / transferring / done / failed
	Message       string `json:"message"`
	Pickcode      string `json:"pickcode"`
	FileID        string `json:"file_id"`
	CreatedAt     int64  `json:"created_at"`
	UpdatedAt     int64  `json:"updated_at"`
}

// InstantTarget 是「被秒传账号」及其落盘目录。
type InstantTarget struct {
	AccountID   string `json:"account_id"`
	AccountName string `json:"account_name"`
	DirCID      string `json:"dir_cid"`
	DirPath     string `json:"dir_path"`
	Enabled     bool   `json:"enabled"`
}

// InstantMapping 把 Emby 用户映射到某个被秒传账号。
type InstantMapping struct {
	EmbyUserID   string `json:"emby_user_id"`
	EmbyUserName string `json:"emby_user_name"`
	AccountID    string `json:"account_id"`
	AccountName  string `json:"account_name"`
	UpdatedAt    int64  `json:"updated_at"`
}

// EmbyServer 是一台 Emby。
type EmbyServer struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	URL       string `json:"url"`
	APIKey    string `json:"api_key"`
	Enabled   bool   `json:"enabled"`
	IsDefault bool   `json:"is_default"` // 反代默认用第一台启用的
	CreatedAt int64  `json:"created_at"`
}

// Event 是任务日志（页面上的「事件」列表、TG 推送来源）。
type Event struct {
	ID        int64  `json:"id"`
	Level     string `json:"level"` // info / warn / error / success
	Kind      string `json:"kind"`
	Message   string `json:"message"`
	Detail    string `json:"detail"`
	CreatedAt int64  `json:"created_at"`
}

// Deletion 是联动删除的待办（宽限期内可撤销）。
type Deletion struct {
	ID        int64  `json:"id"`
	Kind      string `json:"kind"` // local→cloud / cloud→local
	AccountID string `json:"account_id"`
	FileID    string `json:"file_id"`
	Path      string `json:"path"`
	StrmPath  string `json:"strm_path"`
	State     string `json:"state"` // pending / done / error
	Message   string `json:"message"`
	CreatedAt int64  `json:"created_at"`
}

// Settings 是全局设置，整体以 JSON 存进 settings 表。
type Settings struct {
	SiteName      string `json:"site_name"`
	StrmRoot      string `json:"strm_root"`
	PublicBaseURL string `json:"public_base_url"`

	// 115 STRM
	DefaultExts      string `json:"default_exts"`
	DefaultMetaExts  string `json:"default_meta_exts"`
	WatchDelaySec    int    `json:"watch_delay_sec"`
	WatchIntervalSec int    `json:"watch_interval_sec"` // 事件监控轮询间隔默认值（秒）
	DownloadMeta     bool   `json:"download_meta"`
	APIQPS           int    `json:"api_qps"`

	// 联动删除
	DeleteEnabled               bool `json:"delete_enabled"`
	DeleteGraceMinutes          int  `json:"delete_grace_minutes"`
	DeleteCloudWhenLocalMissing bool `json:"delete_cloud_when_local_missing"`
	DeleteLocalWhenCloudDeleted bool `json:"delete_local_when_cloud_deleted"`
	DeleteMetaWithStrm          bool `json:"delete_meta_with_strm"`

	// Telegram
	TGEnabled  bool   `json:"tg_enabled"`
	TGBotToken string `json:"tg_bot_token"`
	TGChatID   string `json:"tg_chat_id"`
	TGCommands bool   `json:"tg_commands"`
	// 共享代理：Telegram 与 TMDB 登录背景都用这一个出口。
	// 例如 http://192.168.1.34:7890 或 socks5://192.168.1.34:7891。
	// 留空则依次回落到 compose 的 PS_PROXY、环境变量 HTTPS_PROXY。
	TGProxy string `json:"tg_proxy"`

	// 微信通知：企业微信自建应用 与 群机器人两种通道，填了哪个发哪个。
	// 自建应用需要把出口 IP 加进「企业可信IP」，否则报 60020。
	WeChatEnabled  bool   `json:"wechat_enabled"`
	WeChatCorpID   string `json:"wechat_corp_id"`
	WeChatAgentID  string `json:"wechat_agent_id"`
	WeChatSecret   string `json:"wechat_secret"`
	WeChatToUser   string `json:"wechat_to_user"`   // 接收成员，留空/未填默认 @all
	WeChatRobotURL string `json:"wechat_robot_url"` // 群机器人 Webhook，也可只填 key
	// 允许在应用聊天窗口里发指令 / 发链接，和 Telegram 那套一致。
	WeChatCommands bool `json:"wechat_commands"`
	// 只推这些类型的事件，逗号分隔（strm,share,ed2k…）。
	// 留空 = 全部类型；「-」= 一个都不推。
	WeChatNotifyKinds string `json:"wechat_notify_kinds"`

	// 企业微信「接收消息」回调。企业微信要求配「企业可信IP」之前先设好
	// 可信域名或接收消息URL，这里就是后者：<公网地址>/api/wechat/app/callback?token=API。
	// WeChatAppAPI 是地址里的凭据，填对了就不再校验 msg_signature，
	// 好处是企业微信后台的 Token / EncodingAESKey 填什么都行（加解密方式选明文模式）。
	// 留空则退回原来的「Token + EncodingAESKey 安全模式」校验。
	WeChatAppAPI    string `json:"wechat_app_api"`
	WeChatAppToken  string `json:"wechat_app_token"`
	WeChatAppAESKey string `json:"wechat_app_aeskey"`

	// 微信服务号（公众号）：模板消息推送 + 公网回调。
	// 回调地址是 <公网地址>/api/wechat/mp/callback，填在公众号后台「服务器配置」。
	WeChatMPEnabled    bool   `json:"wechat_mp_enabled"`
	WeChatMPAppID      string `json:"wechat_mp_app_id"`
	WeChatMPSecret     string `json:"wechat_mp_secret"`
	WeChatMPTemplateID string `json:"wechat_mp_template_id"`
	WeChatMPToUser     string `json:"wechat_mp_to_user"` // 接收 OpenID，多个用逗号分隔
	WeChatMPBase       string `json:"wechat_mp_base"`    // 公网地址，留空则用 public_base_url
	WeChatMPToken      string `json:"wechat_mp_token"`   // 回调 Token（后台「服务器配置」里填的那个）
	WeChatMPAESKey     string `json:"wechat_mp_aes_key"` // EncodingAESKey，安全模式才需要

	// ed2k
	Ed2kAccountID     string `json:"ed2k_account_id"`
	Ed2kDirCID        string `json:"ed2k_dir_cid"`
	Ed2kDirName       string `json:"ed2k_dir_name"`  // 只用于页面回显
	Ed2kLocalDir      string `json:"ed2k_local_dir"` // 上次选的本地目录（相对映射根）
	Ed2kTransferOnAdd bool   `json:"ed2k_transfer_on_add"`
	Ed2kTTLHours      int    `json:"ed2k_ttl_hours"`

	// 分享转存
	ShareDirCID    string `json:"share_dir_cid"`
	ShareDirName   string `json:"share_dir_name"`
	ShareAccountID string `json:"share_account_id"`
	ShareLocalDir  string `json:"share_local_dir"`

	// Emby 反代监听端口。0 = 跟随环境变量 PS_EMBY_PROXY_PORT（出厂默认 7259），
	// >0 = 用页面上手填的端口。
	EmbyProxyPort int `json:"emby_proxy_port"`
	// Emby 反代的开关：true = 关掉（就算端口填了也不监听）。
	// 默认 false（老配置升上来也就自动开着，不会因为多了个字段就断掉）。
	EmbyProxyOff bool `json:"emby_proxy_off"`

	// 秒传：true = 不在 PlaybackInfo 阶段提前秒传。
	// 默认 false（保持「提前备好」的行为）。关掉后仍然能用：播放到
	// /p115/instant 时会同步秒传，只是首播要多等两三秒。
	InstantPrewarmOff bool `json:"instant_prewarm_off"`
}

func DefaultSettings(strmRoot, baseURL string) Settings {
	return Settings{
		SiteName:        "PanShare",
		StrmRoot:        strmRoot,
		PublicBaseURL:   baseURL,
		DefaultExts:     ".mp4,.mkv,.ts,.m2ts,.avi,.wmv,.mov,.rmvb,.flv,.iso,.mpg,.mpeg,.m4v",
		DefaultMetaExts: ".nfo,.jpg,.jpeg,.png,.srt,.ass,.sub,.idx",
		WatchDelaySec:   30,
		// 事件监控不是「每 N 秒生成一次」，而是每 N 秒探一次目录指纹，变了才生成。
		// 60 秒对 115 的接口压力可以忽略，又能让新片 1 分钟左右出现。
		WatchIntervalSec: 60,
		DownloadMeta:     false,
		APIQPS:           2,
		DeleteEnabled:    false, DeleteGraceMinutes: 0,
		DeleteCloudWhenLocalMissing: true,
		DeleteLocalWhenCloudDeleted: true,
		DeleteMetaWithStrm:          true,
		TGChatID:                    "",
		WeChatToUser:                "@all",
		WeChatMPToUser:              "",
		Ed2kTransferOnAdd:           false,
		Ed2kTTLHours:                0,
	}
}
