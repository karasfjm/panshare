// 公网出口 IP 检测（对齐 Python IP_SERVICES 的「6 接口容灾」）：
// 依次试，第一个拿到合法 IPv4 的就用，并记录来源。全部走标准库 http。
package core

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// IPService 一条检测服务（名字 + URL）。
type IPService struct {
	Name string
	URL  string
}

// IPServices 对齐 Python 的 6 个接口。
var IPServices = []IPService{
	{"ipify", "https://api.ipify.org"},
	{"ifconfig.me", "https://ifconfig.me/ip"},
	{"ip.sb", "https://api.ip.sb/ip"},
	{"3322", "http://ip.3322.net"},
	{"ipip.net", "https://myip.ipip.net"},
	{"cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"},
}

// ipRe 匹配正文里的第一个 IPv4。
var ipRe = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}\b`)

// IsIPv4 全串校验。
func IsIPv4(s string) bool {
	return ipRe.String() != "" && func() bool {
		locs := ipRe.FindStringIndex(s)
		return locs != nil && locs[0] == 0 && locs[1] == len(s)
	}()
}

// ParseIPBody 按服务名从响应正文抠 IP（cloudflare 是 key=value 格式）。
// 纯函数，方便单测。
func ParseIPBody(service, body string) string {
	body = strings.TrimSpace(body)
	if service == "cloudflare" {
		for _, line := range strings.Split(body, "\n") {
			if v, ok := strings.CutPrefix(strings.TrimSpace(line), "ip="); ok {
				body = strings.TrimSpace(v)
				break
			}
		}
	}
	m := ipRe.FindString(body)
	if m == "" {
		return ""
	}
	// 每段 0-255 才算数，别把 "999.1.1.1" 这种当 IP
	for _, seg := range strings.Split(m, ".") {
		n := 0
		if len(seg) == 0 || len(seg) > 3 {
			return ""
		}
		for _, r := range seg {
			if r < '0' || r > '9' {
				return ""
			}
			n = n*10 + int(r-'0')
		}
		if n > 255 {
			return ""
		}
	}
	return m
}

// fetchIP 用给定 client 抓一个接口，返回 (ip, error)。
func fetchIP(client *http.Client, svc IPService) (string, error) {
	req, err := http.NewRequest(http.MethodGet, svc.URL, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", "PanShare")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%s: HTTP %d", svc.Name, resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if err != nil {
		return "", err
	}
	return ParseIPBody(svc.Name, string(raw)), nil
}

// DetectIP 依次试 IPServices，第一个拿到 IP 的就用（来源记到 *source）。
// proxy 形如 http://host:port，空表示直连。
func DetectIP(proxy string) (ip, source string) {
	transport := &http.Transport{}
	if proxy != "" {
		u, err := url.Parse(proxy)
		if err == nil && u.Scheme != "" {
			transport.Proxy = http.ProxyURL(u)
		}
	}
	client := &http.Client{Timeout: 8 * time.Second, Transport: transport}
	for _, svc := range IPServices {
		text, err := fetchIP(client, svc)
		if err != nil || text == "" || !IsIPv4(text) {
			continue
		}
		return text, svc.Name
	}
	return "", ""
}
