export interface Account {
  id: string
  name: string
  uid: string
  user_name: string
  source: string
  has_cookies: boolean
  copy_play: boolean
  is_default: boolean
  status: string
  last_error: string
  configs: number
  created_at: number
}

export interface StrmPath {
  remote_id: string
  remote_path: string
  local_dir: string
}

export interface StrmConfig {
  id: string
  name: string
  account_id: string
  account_name: string
  paths?: StrmPath[]
  remote_id: string
  remote_path: string
  local_dir: string
  mode: string
  include_exts: string
  meta_exts?: string
  strm_base?: string
  all_files: boolean
  overwrite_mode?: 'always' | 'never'
  exclude_keywords?: string
  min_file_size_mb?: number
  skip_pending?: boolean
  pending_dirs?: string
  concurrency?: number
  cron_full?: string
  cleanup_stale_count?: number
  watch_enabled: boolean
  watch_delay_sec: number
  watch_interval: number
  last_check_at: number
  sched_enabled: boolean
  sched_minutes: number
  download_meta: boolean
  last_run_at: number
  last_status: string
  last_message: string
  files: number
  running: boolean
  created_at?: number
  /** TG 专属指令的代号：/inc_<代号> 增量、/full_<代号> 全量 */
  tg_alias?: string
}

export interface CleanupItem {
  file_id: string
  path: string
  name: string
}

export interface CleanupConfirmation {
  id: string
  config_id: string
  config_name: string
  stale_sig: string
  items: CleanupItem[]
  total: number
  ratio: number
  state: string
  created_at: number
  expires_at: number
}

export interface ShareTask {
  id: string
  url: string
  title: string
  account_id: string
  account_name: string
  share_code: string
  receive_code: string
  dir_cid: string
  dir_name: string
  local_dir: string
  no_transfer: boolean
  my_share_code: string
  my_receive_code: string
  status: string
  message: string
  files: number
  created_at: number
}

export interface Ed2kTask {
  id: string
  name: string
  size: number
  url: string
  account_id: string
  account_name: string
  dir_cid: string
  local_dir: string
  transfer_on_add: boolean
  state: string
  message: string
  pickcode: string
  created_at: number
}

export interface EmbyServer {
  id: string
  name: string
  url: string
  enabled: boolean
  is_default: boolean
  has_key: boolean
  created_at: number
}

/** Emby 反代状态（独立端口；端口可在页面手填，留空用 PS_EMBY_PROXY_PORT） */
export interface EmbyProxyInfo {
  enabled: boolean
  /** 页面上的开关：true = 开 */
  on: boolean
  port: number
  /** 真正在监听的端口（重绑瞬间可能是 0） */
  listen_port: number
  /** 环境变量里的默认端口 */
  port_default: number
  /** 页面上手填的端口，0 = 没填 */
  port_custom: number
  running: boolean
  target: string
  last_error: string
  uptime: string
  requests: number
  redirects: number
  server_name?: string
  server_url?: string
  base_url?: string
}

export interface InstantTarget {
  account_id: string
  account_name: string
  dir_cid: string
  dir_path: string
  enabled: boolean
}

export interface InstantMapping {
  emby_user_id: string
  emby_user_name: string
  account_id: string
  account_name: string
  updated_at: number
}

export interface Settings {
  site_name: string
  strm_root: string
  public_base_url: string
  default_exts: string
  default_meta_exts: string
  watch_delay_sec: number
  watch_interval_sec: number
  download_meta: boolean
  api_qps: number
  cleanup_max_delete_ratio?: number
  cleanup_stable_rounds?: number
  cleanup_confirm_required?: boolean
  cleanup_confirm_ttl_hours?: number
  default_concurrency?: number
  delete_enabled: boolean
  delete_grace_minutes: number
  delete_cloud_when_local_missing: boolean
  delete_local_when_cloud_deleted: boolean
  delete_meta_with_strm: boolean
  tg_enabled: boolean
  tg_bot_token: string
  tg_chat_id: string
  tg_commands: boolean
  tg_proxy: string
  /** 微信通知：企业微信自建应用 + 群机器人，填了哪个发哪个 */
  wechat_enabled: boolean
  wechat_corp_id: string
  wechat_agent_id: string
  wechat_secret: string
  /** 接收成员，留空默认 @all */
  wechat_to_user: string
  /** 群机器人 Webhook，也可只填 key */
  wechat_robot_url: string
  /** 允许在应用聊天窗口里发指令 / 发链接 */
  wechat_commands: boolean
  /** 只推这些事件类型，逗号分隔；空 = 全部，「-」= 都不推 */
  wechat_notify_kinds: string
  /** 企业微信「接收消息」回调：配置企业可信IP的前置条件 */
  /** 地址里的凭据，形如 /api/wechat/app/callback?token=API */
  wechat_app_api: string
  wechat_app_token: string
  wechat_app_aeskey: string
  /** 微信服务号（公众号）：模板消息推送 + 公网回调 */
  wechat_mp_enabled: boolean
  wechat_mp_app_id: string
  wechat_mp_secret: string
  wechat_mp_template_id: string
  /** 接收 OpenID，多个用逗号分隔 */
  wechat_mp_to_user: string
  /** 公网地址（形如 https://ps.example.com），留空则用 public_base_url */
  wechat_mp_base: string
  /** 回调 Token，填在公众号后台「服务器配置」里 */
  wechat_mp_token: string
  /** EncodingAESKey，安全模式才需要 */
  wechat_mp_aes_key: string
  ed2k_account_id: string
  ed2k_dir_cid: string
  /** 只用于页面回显：选过的目录名 */
  ed2k_dir_name: string
  /** 上次选的本地目录（相对映射根） */
  ed2k_local_dir: string
  ed2k_transfer_on_add: boolean
  ed2k_ttl_hours: number
  share_dir_cid: string
  share_dir_name: string
  share_account_id: string
  share_local_dir: string
  /** Emby 反代：true = 关掉 */
  emby_proxy_off: boolean
  /** 115 秒传：true = 关掉「播放前预秒传」，改成播放到 /p115/instant 时再同步秒传 */
  instant_prewarm_off: boolean
}

export interface AppEvent {
  id: number
  level: string
  kind: string
  message: string
  detail: string
  created_at: number
}

export interface Deletion {
  id: number
  kind: string
  account_id: string
  file_id: string
  path: string
  strm_path: string
  state: string
  message: string
  created_at: number
}

export interface PluginInfo {
  id: string
  name: string
  version: string
  description: string
  author: string
  installed: boolean
  running: boolean
  port: number
  error: string
  binary: boolean
}

export interface Stats {
  accounts: number
  accounts_ok: number
  configs: number
  configs_running: number
  files: number
  shares: number
  ed2k: number
  ed2k_done: number
  emby_servers: number
  strm_root: string
  base_url: string
  uptime: string
  version: string
  settings: Settings
  events: AppEvent[]
}

export interface DirEntry {
  id: string
  name: string
}
