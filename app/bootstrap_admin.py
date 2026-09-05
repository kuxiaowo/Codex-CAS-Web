"""Assign a CAS role to an existing NetHub Accounts member by central subject."""

from __future__ import annotations

import argparse

from app.database import initialize_database, transaction


class AdminBootstrapError(RuntimeError):
    pass


def grant_admin(sub: str) -> dict:
    initialize_database()
    sub = sub.strip()
    if not sub:
        raise AdminBootstrapError("中央 sub 不能为空")
    with transaction() as connection:
        row = connection.execute("SELECT id, username FROM users WHERE auth_sub = ?", (sub,)).fetchone()
        if not row:
            raise AdminBootstrapError("该中央账号尚未登录本站，无法授予本站管理员权限")
        connection.execute("UPDATE users SET role = 'admin', is_active = 1 WHERE id = ?", (row["id"],))
    return {"id": row["id"], "username": row["username"], "sub": sub}


def main() -> None:
    parser = argparse.ArgumentParser(description="授予已登录 CAS 的中央账号管理员角色")
    parser.add_argument("--sub", required=True, help="NetHub Accounts 用户 sub")
    args = parser.parse_args()
    try:
        result = grant_admin(args.sub)
    except AdminBootstrapError as exc:
        raise SystemExit(f"授权失败：{exc}") from exc
    print(f"已授予 CAS 管理员权限：{result['username']} ({result['sub']})")


if __name__ == "__main__":
    main()
