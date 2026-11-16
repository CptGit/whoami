#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
用法: bash scripts/deploy.sh [--dry-run] [--prepare-only | --rollback] [--ref 完整SHA] [SSH主机别名]

从环境变量或未跟踪配置读取 DEPLOY_HOST、DEPLOY_AS_USER、DEPLOY_DIR、DEPLOY_REPO。
DEPLOY_AS_USER 必填：指定发布执行账户，与 SSH 登录账户不同时免密切换。
DEPLOY_DIR 必须是独立发布根目录，不能是现有线上代码目录。
默认保留当前版本与最近 3 个历史版本，SERVICE_NAME 默认 whoami.service。

--dry-run       本地预览，不连接或修改服务器
--prepare-only  准备版本和环境，不切换 current 或重启服务
--rollback      切回 previous，健康检查失败则恢复当前版本
--ref           部署精确提交；默认本地 HEAD，必须先推送到远端
USAGE
}

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_ROOT"
declare -A overrides=()
for name in DEPLOY_HOST DEPLOY_AS_USER DEPLOY_DIR DEPLOY_REPO SERVICE_NAME; do
    if [[ -v "$name" ]]; then overrides[$name]=${!name}; fi
done
config_file=${DEPLOY_CONFIG_FILE:-$PROJECT_ROOT/.deploy.env}
if [[ -f "$config_file" ]]; then source "$config_file"; fi
for name in "${!overrides[@]}"; do printf -v "$name" '%s' "${overrides[$name]}"; done

host=${DEPLOY_HOST:-}
deploy_as=${DEPLOY_AS_USER:-}
root=${DEPLOY_DIR:-}
source_repo=${DEPLOY_REPO:-}
service=${SERVICE_NAME:-whoami.service}
mode=deploy
dry_run=false
ref=HEAD
host_set=false
mode_set=false
while (($#)); do
    case "$1" in
        --dry-run) dry_run=true ;;
        --prepare-only|--rollback)
            if $mode_set; then usage >&2; exit 2; fi
            mode_set=true
            if [[ "$1" == --rollback ]]; then mode=rollback; else mode=prepare; fi
            ;;
        --ref)
            (($# >= 2)) || { usage >&2; exit 2; }
            ref=$2
            [[ "$ref" =~ ^[0-9a-f]{40}$ || "$ref" =~ ^[0-9a-f]{64}$ ]] || { echo '--ref 必须是完整提交 SHA。' >&2; exit 2; }
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; exit 2 ;;
        *)
            if $host_set; then usage >&2; exit 2; fi
            host=$1
            host_set=true
            ;;
    esac
    shift
done
if [[ "$mode" == rollback && "$ref" != HEAD ]]; then
    echo '--rollback 与 --ref 不能同时使用。' >&2
    exit 2
fi

if [[ -z "$host" || -z "$deploy_as" || -z "$root" || ( "$mode" != rollback && -z "$source_repo" ) ]]; then
    echo '缺少 DEPLOY_HOST、DEPLOY_AS_USER、DEPLOY_DIR 或 DEPLOY_REPO，请配置私有环境变量或配置文件。' >&2
    exit 2
fi
if [[ ! "$host" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] ||
   [[ ! "$deploy_as" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ || "$deploy_as" == root ]] ||
   [[ ! "$root" =~ ^/[a-zA-Z0-9_./-]+$ || "$root" =~ ^/+$ || "$root" =~ (^|/)\.{1,2}(/|$) ]] ||
   [[ ! "$service" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*\.service$ ]] ||
   [[ "$source_repo" == -* || "$source_repo" == *$'\n'* ]]; then
    echo '部署配置格式无效。' >&2
    exit 2
fi

sha=''
if [[ "$mode" != rollback ]]; then sha=$(git rev-parse --verify "$ref^{commit}"); fi
if $dry_run; then
    echo "发布模式: $mode；提交: ${sha:-previous}。"
    echo "发布执行账户: $deploy_as。"
    echo '仅部署已提交并推送的代码；本地未提交改动不会传输。未连接服务器。'
    exit 0
fi
# Only the deployment program is streamed; application code is fetched by Git on the server.
remote_bootstrap='deploy_as=$1; shift
if [[ "$deploy_as" != "$(id -un)" ]]; then
    cd /
    exec sudo -n -H -u "$deploy_as" -- /bin/bash -s -- "$@"
fi
exec /bin/bash -s -- "$@"'
printf -v remote_args '%q ' "$deploy_as" "$root" "$source_repo" "$sha" "$service" "$mode"
printf -v remote_command 'bash -c %q bash %s' "$remote_bootstrap" "$remote_args"
ssh -T -o BatchMode=yes -o ConnectTimeout=10 "$host" \
    "$remote_command" < scripts/release.sh
