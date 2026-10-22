"""账号业务：校验注册输入、生成盐化密码摘要、验证登录；SQL 归 users 仓库。"""
import hashlib
import secrets
import uuid

from station.persistence import database
from station.persistence.errors import DuplicateKey
from station.persistence.repositories import users


def _hash(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def create_user(name: str, pw: str) -> dict:
    if not (name or "").strip() or not pw:
        raise ValueError("用户名和密码不能为空")
    salt = secrets.token_hex(8)
    try:
        return users.insert_user(uuid.uuid4().hex[:12], name.strip(), _hash(pw, salt), salt, database.now())
    except DuplicateKey as error:
        raise ValueError(f"用户名已存在：{name}") from error


def check_login(name: str, pw: str) -> dict | None:
    row = users.credentials_by_name(name)
    if row is None or _hash(pw or "", row["salt"]) != row["pw_hash"]:
        return None
    return {"id": row["id"], "name": row["name"]}
