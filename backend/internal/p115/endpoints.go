package p115

// 115 的接口地址集中放这里。官方偶尔会改域名/路径，出问题时只改这一个文件。
const (
	WebAPI      = "https://webapi.115.com"
	ProAPI      = "https://proapi.115.com"
	QRCodeAPI   = "https://qrcodeapi.115.com"
	UploadAPI   = "https://uplb.115.com"
	PassportAPI = "https://passportapi.115.com"

	// 账号
	pathUserInfo = "/user/info"

	// 文件
	pathFiles    = "/files"
	pathFilesAdd = "/files/add"
	// 删文件（进回收站）：旧接口 /files/delete 现在一律回「服务器开小差了」，
	// 改走 /rb/delete。参数仍是 fid[0]=… 加 pid。
	pathFilesDelete  = "/rb/delete"
	pathFilesCopy    = "/files/copy"
	pathFilesMove    = "/files/move"
	pathFilesDetails = "/files/details"
	pathFilesDown    = "/files/download"
	pathChromeDown   = "/app/chrome/downurl"
	// 安卓客户端取直链通道：payload 用 pick_code，且**不需要 Cookie**。
	// 这是 Community 里能拿到「只校验 UA」直链的那条路（参考 MoviePilot p115strmhelper）。
	pathAndroidDown = "/android/2.0/ufile/download"

	// 生活事件（网盘操作日志）：事件监控靠它拿「谁被加了/删了/改名了」。
	// 注意是 webapi 的 behavior/detail，proapi 的 app/2.0 版本已经 404。
	pathBehaviorDetail = "/behavior/detail"

	// 分享
	pathShareSnap     = "/share/snap"
	pathShareReceive  = "/share/receive"
	pathShareSend     = "/share/send"
	pathShareUpdate   = "/share/update"
	pathShareDownload = "/share/download"
	// 2026-09 起网页版 /share/download 对第三方客户端一律回「服务器开小差了」，
	// 分享直链改走 proapi 的 app 通道（body 要 m115 编码 + 115 客户端 UA）。
	pathAppShareDownurl = "/app/share/downurl"

	// 离线 / 云下载（ed2k）：
	//   - webapi.115.com/offline/* 已整体下架（一律 404）；
	//   - 115.com/web/lixian 老网关只认浏览器登录态，第三方客户端提交会回
	//     「Not Found」或「错误的链接」；
	//   - 现在官方客户端走 clouddownload.115.com：
	//     提交用 lixianssp（body 要 m115 编码 + 115 客户端 UA），
	//     查询/列表/删除用 /web/ 的 ac=get_user_task / task_lists / task_del。
	URLCloudDownAdd  = "https://clouddownload.115.com/lixianssp/?ac=add_task_url"
	URLCloudDownWeb  = "https://clouddownload.115.com/web/"
	// 云下载通道要求的客户端版本号与 UA（照抄 115 安卓客户端）
	CloudDownAppVer = "36.2.28"
	CloudDownUA     = "Mozilla/5.0 115disk/" + CloudDownAppVer + " 115Browser/" + CloudDownAppVer +
		" 115wangpan_android/" + CloudDownAppVer

	// 秒传
	pathUploadInfo   = "/3.0/getuploadinfo.php"
	// 秒传初始化。2026 版必须带 .php，少了就是 404；
	// 且要自算 sig / token（见 instant.go）。
	pathUploadInit = "/4.0/initupload.php"
	// 秒传签名用的 userkey（webapi 的 files/uploadinfo，不是 proapi 的 upload_key）
	pathUploadUserkey = "/files/uploadinfo"
	pathUploadResume = "/4.0/upload/resume"

	// 扫码登录
	pathQRToken  = "/api/1.0/web/1.0/token"
	pathQRStatus = "/get/status/"

	// 默认浏览器 UA：115 直链（f=1）要求取链接与请求 CDN 的 UA 一致
	UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
		"(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
