package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// TMDB 热门影视背景。
//
// 登录页是公开页面，不能把 API Key 下发给浏览器，所以由后端取图再回传。
// 代理复用 Telegram 的同一个设置（App.SharedProxy），国内机器只要配一次。
type TMDB struct {
	app *App

	mu       sync.Mutex
	fetchMu  sync.Mutex
	client   *http.Client
	proxyKey string

	paths   []string
	pathsAt time.Time

	images map[string]tmdbImage

	lastErr   string
	lastErrAt time.Time
}

type tmdbImage struct {
	data  []byte
	ctype string
	at    time.Time
}

type tmdbList struct {
	Results []struct {
		BackdropPath string `json:"backdrop_path"`
	} `json:"results"`
}

const (
	tmdbMetaTTL    = 30 * time.Minute
	tmdbImageTTL   = 12 * time.Hour
	tmdbMaxImages  = 12
	tmdbTimeout    = 15 * time.Second
	tmdbMaxImgSize = 3 << 20
)

// 没配 API Key 时，从 TMDB 首页的热门横幅里抠 backdrop 路径。
// 首页是公开页面，一样走共享代理；这样不填 Key 也能随机换背景。
var tmdbWebBackdrop = regexp.MustCompile(`w1920_and_h600_multi_faces_filter\([^)]*\)/([A-Za-z0-9]+\.jpg)`)

