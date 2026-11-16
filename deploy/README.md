# 部署说明

日常操作都在本地发起，通过 SSH 执行服务器发布。**只有首次安装服务和授权，需要管理员在服务器上手动完成。** 部署脚本不接收、保存或传输 sudo 密码。

## 1. 本地准备

提交并推送代码，复制 `.deploy.env.example` 为 `.deploy.env`，填写 SSH 别名、仓库地址和新的发布根目录。真实配置不要提交。

确认本地能通过 SSH 连接部署账户，部署账户能读取 Git 仓库。私有仓库可给服务器配置单仓库只读 deploy key。

## 2. 首次创建目录：管理员在服务器执行

使用已有的管理员会话。以下命令不由本地部署脚本代为执行。先私下设置 `DEPLOY_DIR`、`DEPLOY_USER`、`APP_USER`、`SERVICE_NAME`，分别对应新发布根目录、部署账户、服务运行账户、服务名。默认服务名为 `whoami.service`。

`DEPLOY_USER` 使用实际的 SSH 登录账户，负责获取代码、准备环境和发布；`APP_USER` 建议使用独立的 `whoami` 服务账户，负责运行游戏，无需 SSH 登录或 sudo 权限。两者不必相同。

建议使用 `/srv/whoami` 作为发布根目录。所有父目录都须允许部署账户进入；其他用户权限为 `700` 的家目录不适合存放发布目录。目录由部署账户拥有，服务账户也需能读取其中的版本文件。

```bash
id "$DEPLOY_USER"
id "$APP_USER"
install -d -m 755 -o "$DEPLOY_USER" -g "$(id -gn "$DEPLOY_USER")" "$DEPLOY_DIR"
```

目录必须是新的空目录，不能使用原线上代码目录。本地 `.deploy.env`、服务配置和 CI Secret 中的 `DEPLOY_DIR` 应保持一致。服务器需要 Git ≥ 2.17、Bash ≥ 4.2、flock 和 curl。

## 3. 准备新版本：本地执行

```bash
bash scripts/deploy.sh --prepare-only
```

服务器自己从 Git 获取代码并准备 Python 环境，不改变正在运行的服务。Python 解释器放在 `shared/python/`，每个版本保留独立 `.venv`，服务账户无需访问部署账户的家目录。记下返回的完整提交 SHA。

## 4. 首次安装服务：管理员在服务器执行

设置 `RELEASE_SHA` 为上一步返回的 SHA。使用部署账户生成配置，避免在版本目录内留下管理员拥有的文件：

```bash
SERVICE_NAME=${SERVICE_NAME:-whoami.service}
cd "$DEPLOY_DIR/releases/$RELEASE_SHA"
runuser -u "$DEPLOY_USER" -- env \
  DEPLOY_DIR="$DEPLOY_DIR" APP_USER="$APP_USER" \
  DEPLOY_USER="$DEPLOY_USER" SERVICE_NAME="$SERVICE_NAME" \
  .venv/bin/python3 scripts/render-deploy-config.py
config_dir="$PWD/.deploy"
visudo -cf "$config_dir/whoami-deploy"
```

选择暂时没人游戏、可以重启服务的时间。设置 `OLD_APP_DIR` 为原应用目录，备份旧服务配置，停止服务后迁移自定义库：

```bash
cp -n "/etc/systemd/system/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME.before-releases"
systemctl stop "$SERVICE_NAME"
if [[ -d "$OLD_APP_DIR/data/custom" ]]; then
    cp -a "$OLD_APP_DIR/data/custom/." "$DEPLOY_DIR/shared/custom/"
fi
chown -R "$APP_USER:$(id -gn "$APP_USER")" "$DEPLOY_DIR/shared/custom"
install -o root -g root -m 644 "$config_dir/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
install -o root -g root -m 440 "$config_dir/whoami-deploy" /etc/sudoers.d/whoami-deploy
visudo -c
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
```

sudoers 只允许部署账户免密执行 `/usr/bin/systemctl restart` 指定服务，不授权安装、停止服务或任意管理员命令。原代码、数据和旧服务配置备份暂时保留。

## 5. 正式发布：本地执行

```bash
bash scripts/deploy.sh
```

服务会切换版本并检查运行的提交 SHA。后续发布失败时自动恢复旧版本；保留当前版本和最近 3 个历史版本。首次发布没有旧发布版本可自动恢复，失败时由管理员恢复 `.before-releases` 服务配置并重启原服务。

日常命令：

```bash
bash scripts/deploy.sh --dry-run   # 只预览
bash scripts/deploy.sh             # 发布已推送的 HEAD
bash scripts/deploy.sh --rollback  # 回退到上一版本
```

不需要打开服务器终端或输入 sudo 密码；缺少免密重启权限时，脚本在修改发布目录前退出。

## GitHub 自动部署

在仓库或 `production` 环境的 Actions Secrets 中配置：

| Secret | 内容 |
| --- | --- |
| `DEPLOY_HOST` | 服务器地址 |
| `DEPLOY_USER` | SSH 登录账户 |
| `DEPLOY_PORT` | SSH 端口 |
| `DEPLOY_DIR` | 发布根目录 |
| `DEPLOY_REPO` | 服务器读取的 Git 仓库 URL |
| `DEPLOY_SSH_PRIVATE_KEY` | CI 专用 SSH 私钥 |
| `DEPLOY_KNOWN_HOSTS` | 已核验的服务器 known_hosts 条目 |
| `DEPLOY_SERVICE_NAME` | 可选，默认 `whoami.service` |

不配置 sudo 密码 Secret。将 Actions Variable `DEPLOY_ENABLED` 设为 `true`，手动运行一次 main 工作流验证。以后推送 main：测试通过后发布精确提交；PR 只测试。

共享自定义库应独立备份，不随版本清理。切换版本会结束内存中的房间；需要保持回退版本时，也应让 main 保持期望状态，避免后续推送再次更新。
