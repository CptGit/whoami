#!/usr/bin/env python3
"""Render private server configuration into the Git-ignored .deploy directory."""
import argparse
import os
from pathlib import Path
import re


def render(root, app_user, deploy_user, service, output, ssh_login_user=''):
    if (not re.fullmatch(r'/[A-Za-z0-9_./-]+', root)
            or not root.strip('/') or any(part in ('.', '..') for part in root.split('/'))):
        raise ValueError('DEPLOY_DIR 必须是有效的绝对路径。')
    if not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', user) for user in (app_user, deploy_user)):
        raise ValueError('APP_USER 和 DEPLOY_USER 必须是有效的系统用户。')
    if ssh_login_user and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', ssh_login_user):
        raise ValueError('SSH_LOGIN_USER 必须是有效的系统用户。')
    if deploy_user == 'root' or app_user == 'root':
        raise ValueError('部署账户和服务账户必须使用非 root 账户。')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service', service):
        raise ValueError('SERVICE_NAME 无效。')
    project = Path(__file__).resolve().parents[1]
    local_publish_rule = ''
    if ssh_login_user and ssh_login_user != deploy_user:
        local_publish_rule = (
            f'Defaults>{deploy_user} secure_path="/usr/local/bin:/usr/bin:/bin"\n'
            f'{ssh_login_user} ALL=({deploy_user}) NOPASSWD: /bin/bash -s -- *\n'
        )
    replacements = {
        '@DEPLOY_DIR@': root.rstrip('/'), '@APP_USER@': app_user,
        '@DEPLOY_USER@': deploy_user, '@SERVICE_NAME@': service,
        '@LOCAL_PUBLISH_RULE@': local_publish_rule,
    }
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    for template, name in (
        ('whoami.service.example', service), ('whoami-deploy.sudoers.example', 'whoami-deploy'),
    ):
        content = (project / 'deploy' / template).read_text()
        for placeholder, value in replacements.items():
            content = content.replace(placeholder, value)
        path = output / name
        path.write_text(content)
        path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    os.umask(0o077)
    try:
        render(
            os.environ.get('DEPLOY_DIR', ''), os.environ.get('APP_USER', ''),
            os.environ.get('DEPLOY_USER', ''), os.environ.get('SERVICE_NAME') or 'whoami.service',
            Path(__file__).resolve().parents[1] / '.deploy',
            os.environ.get('SSH_LOGIN_USER', ''),
        )
    except ValueError as error:
        parser.error(str(error))
    print('配置已生成到当前版本的 .deploy/；尚未安装或重启服务。')


if __name__ == '__main__':
    main()
