package server

import (
	"io"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// 微信通知的测试、状态查询与服务号回调。
// 发送逻辑在 service/wechat.go（企业微信应用 + 群机器人）
// 与 service/wechatmp.go（服务号模板消息 + 公网回调）。

func (s *Server) testWeChat(w http.ResponseWriter, r *http.Request) {
	// 同步发：失败就把微信的原话带回去，别让页面显示「已发出」实际没到。
	// ?only=mp 只测服务号，不带则测企业微信应用 / 群机器人。
	only := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("only")))
	if only == "mp" {
		if err := s.app.WeChatMP.Test(); err != nil {
			writeErr(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, map[string]any{"ok": true})
		return
	}
	if err := s.app.WeChat.Test(); err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

// wxAppMenuPush 把指令表提交成企业微信「自定义菜单」，覆盖原有菜单。
func (s *Server) wxAppMenuPush(w http.ResponseWriter, r *http.Request) {
	if err := s.app.WeChatApp.PushMenu(); err != nil {
		writeErr(w, http.StatusBadGateway, err)
		return
	}
	writeJSON(w, map[string]any{"ok": true})
}

func (s *Server) wechatStatus(w http.ResponseWriter, r *http.Request) {
	out := s.app.WeChat.Status()
	for k, v := range s.app.WeChatMP.Status() {
		out[k] = v
	}
	for k, v := range s.app.WeChatApp.Status() {
		out[k] = v
	}
	writeJSON(w, out)
}

// wxMPCallback 是公众号后台「服务器配置」指向的地址。
//
// GET  保存配置时校验 URL：验签通过后把 echostr 原样回出去
// POST 用户发消息 / 关注事件：回一条被动文本（空串就回 success 表示不回复）
//
// 出错时不返回 5xx——微信会因此反复重试、还把地址标记成不可用；
// 这里统一回 success，错误只写日志。
func (s *Server) wxMPCallback(w http.ResponseWriter, r *http.Request) {
	m := s.app.WeChatMP

	switch r.Method {
	case http.MethodGet:
		echo, err := m.Verify(r.URL.Query())
		if err != nil {
			log.Printf("[warn] 微信服务号回调校验失败：%v", err)
			http.Error(w, err.Error(), http.StatusForbidden)
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, echo)

	case http.MethodPost:
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			log.Printf("[warn] 微信服务号回调读取失败：%v", err)
			_, _ = io.WriteString(w, "success")
			return
		}
		reply, err := m.Receive(r.URL.Query(), body)
		if err != nil {
			log.Printf("[warn] 微信服务号回调处理失败：%v", err)
			_, _ = io.WriteString(w, "success")
			return
		}
		if reply == "" {
			_, _ = io.WriteString(w, "success")
			return
		}
		w.Header().Set("Content-Type", "text/xml; charset=utf-8")
		_, _ = io.WriteString(w, reply)

	default:
		w.Header().Set("Allow", "GET, POST")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// wxAppCallback 是企业微信后台「接收消息 → 设置API接收」指向的地址。
//
// 它存在的直接原因：企业微信不允许在没配「可信域名」或「接收消息URL」时
// 配置「企业可信IP」。配了这个地址就能加 IP，通知才发得出去。
//
// GET  保存配置时校验：解密 echostr 后回明文
// POST 成员发消息：解出来回一句确认，回包同样加密
func (s *Server) wxAppCallback(w http.ResponseWriter, r *http.Request) {
	m := s.app.WeChatApp

	switch r.Method {
	case http.MethodGet:
		echo, err := m.Verify(r.URL.Query())
		if err != nil {
			log.Printf("[warn] 企业微信回调校验失败：%v（参数=%s ua=%q remote=%s）",
				err, describeQuery(r.URL.Query()), r.UserAgent(), r.RemoteAddr)
			http.Error(w, err.Error(), http.StatusForbidden)
			return
		}
		log.Printf("[info] 企业微信回调校验通过（参数=%s ua=%q）",
			describeQuery(r.URL.Query()), r.UserAgent())
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, echo)

	case http.MethodPost:
		// 少数情况下企业微信把校验参数带在 query 上、用 POST 发过来，
		// 体里没有 Encrypt。这种按 GET 校验处理，别直接判失败。
		if r.URL.Query().Get("echostr") != "" {
			echo, err := m.Verify(r.URL.Query())
			if err != nil {
				log.Printf("[warn] 企业微信回调校验失败(POST)：%v（参数=%s ua=%q）",
					err, describeQuery(r.URL.Query()), r.UserAgent())
				http.Error(w, err.Error(), http.StatusForbidden)
				return
			}
			log.Printf("[info] 企业微信回调校验通过(POST 携带 echostr)")
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			_, _ = io.WriteString(w, echo)
			return
		}
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			log.Printf("[warn] 企业微信回调读取失败：%v", err)
			_, _ = io.WriteString(w, "success")
			return
		}
		reply, err := m.Receive(r.URL.Query(), body)
		if err != nil {
			log.Printf("[warn] 企业微信回调处理失败：%v", err)
			_, _ = io.WriteString(w, "success")
			return
		}
		if reply == "" {
			_, _ = io.WriteString(w, "success")
			return
		}
		w.Header().Set("Content-Type", "text/xml; charset=utf-8")
		_, _ = io.WriteString(w, reply)

	default:
		w.Header().Set("Allow", "GET, POST")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// describeQuery 把回调请求带的参数名和长度写成一行日志。
// 不落明文：echostr 和 msg_signature 都算敏感值，长度足够判断「有没有带」。
func describeQuery(q url.Values) string {
	parts := make([]string, 0, 4)
	for _, k := range []string{"msg_signature", "signature", "timestamp", "nonce", "echostr", "token"} {
		if v := q.Get(k); v != "" {
			parts = append(parts, k+"("+strconv.Itoa(len(v))+")")
		}
	}
	if len(parts) == 0 {
		return "无（没带任何参数）"
	}
	return strings.Join(parts, " ")
}
