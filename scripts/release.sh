#!/usr/bin/env bash
# Run on the server, normally streamed by deploy.sh. No private defaults here.
set -Eeuo pipefail
# The service account must be able to read releases created by the SSH account.
umask 022
export GIT_TERMINAL_PROMPT=0
printf -v GIT_SSH_COMMAND 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -i %q' \
    "$HOME/.ssh/id_whoami_deploy"
export GIT_SSH_COMMAND

fail() { echo "$*" >&2; exit 1; }
valid_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ || "$1" =~ ^[0-9a-f]{64}$ ]]; }

(($# == 5)) || fail '发布参数数量错误。'
root=$1
source_repo=$2
sha=$3
service=$4
mode=$5
keep_previous=3
health_url=http://127.0.0.1:8000/healthz
[[ "$root" =~ ^/[a-zA-Z0-9_./-]+$ && ! "$root" =~ ^/+$ && ! "$root" =~ (^|/)\.{1,2}(/|$) ]] || fail '发布根目录无效。'
[[ "$service" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*\.service$ ]] || fail '服务名称无效。'
[[ "$mode" == deploy || "$mode" == prepare || "$mode" == rollback ]] || fail '发布模式无效。'
if [[ "$mode" != rollback ]]; then
    valid_sha "$sha" || fail '必须提供完整提交 SHA。'
    [[ -n "$source_repo" && "$source_repo" != -* && "$source_repo" != *$'\n'* ]] || fail '缺少或无效的仓库地址。'
fi
for tool in git flock curl readlink; do command -v "$tool" >/dev/null || fail "服务器缺少命令: $tool"; done
git_version=$(git --version)
if [[ "$git_version" =~ ([0-9]+)\.([0-9]+) ]]; then
    git_major=${BASH_REMATCH[1]}
    git_minor=${BASH_REMATCH[2]}
    ((git_major > 2 || (git_major == 2 && git_minor >= 17))) || fail '服务器需要 Git 2.17 或更新版本。请先升级 Git。'
else
    fail '无法确认服务器 Git 版本。'
fi
if [[ "$mode" != prepare ]]; then
    sudo -n -l /usr/bin/systemctl restart "$service" >/dev/null || fail '没有目标服务的免密重启权限。'
fi

# A separate, explicitly managed root prevents touching an existing live checkout.
[[ ! -L "$root" ]] || fail '发布根目录不能是符号链接。'
accessible_parent=$root
while [[ ! -e "$accessible_parent" && ! -L "$accessible_parent" ]]; do
    accessible_parent=${accessible_parent%/*}
    accessible_parent=${accessible_parent:-/}
done
[[ -d "$accessible_parent" && -x "$accessible_parent" && -w "$accessible_parent" ]] || \
    fail '部署账户无法访问或写入发布目录。首次请由管理员创建新的空目录并归属部署账户，所有父目录也须允许进入。建议使用 /srv/whoami，避免使用其他用户的私有家目录；修改后请同步 DEPLOY_DIR。详见 deploy/README.md。'
if [[ "$mode" == rollback && ! -f "$root/.whoami-releases" ]]; then fail '发布根目录尚未初始化，无法回退。'; fi
if [[ -e "$root" && ! -f "$root/.whoami-releases" ]]; then
    [[ -d "$root" && -z "$(find "$root" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail '发布根目录非空且不属于发布系统。请使用新的空目录。'
fi
if [[ -f "$root/.whoami-releases" ]]; then
    [[ ! -L "$root/.whoami-releases" && "$(cat "$root/.whoami-releases")" == whoami-releases-v1 ]] || fail '发布根目录标记无效。'
fi
mkdir -p "$root"
root=$(cd "$root" && pwd -P)
exec 9>"$root/.deploy.lock"
flock -n 9 || fail '已有发布任务正在运行。'
printf 'whoami-releases-v1\n' > "$root/.whoami-releases"
repo="$root/repo.git"
releases="$root/releases"
state="$root/state"
mkdir -p "$releases" "$state" "$root/shared/custom"

link_sha() {
    local name=$1 target
    if [[ ! -e "$root/$name" && ! -L "$root/$name" ]]; then return 0; fi
    [[ -L "$root/$name" ]] || return 1
    target=$(readlink "$root/$name")
    [[ "$target" == releases/* ]] || return 1
    target=${target#releases/}
    valid_sha "$target" && [[ -d "$releases/$target" ]] || return 1
    printf '%s' "$target"
}
old_sha=$(link_sha current) || fail 'current 不是本系统管理的版本链接。'
previous_sha=$(link_sha previous) || fail 'previous 不是本系统管理的版本链接。'
for existing_sha in "$old_sha" "$previous_sha"; do
    if [[ -n "$existing_sha" ]]; then
        [[ -f "$state/$existing_sha.managed" && -f "$state/$existing_sha.ready" ]] || fail '现有版本缺少管理标记。'
    fi
done
switched=false
committed=false
created=false

atomic_link() {
    local name=$1 target=$2
    ln -s "releases/$target" "$root/.$name.$$" || return 1
    mv -Tf -- "$root/.$name.$$" "$root/$name"
}

health_check() {
    local expected=$1 body attempt
    for ((attempt=0; attempt<15; attempt++)); do
        if body=$(curl --fail --silent --connect-timeout 1 --max-time 2 "$health_url"); then
            if printf '%s' "$body" | "$releases/$expected/.venv/bin/python3" -c \
                'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" and d.get("release")==sys.argv[1] else 1)' "$expected" 2>/dev/null; then
                return 0
            fi
        fi
        sleep 1
    done
    return 1
}

remove_release() {
    local candidate=$1 active previous
    valid_sha "$candidate" && [[ -f "$state/$candidate.managed" ]] || return 1
    active=$(link_sha current) || return 1
    previous=$(link_sha previous) || return 1
    [[ "$candidate" != "$active" && "$candidate" != "$previous" ]] || return 1
    git -C "$repo" worktree remove --force -- "$releases/$candidate" || return 1
    rm -f -- "$state/$candidate.managed" "$state/$candidate.ready"
}

on_exit() {
    local status=$? cleanup_candidate=false
    trap - EXIT HUP INT TERM
    if ((status != 0)); then
        if ! $switched; then cleanup_candidate=true; fi
        if $switched && ! $committed; then
            if [[ -n "$old_sha" ]]; then
                echo '新版本启动失败，正在恢复原版本。' >&2
                if atomic_link current "$old_sha" && sudo -n /usr/bin/systemctl restart "$service" && health_check "$old_sha"; then
                    if [[ -n "$previous_sha" ]]; then
                        atomic_link previous "$previous_sha"
                    elif [[ -L "$root/previous" ]]; then
                        rm -f -- "$root/previous"
                    fi
                    cleanup_candidate=true
                    echo '原版本已恢复，健康检查通过。' >&2
                else
                    echo '自动恢复未能确认成功，请立即检查服务日志。' >&2
                fi
            else
                echo '首次发布失败，没有旧版本可恢复；保留当前目录供检查。' >&2
            fi
        fi
        if $created && $cleanup_candidate; then remove_release "$sha" >/dev/null 2>&1 || true; fi
    fi
    rm -f -- "$root/.current.$$" "$root/.previous.$$"
    exit "$status"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$mode" == rollback ]]; then
    [[ -n "$previous_sha" && -f "$state/$previous_sha.ready" ]] || fail '没有已准备好的上一版本。'
    sha=$previous_sha
else
    if [[ ! -d "$repo" ]]; then
        git init --bare --quiet "$repo"
        git -C "$repo" remote add origin "$source_repo"
    fi
    [[ "$(git -C "$repo" rev-parse --is-bare-repository)" == true ]] || fail '版本仓库不是 bare 仓库。'
    [[ "$(git -C "$repo" remote get-url origin)" == "$source_repo" ]] || fail '发布根目录已绑定其他仓库。'
    if [[ ! -f "$state/$sha.ready" ]]; then
        git -C "$repo" fetch --quiet --no-tags --prune origin '+refs/heads/*:refs/heads/*'
        git -C "$repo" cat-file -e "$sha^{commit}" || fail '目标提交尚未推送到服务器可访问的仓库。'
    fi
fi

release="$releases/$sha"
if [[ ! -f "$state/$sha.ready" ]]; then
    [[ "$mode" != rollback ]] || fail '回退版本尚未准备完成。'
    [[ ! -e "$release" && ! -L "$release" ]] || fail '发现未完成的版本目录；请先检查并移除对应 worktree。'
    git -C "$repo" worktree add --detach "$release" "$sha" >/dev/null
    touch "$state/$sha.managed"
    created=true
    [[ -f "$release/pyproject.toml" && -f "$release/uv.lock" && -f "$release/scripts/setup.sh" ]] || fail '目标提交缺少 uv 项目文件或初始化脚本。'
    # Keep the interpreter outside the deployment user's private home directory.
    (cd "$release" && UV_PYTHON_INSTALL_DIR="$root/shared/python" UV_MANAGED_PYTHON=1 \
        bash scripts/setup.sh --production)
    (cd "$release" && WHOAMI_CUSTOM_DIR="$root/shared/custom" WHOAMI_RELEASE="$sha" \
        .venv/bin/python3 -c 'from main import app')
    printf 'WHOAMI_RELEASE=%s\n' "$sha" > "$release/.release.env"
    touch "$state/$sha.ready"
fi
[[ -x "$release/.venv/bin/python3" && -f "$state/$sha.managed" ]] || fail '版本环境不完整。'
[[ "$(cat "$release/.release.env")" == "WHOAMI_RELEASE=$sha" ]] || fail '版本标识文件不匹配。'
git -C "$release" diff --quiet HEAD -- || fail '版本目录的受跟踪文件被修改，拒绝发布。'
if [[ "$mode" == prepare ]]; then
    echo "版本 $sha 已准备完成；未切换 current 或重启服务。"
    exit 0
fi

if [[ "$sha" == "$old_sha" ]]; then
    health_check "$sha" || fail '当前版本健康检查失败。'
    echo '目标版本已在运行，无需重启。'
    exit 0
fi

switched=true
atomic_link current "$sha"
sudo -n /usr/bin/systemctl restart "$service"
health_check "$sha" || fail '新版本健康检查失败。'
if [[ -n "$old_sha" ]]; then atomic_link previous "$old_sha"; fi

# Record only successful activations, newest first; retries do not consume slots.
retained=("$sha")
if [[ -n "$old_sha" ]]; then retained+=("$old_sha"); fi
if [[ -f "$state/history" ]]; then
    while IFS= read -r candidate; do
        valid_sha "$candidate" || continue
        [[ -f "$state/$candidate.ready" ]] || continue
        duplicate=false
        for kept in "${retained[@]}"; do [[ "$candidate" != "$kept" ]] || duplicate=true; done
        if ! $duplicate && ((${#retained[@]} < keep_previous + 1)); then retained+=("$candidate"); fi
    done < "$state/history"
fi
printf '%s\n' "${retained[@]}" > "$state/.history.$$"
mv -f -- "$state/.history.$$" "$state/history"
committed=true

# Remove only worktrees created by this script. Data, current and previous are protected.
for marker in "$state"/*.managed; do
    [[ -f "$marker" ]] || continue
    candidate=${marker##*/}
    candidate=${candidate%.managed}
    keep=false
    for kept in "${retained[@]}"; do [[ "$candidate" != "$kept" ]] || keep=true; done
    if ! $keep; then
        if ! remove_release "$candidate"; then
            echo '警告：有旧 worktree 无法清理，请检查目录权限。' >&2
        fi
    fi
done
echo "版本 $sha 发布成功，保留当前版本及最近 $keep_previous 个历史版本。"
