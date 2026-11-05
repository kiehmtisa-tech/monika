# -*- coding: utf-8 -*-
"""Dragon Dashboard 凭证轮换：生成新密码、PBKDF2 哈希存储、明文只落 root-only 文件。

安全约束（按审核要求）：
- 不复用服务器 root 密码
- 不在终端/报告/日志输出任何密码
- 凭证文件 600 权限，明文提示文件单独存放且不入 git
- 旧凭证使失效（替换即失效，Basic Auth 无服务端会话）

用法：python3 rotate_dashboard_credentials.py
"""
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path

BACKEND = Path("/root/dragon-dashboard/dragon/backend")
CRED_FILE = BACKEND / ".credentials.json"          # 只存哈希，不入 git
LOGIN_HINT = Path("/root/DASHBOARD_LOGIN.txt")      # 明文提示，600 权限
GITIGNORE = BACKEND / ".gitignore"

ITERATIONS = 200_000                               # PBKDF2 迭代次数
USERNAME = "admin"                                 # 不再用 root


def main() -> int:
    password = secrets.token_urlsafe(18)            # 随机新密码，不打印
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)

    record = {
        "username": USERNAME,
        "algo": "pbkdf2_sha256",
        "iterations": ITERATIONS,
        "salt": salt.hex(),
        "hash": digest.hex(),
        "updated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
    }
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CRED_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, CRED_FILE)
    os.chmod(CRED_FILE, stat.S_IRUSR | stat.S_IWUSR)   # 600

    # 明文只落 root-only 文件，供人工登录时查看
    LOGIN_HINT.write_text(
        "Dragon Dashboard 登录（请勿提交到 git / 勿贴到聊天）\n"
        "地址：SSH 隧道到 127.0.0.1:8765\n"
        "用户名：%s\n密码：%s\n" % (USERNAME, password),
        encoding="utf-8",
    )
    os.chmod(LOGIN_HINT, stat.S_IRUSR | stat.S_IWUSR)  # 600

    # 确保凭证文件不会被 git 收录（若 dashboard 是 git 仓库）
    line = ".credentials.json"
    try:
        existing = GITIGNORE.read_text(encoding="utf-8") if GITIGNORE.exists() else ""
        if line not in existing:
            GITIGNORE.write_text(existing + ("\n" if existing else "") + line + "\n", encoding="utf-8")
    except Exception:
        pass

    # 不输出密码本身
    print("凭证已轮换。")
    print("  用户名: %s" % USERNAME)
    print("  密码长度: %d 字符（已随机生成）" % len(password))
    print("  哈希凭证: %s (600)" % CRED_FILE)
    print("  登录提示: %s (600)  ← 只有 root 能读" % LOGIN_HINT)
    print("  算法: PBKDF2-SHA256, %d 次迭代" % ITERATIONS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
