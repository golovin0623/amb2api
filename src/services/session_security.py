"""
会话安全工具：账户密码的本地加密存储 + Stytch sessionJWT 过期时间解析。

设计目标：
- 不引入额外依赖（环境里 ``cryptography`` 的 CFFI 后端可能缺失），仅用标准库
  实现一个 encrypt-then-MAC 的认证加密方案，用于把账户密码加密落盘，
  以支撑"会话失效时用凭据自动重新登录"的兜底续期。
- 解析 Stytch 的 ``sessionJWT``（固定 5 分钟寿命）里的 ``exp`` 声明，
  以便按真实过期时间提前续期，而不是依赖一个写死的本地 7 天过期。

加密格式：``v1:<base64(nonce(16) || ciphertext || tag(32))>``
- keystream = HMAC-SHA256(key, nonce || counter_be32) 拼接（CTR 模式）
- tag = HMAC-SHA256(key, nonce || ciphertext)（encrypt-then-MAC，常数时间校验）

注意：这只是"静态混淆/认证加密"，真正的机密性仍依赖存储后端本身的访问控制。
绝不可把明文密码或本文件产出的 token 写入日志。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Optional

_VERSION = "v1"
_NONCE_LEN = 16
_TAG_LEN = 32


def _derive_keys(key: bytes) -> tuple:
    """从主密钥派生独立的加密子密钥与 MAC 子密钥（避免密钥复用）。"""
    enc_key = hashlib.sha256(key + b"enc").digest()
    mac_key = hashlib.sha256(key + b"mac").digest()
    return enc_key, mac_key


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """用 HMAC-SHA256 在 CTR 模式下生成 ``length`` 字节的密钥流。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            key, nonce + counter.to_bytes(4, "big"), hashlib.sha256
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_secret(key: bytes, plaintext: str) -> str:
    """加密一个字符串（如账户密码），返回可安全落盘的 token。"""
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be str")
    enc_key, mac_key = _derive_keys(key)
    nonce = os.urandom(_NONCE_LEN)
    data = plaintext.encode("utf-8")
    ks = _keystream(enc_key, nonce, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, ks))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    blob = nonce + ciphertext + tag
    return f"{_VERSION}:{base64.b64encode(blob).decode('ascii')}"


def decrypt_secret(key: bytes, token: str) -> Optional[str]:
    """解密 :func:`encrypt_secret` 产生的 token；失败（被篡改/格式错）返回 None。"""
    if not token or not isinstance(token, str):
        return None
    try:
        version, _, payload = token.partition(":")
        if version != _VERSION or not payload:
            return None
        blob = base64.b64decode(payload)
        if len(blob) < _NONCE_LEN + _TAG_LEN:
            return None
        nonce = blob[:_NONCE_LEN]
        tag = blob[-_TAG_LEN:]
        ciphertext = blob[_NONCE_LEN:-_TAG_LEN]
        enc_key, mac_key = _derive_keys(key)
        expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            return None
        ks = _keystream(enc_key, nonce, len(ciphertext))
        data = bytes(a ^ b for a, b in zip(ciphertext, ks))
        return data.decode("utf-8")
    except Exception:
        return None


def _b64url_decode(segment: str) -> bytes:
    """解码 JWT 的 base64url 段（自动补齐 padding）。"""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_exp(token: Optional[str]) -> Optional[int]:
    """读取 JWT payload 中的 ``exp``（epoch 秒）。仅解析、不校验签名。

    解析失败或不存在 ``exp`` 时返回 None。
    """
    if not token or not isinstance(token, str):
        return None
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return int(exp)
    except Exception:
        return None
    return None
