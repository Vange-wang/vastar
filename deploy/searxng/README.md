# Vastar 本地 SearXNG

使用 Docker 在本机运行 SearXNG，为 Vastar 提供免费搜索接口。搜索结果和上游可用性取决于网络出口。

需要已启动的 Docker Desktop Linux engine。仅网关发布 `127.0.0.1:8888`；SearXNG 无宿主端口。两个服务都设 `restart: "no"`，默认不开机自启，不修改 Docker Desktop 的登录自启设置。镜像和容器数据仅留本机，不推仓库。

在仓库根目录的 PowerShell 7 中运行：

```powershell
docker version # 必须同时看到 Server
$env:SEARXNG_SECRET = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
docker compose -f deploy/searxng/compose.yaml up -d
docker compose -f deploy/searxng/compose.yaml ps
```

每次启停命令保持该终端环境变量；新终端可重新生成随机值。它用于本地服务会话签名，不是付费 API 密钥。不要将其写进提交或日志。compose 已固定本轮实测 SearXNG 与 nginx 镜像 digest，重建复用相同版本。更新需显式替换 digest 后重新验证。

配置打开 JSON，只保留 Bing、Brave、Wikipedia 三个候选免费源；是否能用取决于出口，必须以实际搜索结果验证。SearXNG 不会自动绕过上游验证码。网关为本机所有者明确授权的 `/search` API 提供 Vastar robots 规则，其他路径拒绝；客户端 robots 检查仍保留。官方实现默认 robots 限制搜索查询，不能靠客户端忽略 robots 来接入。

发布版 `config/settings.example.json` 使用以下接入值：

```json
{
  "search_provider": "searxng",
  "search_endpoint": "http://127.0.0.1:8888/search",
  "allowed_hosts": ["127.0.0.1"],
  "private_hosts": ["127.0.0.1"],
  "allowed_ports": [80, 443, 8888]
}
```

上述是 `network` 块的增量字段。保留原 `allow_public_hosts`、`proxy_mode`、预算和授权字段；新增 8888 仅用于本地搜索端口。`execution.network` 保持 false。实体回环地址在通过 NetworkPolicy 后直连，公网继续走原 system proxy。

停止：`docker compose -f deploy/searxng/compose.yaml stop`。

卸载本实例：`docker compose -f deploy/searxng/compose.yaml down`。本方案未挂载数据卷，不使用 `down --volumes` 或全局 prune；保留下载镜像供复用。

需要常驻时由使用者决定，再将 restart 改为 `unless-stopped` 并评估 Docker Desktop 登录自启；默认不启用。实例停机时搜索应明确返回网络错误，不能静默回落 DDG。切回 DDG 时将 provider 设为 `duckduckgo`、endpoint 置空；DDG 验证码仍会如实报错。

依据：[官方容器部署](https://docs.searxng.org/admin/installation-docker.html)、[默认配置合并](https://docs.searxng.org/admin/settings/settings.html)、[官方 robots 路由](https://github.com/searxng/searxng/blob/master/searx/webapp.py)。
