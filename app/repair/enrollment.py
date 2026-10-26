"""一次性注册令牌:生成、校验、作废。

令牌形状 ``rct_<token_id>_<secret>``:
- token_id 进库做主键,按它查行,不用全表扫;
- secret 只存 sha256,明文只在生成命令那一刻出现一次;
- 绑定平台 + 有效期 + 用一次就废,泄漏了最多换一次连接,管理员还能随时撤销。

为什么不用 JWT:这里要的是"一次性、可撤销、能查使用记录",JWT 无状态反而要额外维护
黑名单;一张小表就能说清的事,不引签名体系。
"""
import hashlib
import secrets
from datetime import datetime, timedelta

from app.db import repository

TOKEN_PREFIX = "rct"
TOKEN_TTL_MINUTES = 30


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def issue(target_id: int) -> dict:
    """给平台签一张注册令牌。返回明文令牌与过期时间——明文只在这里出现。"""
    token_id = secrets.token_hex(6)
    secret = secrets.token_hex(16)
    expires_at = datetime.now() + timedelta(minutes=TOKEN_TTL_MINUTES)
    await repository.create_enrollment_token(token_id, target_id, _hash(secret), expires_at)
    return {"token": f"{TOKEN_PREFIX}_{token_id}_{secret}",
            "token_id": token_id,
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "ttl_minutes": TOKEN_TTL_MINUTES}


def _parse(token: str) -> tuple[str, str]:
    parts = (token or "").strip().split("_")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[1] or not parts[2]:
        raise ValueError("令牌格式不对,请重新生成接入命令")
    return parts[1], parts[2]


async def verify(token: str) -> tuple[int, str]:
    """校验令牌,返回 (target_id, token_id)。任何一条不满足都抛 ValueError,说明原因。"""
    token_id, secret = _parse(token)
    row = await repository.get_enrollment_token(token_id)
    if row is None:
        raise ValueError("令牌不存在,请重新生成接入命令")
    if row.token_hash != _hash(secret):
        raise ValueError("令牌校验失败,请重新生成接入命令")
    if row.revoked_at is not None:
        raise ValueError("令牌已作废,请重新生成接入命令")
    if row.used_at is not None:
        raise ValueError("令牌已使用过(一次性),请重新生成接入命令")
    if row.expires_at < datetime.now():
        raise ValueError("令牌已过期,请重新生成接入命令")
    return row.target_id, token_id


def connector_secret_hash(secret: str) -> str:
    return _hash(secret)


def verify_connector_secret(row, secret: str) -> bool:
    """连接器上线后每次请求都带自己的密钥;用固定时间比较,别把 hash 比较做成计时侧信道。"""
    import hmac
    return hmac.compare_digest(row.secret_hash or "", _hash(secret or ""))
