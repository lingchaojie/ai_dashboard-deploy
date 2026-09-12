# AI Gateway 部署与维护

应用源码仓库保持私有。本公开仓库只包含部署脚本、Compose 配置和运维文档；安装使用编译好的容器镜像，不下载应用源码。

## 一键安装（免登录）

服务器准备 Docker Engine、Docker Compose v2.20+、Bash、curl、Python 3.10+ 后，执行：

```bash
curl -fsSL https://raw.githubusercontent.com/lingchaojie/ai_dashboard-deploy/main/deploy/install.sh \
  -o /tmp/ai-gateway-install.sh && bash /tmp/ai-gateway-install.sh
```

脚本不需要 Git、GitHub CLI 或 GitHub token。默认安装在当前目录的 `ai-gateway/`，端口为 `127.0.0.1:8080`。应用镜像 `ghcr.io/lingchaojie/ai_dashboard:latest` 已公开，支持匿名拉取。源码仓库保持私有。

脚本一次下载公开部署仓库的完整提交快照，从中提取明确列出的部署文件，并记录提交 SHA；不调用 GitHub API，因此不依赖匿名 API 配额。重复执行保留已有 `.env`、Compose、Caddyfile 和数据；脚本不自动安装 Docker 或更改系统防火墙。以上命令会先完整下载，成功后再执行。

自定义目录和端口：

```bash
INSTALL_DIR="$HOME/ai-gateway" SERVER_PORT=8088 bash /tmp/ai-gateway-install.sh
```

固定镜像版本：首次安装时设置 `GATEWAY_IMAGE=ghcr.io/lingchaojie/ai_dashboard:sha-完整提交SHA`；`GATEWAY_REF` 用于选择**部署仓库**的提交或已有标签，和应用镜像版本相互独立。环境变量只在首次生成 `.env` 时生效，之后编辑安装目录的 `.env`。

## 管理员与访问地址

默认绑定 `127.0.0.1:8080`。本机打开 `http://127.0.0.1:8080`，首次创建管理员；密码至少 12 字节，没有固定默认密码。现有数据库的管理员会保留。

远程服务器可先建立 SSH 隧道完成初始化：

```bash
ssh -L 8080:127.0.0.1:8080 your-user@your-server
```

然后浏览器访问本机 `http://127.0.0.1:8080`。完成初始化后再配置域名 HTTPS，或者自行设置反向代理。管理员信息、账号令牌和备份均不提交到 GitHub。

## 配置与 HTTPS

| `.env` 设置 | 默认值 | 说明 |
|---|---|---|
| `GATEWAY_IMAGE` | `ghcr.io/lingchaojie/ai_dashboard:latest` | 支持 `main`、`sha-完整提交SHA`、版本标签和 `@sha256:摘要` |
| `SERVER_PORT` | `8080` | 本机端口，1–65535 |
| `BIND_HOST` | `127.0.0.1` | IPv4 监听地址，直接开放时自行设为 `0.0.0.0` |
| `COMPOSE_PROJECT_NAME` | `ai-gateway` | 同一主机多实例须使用不同名称、端口、目录 |
| `GATEWAY_UID/GATEWAY_GID` | 安装用户 ID；root 安装时 10001 | 容器用户与 `data/` 所有者必须一致；容器不以 root 运行 |
| `TZ` | `Asia/Shanghai` | 时区 |
| `DOMAIN` | 空 | 填入域名后自动加入 Caddy HTTPS 容器 |

配置采用简单 `KEY=value`，不支持 shell 表达式、引号、变量插值；不会用 `source` 执行 `.env`。修改 UID/GID 时需自行调整现有数据的所有权。

要启用自动 HTTPS，把域名 DNS 指向服务器，开放 80/tcp、443/tcp（可选 443/udp），完成管理员初始化后，在 `.env` 设置 `DOMAIN=dashboard.example.com`，执行：

```bash
cd ~/ai-gateway
./gateway.sh start
```

Caddy 自动申请证书，并保留原始 Host 和 Origin 供应用同源校验。证书使用 Compose 命名卷保存。取消 `DOMAIN` 后再次 `start` 会移除该实例的 Caddy 容器，数据卷保留。只使用现有 Nginx/Caddy 时保留 `DOMAIN` 为空，将请求转到 `127.0.0.1:8080` 并保留 Host；Go `/healthz` 可用于上游健康检查。