// Background 随机返回一张 TMDB 热门影视 backdrop 的原始字节。
func (t *TMDB) Background() (data []byte, ctype string, err error) {
	defer func() {
		if err != nil {
			t.noteErr(err)
		}
	}()

	key := strings.TrimSpace(t.app.Cfg.TMDBAPIKey)
	client, err := t.httpClient()
	if err != nil {
		return nil, "", err
	}

	paths, err := t.backdrops(client, key)
	if err != nil {
		return nil, "", err
	}
	if len(paths) == 0 {
		return nil, "", errors.New("TMDB 暂时没有带背景图的热门条目")
	}

	// 从随机位置开始顺序尝试，最多三张；某张图失效也不会让登录页开天窗。
	start := rand.Intn(len(paths))
	var lastErr error
	for i := 0; i < len(paths) && i < 3; i++ {
		path := paths[(start+i)%len(paths)]
		data, ctype, err = t.image(client, path)
		if err == nil {
			return data, ctype, nil
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = errors.New("取背景图失败")
	}
	return nil, "", lastErr
}

// httpClient 复用共享代理；代理串变了就重建 client。
func (t *TMDB) httpClient() (*http.Client, error) {
	proxyStr := t.app.SharedProxy()

	t.mu.Lock()
	defer t.mu.Unlock()
	if t.client != nil && t.proxyKey == proxyStr {
		return t.client, nil
	}
	client, err := newProxyClient(proxyStr, tmdbTimeout)
	if err != nil {
		return nil, err
	}
	t.client, t.proxyKey = client, proxyStr
	return client, nil
}

// backdrops 返回热门影视的背景图路径列表，带 30 分钟内存缓存。
func (t *TMDB) backdrops(client *http.Client, key string) ([]string, error) {
	t.mu.Lock()
	if len(t.paths) > 0 && time.Since(t.pathsAt) < tmdbMetaTTL {
		paths := append([]string(nil), t.paths...)
		t.mu.Unlock()
		return paths, nil
	}
	t.mu.Unlock()

	t.fetchMu.Lock()
	defer t.fetchMu.Unlock()
	// 等锁期间可能别的请求已经填好了
	t.mu.Lock()
	if len(t.paths) > 0 && time.Since(t.pathsAt) < tmdbMetaTTL {
		paths := append([]string(nil), t.paths...)
		t.mu.Unlock()
		return paths, nil
	}
	t.mu.Unlock()

	var paths []string
	var err error
	if key == "" {
		paths, err = t.fetchWebBackdrops(client)
	} else {
		paths, err = t.fetchBackdrops(client, key)
	}
	if err != nil {
		return nil, err
	}
	t.mu.Lock()
	t.paths, t.pathsAt = paths, time.Now()
	t.mu.Unlock()
	return append([]string(nil), paths...), nil
}

func (t *TMDB) fetchBackdrops(client *http.Client, key string) ([]string, error) {
	page := rand.Intn(5) + 1

	var list tmdbList
	if err := t.getJSON(client, "trending/movie/week", key, page, &list); err != nil {
		return nil, err
	}
	paths := collectBackdrops(list)
	if len(paths) == 0 {
		// trending 偶尔整页都没有 backdrop，退回 popular 保底
		list = tmdbList{}
		if err := t.getJSON(client, "movie/popular", key, page, &list); err != nil {
			return nil, err
		}
		paths = collectBackdrops(list)
	}
	return paths, nil
}

// fetchWebBackdrops 是从 TMDB 公开首页抠热门横幅的兜底路径。
// 没有 API Key 时用这条，依然从 TMDB 热门内容随机取图，且共用同一个代理。
func (t *TMDB) fetchWebBackdrops(client *http.Client) ([]string, error) {
	resp, err := client.Get("https://www.themoviedb.org/")
	if err != nil {
		return nil, fmt.Errorf("请求 TMDB 热门页失败：%v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return nil, fmt.Errorf("TMDB 热门页返回 HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, err
	}
	matches := tmdbWebBackdrop.FindAllSubmatch(body, -1)
	seen := map[string]bool{}
	paths := make([]string, 0, len(matches))
	for _, m := range matches {
		p := "/" + string(m[1])
		if seen[p] {
			continue
		}
		seen[p] = true
		paths = append(paths, p)
	}
	return paths, nil
}

func collectBackdrops(list tmdbList) []string {
	seen := map[string]bool{}
	paths := make([]string, 0, len(list.Results))
	for _, r := range list.Results {
		p := strings.TrimSpace(r.BackdropPath)
		if !strings.HasPrefix(p, "/") || strings.Contains(p, "..") || seen[p] {
			continue
		}
		seen[p] = true
		paths = append(paths, p)
	}
	return paths
}

func (t *TMDB) getJSON(client *http.Client, endpoint, key string, page int, dst any) error {
	u, err := url.Parse("https://api.themoviedb.org/3/" + endpoint)
	if err != nil {
		return err
	}
	q := u.Query()
	q.Set("language", "zh-CN")
	q.Set("page", strconv.Itoa(page))
	bearer := strings.HasPrefix(strings.ToLower(key), "eyj")
	if !bearer {
		q.Set("api_key", key)
	}
	u.RawQuery = q.Encode()

	req, err := http.NewRequest(http.MethodGet, u.String(), nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	if bearer {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("请求 TMDB 失败：%s", scrubTMDB(err.Error(), key))
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("TMDB 返回 HTTP %d：%s", resp.StatusCode, scrubTMDB(strings.TrimSpace(string(body)), key))
	}
	if err := json.Unmarshal(body, dst); err != nil {
		return fmt.Errorf("解析 TMDB 数据失败：%v", err)
	}
	return nil
}

// image 取单张 backdrop，按路径缓存，避免每次登录都打 TMDB 图片 CDN。
func (t *TMDB) image(client *http.Client, path string) ([]byte, string, error) {
	if !strings.HasPrefix(path, "/") || strings.Contains(path, "..") {
		return nil, "", errors.New("非法的背景图路径")
	}

	t.mu.Lock()
	if im, ok := t.images[path]; ok && time.Since(im.at) < tmdbImageTTL {
		data, ctype := im.data, im.ctype
		t.mu.Unlock()
		return data, ctype, nil
	}
	t.mu.Unlock()

	resp, err := client.Get("https://image.tmdb.org/t/p/w1280" + path)
	if err != nil {
		return nil, "", fmt.Errorf("取背景图失败：%v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return nil, "", fmt.Errorf("背景图返回 HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, tmdbMaxImgSize+1))
	if err != nil {
		return nil, "", err
	}
	if len(data) > tmdbMaxImgSize {
		return nil, "", errors.New("背景图太大")
	}
	ctype := resp.Header.Get("Content-Type")
	if !strings.HasPrefix(ctype, "image/") {
		ctype = http.DetectContentType(data)
	}

	t.mu.Lock()
	if t.images == nil {
		t.images = map[string]tmdbImage{}
	}
	t.images[path] = tmdbImage{data: data, ctype: ctype, at: time.Now()}
	if len(t.images) > tmdbMaxImages {
		oldest, oldAt := "", time.Time{}
		for p, im := range t.images {
			if oldest == "" || im.at.Before(oldAt) {
				oldest, oldAt = p, im.at
			}
		}
		delete(t.images, oldest)
	}
	t.mu.Unlock()
	return data, ctype, nil
}

// noteErr 同一种错误一分钟只记一次，免得登录页一直刷日志。
func (t *TMDB) noteErr(err error) {
	if err == nil {
		return
	}
	msg := err.Error()
	t.mu.Lock()
	defer t.mu.Unlock()
	if msg == t.lastErr && time.Since(t.lastErrAt) < time.Minute {
		return
	}
	t.lastErr, t.lastErrAt = msg, time.Now()
	log.Printf("TMDB 背景图: %s", msg)
}

func scrubTMDB(msg, key string) string {
	if key != "" {
		msg = strings.ReplaceAll(msg, key, "***")
	}
	return msg
}
