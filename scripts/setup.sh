#!/usr/bin/env bash
set -euo pipefail

production=false
while (($#)); do
    case "$1" in
        --production) production=true ;;
        -h|--help)
            echo '用法: bash scripts/setup.sh [--production]'
            echo '准备 uv、Python 和本地测试用 Node；--production 只准备 Python 运行环境。'
            exit 0
            ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
    shift
done

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_ROOT"

# Bootstrap without requiring Python or a TOML library. Keep the exact uv pin in
# pyproject.toml; uv itself validates this constraint when syncing the project.
uv_version=$(sed -nE 's/^required-version[[:space:]]*=[[:space:]]*"==([0-9]+\.[0-9]+\.[0-9]+)"[[:space:]]*$/\1/p' pyproject.toml)
if [[ ! "$uv_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'pyproject.toml 必须通过 required-version 精确指定 uv 版本。' >&2
    exit 1
fi

install_dir=${UV_INSTALL_DIR:-$HOME/.local/bin}
uv_bin=${UV_BIN:-}
if [[ -z "$uv_bin" ]]; then
    if command -v uv >/dev/null 2>&1; then
        uv_bin=$(command -v uv)
    elif [[ -x "$install_dir/uv" ]]; then
        uv_bin="$install_dir/uv"
    fi
fi

installed_version=''
if [[ -n "$uv_bin" ]]; then
    installed_version=$("$uv_bin" --version | awk '{print $2}')
fi
if [[ "$installed_version" != "$uv_version" ]]; then
    if [[ -n "${UV_BIN:-}" ]]; then
        echo "UV_BIN 指定的 uv 版本为 $installed_version，需要 $uv_version。" >&2
        exit 1
    fi
    echo "使用官方独立安装器安装 uv $uv_version 到 $install_dir…"
    installer_script=$(mktemp "${TMPDIR:-/tmp}/whoami-uv.XXXXXX")
    trap 'rm -f -- "$installer_script"' EXIT
    installer_url="https://astral.sh/uv/$uv_version/install.sh"
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --show-error --location --retry 2 \
            --connect-timeout 10 --max-time 60 "$installer_url" --output "$installer_script"
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet --timeout=60 --tries=3 --output-document="$installer_script" "$installer_url"
    else
        echo '自动安装 uv 需要 curl 或 wget，请先安装其中之一。' >&2
        exit 1
    fi
    # Do not edit shell profiles; the rest of this script uses the absolute path.
    UV_INSTALL_DIR="$install_dir" UV_NO_MODIFY_PATH=1 sh "$installer_script"
    uv_bin="$install_dir/uv"
fi

python_version=$(tr -d '\r\n' < .python-version)
if [[ -x .venv/bin/python3 ]]; then
    actual_version=$(.venv/bin/python3 -c 'import platform; print(platform.python_version())')
    if [[ "$actual_version" != "$python_version" ]]; then
        echo "现有 .venv 使用 Python $actual_version，需要 $python_version。" >&2
        echo '请先停止使用该环境的服务，将旧 .venv 移走，再重新运行此脚本。' >&2
        exit 1
    fi
fi

sync_options=(sync --locked --python "$python_version")
if $production; then
    sync_options+=(--no-dev)
fi
# Always use this project's environment, even if the caller has activated Conda
# or set UV_PROJECT_ENVIRONMENT to another project.
UV_PROJECT_ENVIRONMENT="$PROJECT_ROOT/.venv" "$uv_bin" "${sync_options[@]}"
if ! $production; then
    .venv/bin/python3 scripts/setup-node.py
fi
echo '环境已按 uv.lock 同步。'
