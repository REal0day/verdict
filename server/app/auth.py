import datetime as dt
import hashlib
import secrets

from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
import bcrypt
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from . import models

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")
ALGO = "HS256"


# ---- passwords ----
def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode("utf-8")[:72], bcrypt.gensalt()).decode("ascii")


def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode("utf-8")[:72], h.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---- JWT ----
def create_access_token(sub: str) -> str:
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=settings.access_token_expire_min)
    return jwt.encode({"sub": sub, "exp": exp}, settings.secret_key, algorithm=ALGO)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> models.User:
    cred_exc = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGO])
        sub = payload.get("sub")
    except JWTError:
        raise cred_exc
    user = db.query(models.User).filter(models.User.id == sub).first()
    if not user:
        raise cred_exc
    return user


# ---- agent API keys ----
def new_api_key() -> tuple[str, str]:
    """Return (plaintext_key, sha256_hash). Plain key shown once to user."""
    key = "irs_" + secrets.token_urlsafe(32)
    return key, hashlib.sha256(key.encode()).hexdigest()


# ---- share-link tokens (guest triage) ----
def new_share_token() -> tuple[str, str, str]:
    """Return (plaintext_token, sha256_hash, prefix). Plaintext is shown
    once on creation and never persisted."""
    tok = secrets.token_urlsafe(32)
    return tok, hashlib.sha256(tok.encode()).hexdigest(), tok[:8]


def hash_share_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def get_current_agent(
    request: Request,
    x_agent_key: str | None = Header(default=None, alias="X-Agent-Key"),
    x_agent_version: str | None = Header(default=None, alias="X-Agent-Version"),
    db: Session = Depends(get_db),
) -> models.Agent:
    if not x_agent_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-Agent-Key")
    h = hashlib.sha256(x_agent_key.encode()).hexdigest()
    agent = db.query(models.Agent).filter(models.Agent.api_key_hash == h).first()
    if not agent:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent key")
    agent.last_seen = dt.datetime.now(dt.timezone.utc)
    fwd = request.headers.get("x-forwarded-for")
    ip = (fwd.split(",")[0].strip() if fwd else None) or (
        request.client.host if request.client else None
    )
    if ip and ip != agent.last_ip:
        agent.last_ip = ip[:45]
    if x_agent_version and x_agent_version != agent.version:
        agent.version = x_agent_version[:32]
    db.commit()
    return agent