## 日常维护

在安装目录执行：

```bash
./gateway.sh status
./gateway.sh logs                       # 最近 100 行并持续跟随，Ctrl-C 退出
./gateway.sh restart
./gateway.sh stop                       # 停止服务，保留容器与全部数据
./gateway.sh start
./gateway.sh update                     # 拉取 .env 配置的镜像
./gateway.sh update ghcr.io/lingchaojie/ai_dashboard:main
./gateway.sh update ghcr.io/lingchaojie/ai_dashboard:sha-完整提交SHA
./gateway.sh backup
./gateway.sh rollback                   # 恢复上一次成功升级前的镜像与数据
./gateway.sh restore backups/gateway-时间戳.tar.gz
```

升级先拉取镜像，成功后停止 dashboard、备份数据与配置，随后启动并等待健康检查。拉取失败不影响旧服务；新版本启动失败时恢复升级前的数据库、加密密钥和镜像，并以失败状态退出。Caddy 在应用短暂停机期间可能返回 502。停止宽限为 120 秒，覆盖应用 100 秒的请求收尾时间。

`rollback` 和 `restore` 会把账号、用量历史等恢复到备份时点，之后产生的修改不在恢复后的数据库中。恢复前会另做一份安全备份；如果当前数据库或加密密钥缺失，则保留残留数据后直接恢复。原数据目录保留为 `data-before-restore-时间戳/`。恢复会保留当前实例的端口、域名和用户 ID，应用镜像取备份记录中的 digest（本地未发布镜像使用 image ID）。手动跨版本恢复失败时，使用刚生成的安全备份恢复当前版本。

备份包含 `data/` 全部文件（SQLite、WAL/SHM 若存在、`encryption.key`）、部署配置和镜像元数据，权限为 600。备份期间停止应用以保证一致性，并恢复原本运行中的服务；原来已停止则保持停止。备份和恢复前的数据不会自动删除，确认不再需要后可自行清理。HTTPS 证书卷不在应用备份中，迁移后 Caddy 可重新申请。

迁移到新机器：安装部署文件和 Docker、登录 GHCR，配置新的 `.env`（可复制 `.env.example` 并修改 UID/GID），复制受信任的备份，然后执行 `./gateway.sh restore /path/to/backup.tar.gz`。**数据库和加密密钥必须一起恢复。** 对本地未发布 image ID 的备份，还需先 `docker save` / `docker load` 对应镜像。

离线更新：先 `docker load -i image.tar`，再 `./gateway.sh update image:tag --no-pull`。命令会先验证本地镜像存在，随后执行相同备份与失败恢复流程。

## 镜像更新与认证

公开 GHCR 镜像支持匿名下载和检查更新，不需要服务器登录 Git 或 Docker。源码仓库的私有状态不影响公开镜像的下载。[GitHub Container Registry 文档](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

当前脚本通过 `./gateway.sh update` 执行升级，未自动安装后台监听器。若另行使用镜像更新工具，公开镜像不需要仓库凭据。直接重建容器的更新工具不会自动执行本脚本的停机备份与失败恢复；希望保留这些行为时，让调度任务调用 `gateway.sh update`。

只有镜像仍为私有时才需要一次 `docker login ghcr.io`，使用有 `read:packages` 权限的凭据。独立更新容器也需要获得镜像仓库认证；宿主机登录不代表凭据自动进入其他容器。Token 到期或被撤销后需要更新登录。

## 部署文件维护

部署文件由私有构建仓库在主分支 CI 成功后按固定文件清单同步到本仓库。同步仅包含 `deploy/` 下明确列出的脚本、配置、文档和脚本测试，不导出应用源码、数据库、密钥、私有 Git 历史或构建工作流。

应用镜像提供 `latest`、`main`、`sha-完整提交SHA` 和已发布的版本标签，支持 Linux amd64/arm64。服务器升级应用用 `gateway.sh update`；升级部署脚本可重新执行一键安装命令，自定义 `.env` 和配置会保留。
