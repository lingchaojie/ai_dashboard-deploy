# AI Gateway 部署

AI Gateway 的公开部署入口。此仓库提供安装脚本、Compose 配置和运维文档；应用源码保持私有，安装使用编译好的 GHCR 镜像。

## 一键安装

需要 Docker、Docker Compose v2.20+、Bash、curl、Python 3.10+。

```bash
curl -fsSL https://raw.githubusercontent.com/lingchaojie/ai_dashboard-deploy/main/deploy/install.sh \
  -o /tmp/ai-gateway-install.sh && bash /tmp/ai-gateway-install.sh
```

默认安装在当前目录的 `ai-gateway/`，监听 `127.0.0.1:8080`。首次访问创建管理员。公开镜像可匿名拉取，安装和更新均无需 GitHub CLI 或 Git 登录。

```bash
cd ai-gateway
./gateway.sh update
./gateway.sh backup
./gateway.sh rollback
```

完整的初始化、远程访问、HTTPS、配置、升级、备份、恢复和镜像更新认证说明见 [部署文档](deploy/README.md)。

镜像：`ghcr.io/lingchaojie/ai_dashboard:latest`（Linux amd64 / arm64）。

部署脚本由主构建仓库在 CI 成功后按文件清单同步；此仓库不包含应用源码、数据库或凭据。
