"""Shared FastAPI dependencies: DB session, current user, role guards."""

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security import decode_token, role_at_least

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
Session = Annotated[AsyncSession, Depends(get_session)]


async def current_user(session: Session, token: Annotated[str, Depends(oauth2)]) -> User:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    user = (await session.execute(select(User).where(User.username == payload.get("sub")))).scalar()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user


def require(role: str):
    async def guard(user: Annotated[User, Depends(current_user)]) -> User:
        if not role_at_least(user.role, role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires {role} role")
        return user

    return guard


Viewer = Annotated[User, Depends(require("viewer"))]
Operator = Annotated[User, Depends(require("operator"))]
Admin = Annotated[User, Depends(require("admin"))]
