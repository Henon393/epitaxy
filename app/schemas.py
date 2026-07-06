import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.invoicing import ALLOWED_VAT_RATES
from app.models import OperationCategory, Role, VatRegime

SIREN_PATTERN = r"^\d{9}$"
SIRET_PATTERN = r"^\d{14}$"
# ISO 3166-1 alpha-2 : deux lettres majuscules, refusé à la saisie plutôt
# qu'à la validation Schematron de l'étape 4b.
COUNTRY_CODE_PATTERN = r"^[A-Z]{2}$"


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    # Facultatifs à la création, exigés à l'émission d'une facture.
    siren: str | None = Field(default=None, pattern=SIREN_PATTERN)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    postal_code: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=100)
    country_code: str | None = Field(default=None, pattern=COUNTRY_CODE_PATTERN)


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    siren: str | None
    address_line1: str | None
    address_line2: str | None
    postal_code: str | None
    city: str | None
    country_code: str | None


class CompanyProfileIn(BaseModel):
    legal_name: str = Field(min_length=1, max_length=200)
    siren: str = Field(pattern=SIREN_PATTERN)
    siret: str | None = Field(default=None, pattern=SIRET_PATTERN)
    address_line1: str = Field(min_length=1, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    postal_code: str = Field(min_length=1, max_length=10)
    city: str = Field(min_length=1, max_length=100)
    country_code: str = Field(pattern=COUNTRY_CODE_PATTERN)
    vat_number: str | None = Field(default=None, min_length=4, max_length=20)
    legal_form: str = Field(min_length=1, max_length=50)
    vat_regime: VatRegime

    @model_validator(mode="after")
    def _vat_number_selon_regime(self) -> "CompanyProfileIn":
        if self.vat_regime == VatRegime.reel_normal and self.vat_number is None:
            raise ValueError(
                "vat_number est requis au régime réel normal (optionnel en franchise en base)."
            )
        return self


class CompanyProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    legal_name: str
    siren: str
    siret: str | None
    address_line1: str
    address_line2: str | None
    postal_code: str
    city: str
    country_code: str
    vat_number: str | None
    legal_form: str
    vat_regime: VatRegime


class InvoiceLineIn(BaseModel):
    designation: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Field(gt=0)
    unit_price_ht: Decimal = Field(ge=0)
    # None = pas de taux applicable (franchise en base). Distinct du taux
    # zéro Decimal("0"), qui est un taux du régime réel.
    vat_rate: Decimal | None = None

    @field_validator("vat_rate")
    @classmethod
    def _taux_francais(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if value not in ALLOWED_VAT_RATES:
            raise ValueError(f"taux de TVA inconnu : {value}")
        return value


class InvoiceCreate(BaseModel):
    customer_id: uuid.UUID
    operation_category: OperationCategory
    vat_on_debits: bool = False
    delivery_address: str | None = Field(default=None, max_length=500)
    supply_date: date | None = None
    due_date: date | None = None
    lines: list[InvoiceLineIn] = Field(min_length=1)


class InvoiceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    designation: str
    quantity: Decimal
    unit_price_ht: Decimal
    vat_rate: Decimal | None
    total_ht: Decimal


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    number: int | None
    status: str
    issue_date: date | None
    supply_date: date | None
    due_date: date | None
    operation_category: OperationCategory
    vat_on_debits: bool
    delivery_address: str | None
    total_ht: Decimal
    total_tva: Decimal
    total_ttc: Decimal
    vat_breakdown: dict[str, dict[str, str]]
    vat_mention: str | None
    seller_legal_name: str | None
    seller_siren: str | None
    seller_siret: str | None
    seller_address_line1: str | None
    seller_address_line2: str | None
    seller_postal_code: str | None
    seller_city: str | None
    seller_country_code: str | None
    seller_vat_number: str | None
    seller_legal_form: str | None
    seller_vat_regime: str | None
    buyer_name: str | None
    buyer_siren: str | None
    buyer_address_line1: str | None
    buyer_address_line2: str | None
    buyer_postal_code: str | None
    buyer_city: str | None
    buyer_country_code: str | None
    lines: list[InvoiceLineOut]


class AuditVerifyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    intact: bool
    entries: int
    broken_at_position: int | None
    reason: str | None


class StatusEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    position: int
    status: str
    status_code: str
    paid_amount: Decimal | None
    paid_at: date | None
    reason: str | None
    created_at: datetime


class TransmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    pa_transmission_ref: str
    routing_identifier: str
    created_at: datetime
    current_status: str | None
    events: list[StatusEventOut]


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
