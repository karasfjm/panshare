package service

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// 插件机制：
//
//	plugins/<id>/manifest.json + 可执行文件   ← 镜像里带的可安装插件
//	<data>/plugins/<id>/                     ← 用户「安装」后落地的副本
//
// 安装后在独立进程里跑，核心只做反向代理。天翼云盘就是这么接进来的：
// 不用时完全不占资源，装坏了也不影响主服务。

type PluginManifest struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Version     string `json:"version"`
	Description string `json:"description"`
	Author      string `json:"author"`
	Entry       string `json:"entry"`

	Installed bool   `json:"installed"`
	Running   bool   `json:"running"`
	Port      int    `json:"port"`
	Error     string `json:"error"`
	Binary    bool   `json:"binary"`
}

type pluginProc struct {
	manifest PluginManifest
	cmd      *exec.Cmd
	addr     string
	cancel   context.CancelFunc
}

type Plugins struct {
	app   *App
	mu    sync.Mutex
	procs map[string]*pluginProc
}

func NewPlugins(a *App) *Plugins {
	return &Plugins{app: a, procs: map[string]*pluginProc{}}
}

func (p *Plugins) builtinDir(id string) string {
	return filepath.Join(p.app.Cfg.PluginDir, id)
}

func (p *Plugins) installedDir(id string) string {
	return filepath.Join(p.app.Cfg.InstalledPlugins(), id)
}

func (p *Plugins) entryPath(dir string, m PluginManifest) string {
	entry := m.Entry
	if entry == "" {
		entry = m.ID
	}
	name := entry
	if filepath.Ext(name) == "" {
		if isWindows() {
			name += ".exe"
		}
	}
	return filepath.Join(dir, name)
}

func isWindows() bool { return os.PathSeparator == '\\' }

