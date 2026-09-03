"""交互式创建首个管理员，不提供默认账号或密码。"""

from __future__ import annotations

import argparse
from getpass import getpass
import sqlite3

from app.auth import hash_password
from app.database import initialize_database, transaction, utc_now


class AdminBootstrapError(RuntimeError):
    pass


def create_initial_admin(username: str, password: str, display_name: str = "管理员") -> dict:
    initialize_database()
    username = username.strip()
    display_name = display_name.strip()
    if len(username) < 2 or len(username) > 50:
        raise AdminBootstrapError("用户名长度需要在 2-50 个字符之间")
    if not display_name:
        raise AdminBootstrapError("显示名称不能为空")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise AdminBootstrapError(str(exc)) from exc
    with transaction() as connection:
        if connection.execute(
            "SELECT 1 FROM users WHERE role = 'admin' AND is_active = 1 LIMIT 1"
        ).fetchone():
            raise AdminBootstrapError("已经存在启用中的管理员，拒绝创建首个管理员")
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (username, display_name, password_hash, role, is_active, created_at)
                VALUES (?, ?, ?, 'admin', 1, ?)
                """,
                (username, display_name, password_hash, utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise AdminBootstrapError("用户名已存在") from exc
    return {"id": cursor.lastrowid, "username": username, "displayName": display_name}


def main() -> None:
    parser = argparse.ArgumentParser(description="创建首个 CAS Notes 管理员")
    parser.add_argument("--username", required=True, help="管理员用户名")
    parser.add_argument("--display-name", default="管理员", help="显示名称")
    args = parser.parse_args()
    password = getpass("请输入管理员密码（至少 8 个字符）：")
    confirmation = getpass("请再次输入管理员密码：")
    if password != confirmation:
        raise SystemExit("两次输入的密码不一致")
    try:
        created = create_initial_admin(args.username, password, args.display_name)
    except AdminBootstrapError as exc:
        raise SystemExit(f"创建失败：{exc}") from exc
    print(f"管理员已创建：{created['username']}（{created['displayName']}）")


if __name__ == "__main__":
    main()

