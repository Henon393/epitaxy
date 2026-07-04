import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import Role


class CustomerCreate(BaseModel):
    name: str
    email: EmailStr


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str


class SignupRequest(BaseModel):
    tenant_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    tenant_id: uuid.UUID
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class SignupResponse(TokenPair):
    tenant_id: uuid.UUID
    user_id: uuid.UUID


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    role: Role


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: Role
    is_active: bool


class MeOut(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
