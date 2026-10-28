from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import Admin, Session, Viewer
from app.models import User
from app.security import create_access_token, hash_password, verify_password
from app.services import audit

router = APIRouter(tags=["auth"])


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8)
    role: Literal["viewer", "operator", "admin"] = "viewer"


def user_json(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role, "created_at": u.created_at}


@router.post("/auth/login")
async def login(session: Session, form: Annotated[OAuth2PasswordRequestForm, Depends()]):
    user = (await session.execute(select(User).where(User.username == form.username))).scalar()
    if not user or not verify_password(form.password, user.hashed_password):
        await audit.record(session, form.username, "auth.login_failed")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    await audit.record(session, user.username, "auth.login")
    return {
        "access_token": create_access_token(user.username, user.role),
        "token_type": "bearer",
        "user": user_json(user),
    }


@router.get("/auth/me")
async def me(user: Viewer):
    return user_json(user)


@router.get("/users")
async def list_users(session: Session, _: Admin):
    return [user_json(u) for u in (await session.execute(select(User).order_by(User.username))).scalars()]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, session: Session, admin: Admin):
    if (await session.execute(select(User).where(User.username == body.username))).scalar():
        raise HTTPException(409, "Username already exists")
    user = User(username=body.username, hashed_password=hash_password(body.password), role=body.role)
    session.add(user)
    await session.commit()
    await audit.record(session, admin.username, "user.created", body.username, {"role": body.role})
    return user_json(user)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str, session: Session, admin: Admin):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.username == admin.username:
        raise HTTPException(400, "You cannot delete yourself")
    await session.delete(user)
    await session.commit()
    await audit.record(session, admin.username, "user.deleted", user.username)
