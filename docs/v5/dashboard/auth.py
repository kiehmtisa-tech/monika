"""
HTTP Basic Auth for Dragon Dashboard（凭证哈希存储版）。

安全设计（替换原硬编码方案）：
- 用户名与密码不再出现在源码中
- 凭证存 `.credentials.json`（600 权限，PBKDF2-SHA256 哈希 + 盐）
- 验证用 hmac.compare_digest 做时序安全比对
- 更换凭证只需重跑 rotate_dashboard_credentials.py，旧凭证立即失效

访问方式：Dashboard 只监听 127.0.0.1:8765，需通过 SSH 隧道访问。
"""
import base64
import hashlib
import hmac
import json
from pathlib import Path

from fastapi import Request, status
from fastapi.responses import Response

# 凭证文件放在 backend/ 根（与 app/ 平级），不进 python 包
CRED_FILE = Path(__file__).resolve().parent.parent / ".credentials.json"
DEFAULT_ITERATIONS = 200_000


def _load_credentials() -> dict:
    """加载凭证文件（含 username / salt / hash / iterations）。"""
    try:
        data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("凭证文件格式错误")
        return data
    except FileNotFoundError:
        # 未初始化时拒绝所有请求（fail closed），提示管理员先生成凭证
        return {}
    except Exception:
        return {}


_CRED_CACHE: dict = {}


def _credentials() -> dict:
    if not _CRED_CACHE:
        _CRED_CACHE.update(_load_credentials())
    return _CRED_CACHE


def _check_auth(username: str, password: str) -> bool:
    cred = _credentials()
    if not cred:
        return False
    if not hmac.compare_digest(username.encode("utf-8"),
                               str(cred.get("username", "")).encode("utf-8")):
        return False
    try:
        salt = bytes.fromhex(str(cred.get("salt", "")))
        expected = bytes.fromhex(str(cred.get("hash", "")))
        iterations = int(cred.get("iterations") or DEFAULT_ITERATIONS)
    except Exception:
        return False
    if not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations,
    )
    return hmac.compare_digest(actual, expected)


PUBLIC_PATHS = ["/health", "/docs", "/openapi.json"]


async def require_auth(request: Request, call_next):
    """Middleware: require basic auth for all routes except public paths."""
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return Response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content='{"detail":"Unauthorized"}',
            headers={"WWW-Authenticate": 'Basic realm="Dragon Dashboard"'},
            media_type="application/json",
        )

    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return Response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content='{"detail":"Invalid auth header"}',
            headers={"WWW-Authenticate": 'Basic realm="Dragon Dashboard"'},
            media_type="application/json",
        )

    if not _check_auth(username, password):
        return Response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content='{"detail":"Invalid credentials"}',
            headers={"WWW-Authenticate": 'Basic realm="Dragon Dashboard"'},
            media_type="application/json",
        )

    response = await call_next(request)
    return response
