# 我是谁

FastAPI + WebSocket 多人身份猜测游戏，使用 uv 管理依赖、Git worktree 发布、systemd 运行服务。

## 本地运行

```bash
bash scripts/setup.sh
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

打开 `http://127.0.0.1:8000`。脚本自动准备 uv、Python 和测试用 Node，并按 `uv.lock` 创建 `.venv`。Node 由 `.node-version` 固定，放在 `.tools/node/`；`--production` 不安装 Node。

首页为游戏大厅，房间列表每 5 秒刷新，可搜索房间名、房间号、房主或角色库，并筛选可加入或游戏中的房间。创建房间时填写昵称、房间名称，选择角色库和人数上限，再把自动生成的房间号分享给朋友；也可以直接从大厅加入已有房间。至少两位玩家且全员在线、准备后，由房主开始游戏。房主离开会自动移交，最后一人主动离开后房间解散；游戏中不接受新玩家，原有玩家仍可断线重连。

验证：

```bash
.venv/bin/python3 -m unittest discover -s tests -v
.tools/node/bin/node tests/client.test.js
```

## 本地触发部署

1. 提交并推送代码。自动部署默认关闭。
2. 复制 `.deploy.env.example` 为 `.deploy.env`，填写 SSH 别名、发布账户 `DEPLOY_AS_USER`、仓库地址和**新的发布根目录**（建议 `/srv/whoami`）。不要使用现有线上代码目录或其他用户的私有家目录。
3. 首次由管理员在服务器创建目录并配置服务权限；服务器需能读取 Git 仓库。
4. 执行 `bash scripts/deploy.sh --prepare-only`，先准备新版本，原服务继续运行。
5. 管理员完成一次性数据迁移和服务安装后，本地执行 `bash scripts/deploy.sh`。

首次设置见[部署说明](deploy/README.md)。之后部署、回退都从本地触发，脚本使用管理员限定的免密授权。真实配置不要提交。

## 启用自动部署

为 CI 准备独立部署账户与专用 SSH 密钥，按[部署说明](deploy/README.md)配置 GitHub Actions Secrets，最后将 Actions Variable `DEPLOY_ENABLED` 设为 `true`，手动运行一次 `main` 工作流确认连接。

之后推送到 `main`：**测试 → 发布精确提交 → 健康检查**。失败自动恢复旧版本；保留当前版本和最近 3 个历史版本。自定义角色库保存在共享目录，不随版本清理。

常用命令：

```bash
bash scripts/deploy.sh --dry-run   # 本地预览
bash scripts/deploy.sh             # 发布已推送的 HEAD
bash scripts/deploy.sh --rollback  # 回退到上一版本
```

**切换版本会重启服务并结束当前房间，玩家需要刷新页面。**
