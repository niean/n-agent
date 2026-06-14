<!-- SUMMARY: {{项目名称}}开发中的经验教训，AI自主维护 -->
# 项目教训

AI 自主维护，人工可通过提示或建议触发新增/修正。
项目教训绑定{{项目名称}}，不随 Harness 模板提取。

---

### P001: 跨 Compose 项目调用必须显式声明 external network

现象：n-agent 容器调用 `http://nkb.localhost` 或 `http://n-kb:8212` 时，TCP connect 成功但 HTTP 收不到响应，httpx 抛 RemoteProtocolError("Server disconnected without sending a response.")，被 KnowledgeToolExecutor 通用 except 吞成 generic "knowledge search failed"。

根因：n-agent 与 n-kb 由不同 docker compose 项目独立创建，n-agent 默认只连入 `n-agent_default` 网络。Docker Desktop 的内部 DNS 仍能把 `n-kb` 解析到代理 IP（198.18.x.x），TCP 经 NAT 看似可达，但流量没有路由到目标网络，HTTP 响应被丢弃。

教训：跨 Compose 项目消费外部服务时，必须在调用方 compose 文件显式声明被调用方的 network 为 external 并把服务加入；同时 base_url 通过 service name 直连，禁止用 .localhost 这类依赖宿主机 DNS resolver 的占位主机名。诊断 generic 错误前优先检查容器网络拓扑（`docker inspect <container> --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}'`），不要只看 base_url 字符串。

来源：bug fix 260614 search_knowledge 功测失败