// List 列出可安装 / 已安装插件。
func (p *Plugins) List() []PluginManifest {
	root := p.app.Cfg.PluginDir
	entries, err := os.ReadDir(root)
	if err != nil {
		// 插件目录不存在也要返回空数组，前端直接 .length 判断
		return []PluginManifest{}
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	out := make([]PluginManifest, 0, len(names))
	for _, id := range names {
		m, err := p.readManifest(p.builtinDir(id))
		if err != nil {
			continue
		}
		m.ID = id
		if m.Name == "" {
			m.Name = id
		}
		if _, err := os.Stat(p.installedDir(id)); err == nil {
			m.Installed = true
		}
		if _, err := os.Stat(p.entryPath(p.builtinDir(id), m)); err == nil {
			m.Binary = true
		}
		p.mu.Lock()
		if proc, ok := p.procs[id]; ok {
			m.Running = true
			m.Port = portOf(proc.addr)
			m.Error = ""
		}
		p.mu.Unlock()
		out = append(out, m)
	}
	return out
}

func (p *Plugins) readManifest(dir string) (PluginManifest, error) {
	var m PluginManifest
	data, err := os.ReadFile(filepath.Join(dir, "manifest.json"))
	if err != nil {
		return m, err
	}
	if err := json.Unmarshal(data, &m); err != nil {
		return m, err
	}
	return m, nil
}

// Install 把内置插件复制到数据目录并启动。
func (p *Plugins) Install(id string) error {
	m, err := p.readManifest(p.builtinDir(id))
	if err != nil {
		return fmt.Errorf("找不到插件 %s 的 manifest", id)
	}
	src := p.builtinDir(id)
	entry := p.entryPath(src, m)
	if _, err := os.Stat(entry); err != nil {
		return fmt.Errorf("插件 %s 还没有编译好的可执行文件（构建镜像时会生成）", id)
	}
	dst := p.installedDir(id)
	if err := copyTree(src, dst); err != nil {
		return err
	}
	p.app.Event("info", "plugin", fmt.Sprintf("已安装插件：%s v%s", m.Name, m.Version), "")
	return p.Start(id)
}

// Uninstall 停掉进程并删除安装副本（数据一同删除）。
func (p *Plugins) Uninstall(id string) error {
	_ = p.Stop(id)
	if err := os.RemoveAll(p.installedDir(id)); err != nil {
		return err
	}
	p.app.Event("info", "plugin", fmt.Sprintf("已卸载插件：%s", id), "")
	return nil
}

// Start 启动插件进程并等它健康。
func (p *Plugins) Start(id string) error {
	p.mu.Lock()
	if _, ok := p.procs[id]; ok {
		p.mu.Unlock()
		return nil
	}
	p.mu.Unlock()

	dir := p.installedDir(id)
	m, err := p.readManifest(dir)
	if err != nil {
		return fmt.Errorf("插件未安装：%s", id)
	}
	entry := p.entryPath(dir, m)
	if _, err := os.Stat(entry); err != nil {
		return fmt.Errorf("插件的可执行文件不存在")
	}
	port, err := freePort()
	if err != nil {
		return err
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	ctx, cancel := context.WithCancel(context.Background())
	// 把当前映射根和请求头传给插件：插件直接往映射根写 strm，
	// strm 里的地址必须是播放器能访问到的主服务，所以两个值都得跟着主服务走。
	cmd := exec.CommandContext(ctx, entry,
		"--addr", addr,
		"--data", filepath.Join(dir, "data"),
		"--strm-root", p.app.Store.Root,
		"--base-url", p.app.BaseURL(),
		"--play-prefix", "/api/plugins/"+id+"/proxy",
	)
	cmd.Dir = dir
	logs := filepath.Join(dir, "plugin.log")
	if f, err := os.OpenFile(logs, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644); err == nil {
		cmd.Stdout = f
		cmd.Stderr = f
	}
	if err := cmd.Start(); err != nil {
		cancel()
		return err
	}
	// 等健康检查
	ok := false
	for i := 0; i < 40; i++ {
		resp, err := http.Get("http://" + addr + "/healthz")
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode == 200 {
				ok = true
				break
			}
		}
		time.Sleep(250 * time.Millisecond)
	}
	if !ok {
		_ = cmd.Process.Kill()
		cancel()
		return fmt.Errorf("插件启动超时（看 %s）", logs)
	}
	p.mu.Lock()
	p.procs[id] = &pluginProc{manifest: m, cmd: cmd, addr: addr, cancel: cancel}
	p.mu.Unlock()
	go func() {
		_ = cmd.Wait()
		p.mu.Lock()
		delete(p.procs, id)
		p.mu.Unlock()
	}()
	p.app.Event("info", "plugin", fmt.Sprintf("插件已启动：%s（%s）", m.Name, addr), "")
	return nil
}

func (p *Plugins) Stop(id string) error {
	p.mu.Lock()
	proc, ok := p.procs[id]
	if ok {
		delete(p.procs, id)
	}
	p.mu.Unlock()
	if !ok {
		return nil
	}
	proc.cancel()
	if proc.cmd.Process != nil {
		_ = proc.cmd.Process.Kill()
	}
	return nil
}

// RestartRunning 重启所有正在跑的插件：映射根 / 请求头改过之后要让插件拿到新值。
func (p *Plugins) RestartRunning() {
	p.mu.Lock()
	ids := make([]string, 0, len(p.procs))
	for id := range p.procs {
		ids = append(ids, id)
	}
	p.mu.Unlock()
	for _, id := range ids {
		if err := p.Stop(id); err != nil {
			p.app.Event("warn", "plugin", fmt.Sprintf("插件 %s 停止失败：%v", id, err), "")
			continue
		}
		if err := p.Start(id); err != nil {
			p.app.Event("warn", "plugin", fmt.Sprintf("插件 %s 重启失败：%v", id, err), "")
		}
	}
}

// SyncBuiltins 让「已经装过」的内置插件跟上镜像里的版本。
//
// 安装时只是把 /app/plugins/<id> 复制到数据目录，之后重建容器并不会覆盖它 ——
// 结果就是插件修了 bug、用户还在跑旧二进制。这里在启动时比一次可执行文件，
// 有变化就停下、覆盖、再交给 Autostart 拉起。没装过的插件不动（仍由用户决定是否安装）。
func (p *Plugins) SyncBuiltins() {
	root := p.app.Cfg.InstalledPlugins()
	entries, err := os.ReadDir(p.app.Cfg.PluginDir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		id := e.Name()
		dst := filepath.Join(root, id)
		if _, err := os.Stat(dst); err != nil {
			continue // 没装过
		}
		src := p.builtinDir(id)
		m, err := p.readManifest(src)
		if err != nil {
			continue
		}
		if sameEntry(p.entryPath(src, m), p.entryPath(dst, m)) {
			continue
		}
		_ = p.Stop(id)
		if err := copyTree(src, dst); err != nil {
			p.app.Event("warn", "plugin", fmt.Sprintf("插件 %s 更新失败：%v", id, err), "")
			continue
		}
		p.app.Event("info", "plugin", fmt.Sprintf("插件 %s 已同步到镜像内版本", id), "")
	}
}

// sameEntry 比可执行文件的大小与修改时间：copyTree 会保留源文件 mtime，
// 所以同一个二进制不会每次启动都被判成「变了」。
func sameEntry(a, b string) bool {
	ia, err := os.Stat(a)
	if err != nil {
		return false
	}
	ib, err := os.Stat(b)
	if err != nil {
		return false
	}
	return ia.Size() == ib.Size() && ia.ModTime().Equal(ib.ModTime())
}

// Autostart 启动时把已安装的插件拉起来。
func (p *Plugins) Autostart() {
	root := p.app.Cfg.InstalledPlugins()
	entries, err := os.ReadDir(root)
	if err != nil {
		return
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		if _, err := p.readManifest(filepath.Join(root, e.Name())); err != nil {
			continue
		}
		if err := p.Start(e.Name()); err != nil {
			p.app.Event("warn", "plugin", fmt.Sprintf("插件 %s 自启失败：%v", e.Name(), err), "")
		}
	}
}

// Handler 返回插件的反代处理器；未运行时返回 nil。
func (p *Plugins) Handler(id string) (http.Handler, bool) {
	p.mu.Lock()
	proc, ok := p.procs[id]
	p.mu.Unlock()
	if !ok {
		return nil, false
	}
	target, err := url.Parse("http://" + proc.addr)
	if err != nil {
		return nil, false
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, "插件不可用: "+err.Error(), http.StatusBadGateway)
	}
	return proxy, true
}

func portOf(addr string) int {
	_, portStr, err := net.SplitHostPort(addr)
	if err != nil {
		return 0
	}
	var port int
	fmt.Sscanf(portStr, "%d", &port)
	return port
}

func freePort() (int, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port, nil
}

func copyTree(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		mode := info.Mode()
		if strings.HasSuffix(path, ".exe") || mode&0o111 != 0 {
			mode = 0o755
		}
		if err := os.WriteFile(target, data, mode); err != nil {
			return err
		}
		// 保留源文件时间：SyncBuiltins 靠 mtime 判断是否需要更新
		return os.Chtimes(target, time.Now(), info.ModTime())
	})
}
