"""JWT authentication and demo user helpers."""
import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional
import jwt
from config.settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SecurityAuth")
SECRET_KEY = settings.jwt_secret_key
ALGORITHM = settings.jwt_algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if not salt:
        salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return f"{salt.hex()}.{key.hex()}"

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        salt_hex, _ = hashed_password.split(".", 1)
        salt = bytes.fromhex(salt_hex)
        rehashed = hash_password(plain_password, salt)
        return secrets.compare_digest(rehashed, hashed_password)
    except Exception as exc:
        logger.error("Password verification error: %s", exc)
        return False

_DEFAULT_DEMO_PASSWORDS = {"admin": "admin123", "cfo": "cfo123", "sales": "sales123"}


def _demo_password(username: str) -> str:
    """Demo passwords can be overridden with DEMO_<USER>_PASSWORD. Defaults are refused in secure mode."""
    value = os.environ.get(f"DEMO_{username.upper()}_PASSWORD")
    if value:
        return value
    if settings.secure_mode:
        raise RuntimeError(f"Refusing to start: set DEMO_{username.upper()}_PASSWORD (default demo passwords are not allowed in secure mode).")
    return _DEFAULT_DEMO_PASSWORDS[username]


MOCK_USERS_DB = {
    "admin": {"username": "admin", "hashed_password": hash_password(_demo_password("admin")), "role": "RevOps_Manager", "email": "admin@guardian.com"},
    "cfo": {"username": "cfo", "hashed_password": hash_password(_demo_password("cfo")), "role": "CFO", "email": "cfo@guardian.com"},
    "sales": {"username": "sales", "hashed_password": hash_password(_demo_password("sales")), "role": "Sales_Rep", "email": "sales@guardian.com"},
}

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token has expired.")
        return None
    except jwt.PyJWTError as exc:
        logger.warning("Invalid JWT token: %s", exc)
        return None

def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    user = MOCK_USERS_DB.get(username)
    if user and verify_password(password, user["hashed_password"]):
        return user
    return None

class RoleAccessRequired:
    def __init__(self, allowed_roles: List[str]):
        self.allowed_roles = allowed_roles
    def __call__(self, token: str) -> Dict[str, Any]:
        payload = verify_access_token(token)
        if not payload:
            raise ValueError("Could not validate credentials.")
        user_role = payload.get("role")
        if user_role not in self.allowed_roles:
            raise PermissionError(f"Role '{user_role}' is not authorized. Requires one of: {self.allowed_roles}")
        return payload
