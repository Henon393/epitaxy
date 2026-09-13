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


# --- Jeu d'exemples pour /docs -------------------------------------------
#
# Sans ces exemples, Swagger UI pré-remplit les formulaires depuis les seuls
# types et patterns : « string », « user@example.com », un code pays aléatoire
# à deux lettres. Inutilisable tel quel. Le jeu ci-dessous est cohérent d'un
# formulaire à l'autre (même vendeur, même client, mêmes montants) pour qu'un
# parcours complet s'enchaîne sans rien inventer.
#
# Entreprises et personnes fictives. Les identifiants sont en revanche
# structurellement valides, SIREN et SIRET à clé de Luhn correcte, numéro de
# TVA à clé française correcte, pour ne pas achopper sur une validation.
EX_SELLER_SIREN = "834521700"
EX_SELLER_SIRET = "83452170000007"
EX_SELLER_VAT = "FR59834521700"
EX_BUYER_SIREN = "902183649"
EX_TENANT_ID = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
EX_CUSTOMER_ID = "3f1c9d5e-8a24-4b17-9c30-5d6e7f801234"
# Valeur manifestement à remplacer : la doc n'est pas un porte-secrets.
EX_PASSWORD = "MotDePasseDemo!2026"
EX_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.PLACEHOLDER.a-remplacer"


def _example(payload: dict) -> dict:
    """json_schema_extra portant l'exemple sous les deux clés reconnues.

    ``examples`` (tableau) est la forme JSON Schema / OpenAPI 3.1 ; ``example``
    est la forme 3.0. Les deux sont émises pour que le pré-remplissage marche
    quelle que soit la version de Swagger UI servie.
    """
    return {"examples": [payload], "example": payload}


class CustomerCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra=_example(
            {
                "name": "Menuiserie Lambert SARL",
                "email": "comptabilite@menuiserie-lambert.fr",
                "siren": EX_BUYER_SIREN,
                "address_line1": "8 avenue Jean Jaurès",
                "address_line2": "Bâtiment C",
                "postal_code": "69007",
                "city": "Lyon",
                "country_code": "FR",
            }
        )
    )

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
    model_config = ConfigDict(
        json_schema_extra=_example(
            {
                "legal_name": "Atelier Bertin SAS",
                "siren": EX_SELLER_SIREN,
                "siret": EX_SELLER_SIRET,
                "address_line1": "12 rue des Lilas",
                "address_line2": "Zone artisanale des Chênes",
                "postal_code": "44000",
                "city": "Nantes",
                "country_code": "FR",
                "vat_number": EX_SELLER_VAT,
                "legal_form": "SAS",
                "vat_regime": "reel_normal",
            }
        )
    )

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
    model_config = ConfigDict(
        json_schema_extra=_example(
            {
                "designation": "Prestation de conseil, audit technique",
                "quantity": "2",
                "unit_price_ht": "520.00",
                "vat_rate": "20.00",
            }
        )
    )

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
    # Trois taux réels (20 / 10 / 5,5), d'où la catégorie « mixte » : deux
    # prestations et un bien. Totaux correspondants : 2 363,50 HT,
    # 337,04 de TVA, 2 700,54 TTC.
    model_config = ConfigDict(
        json_schema_extra=_example(
            {
                "customer_id": EX_CUSTOMER_ID,
                "operation_category": "mixte",
                "vat_on_debits": False,
                "delivery_address": "8 avenue Jean Jaurès, 69007 Lyon",
                "supply_date": "2026-09-10",
                "due_date": "2026-10-10",
                "lines": [
                    {
                        "designation": "Prestation de conseil, audit technique",
                        "quantity": "2",
                        "unit_price_ht": "520.00",
                        "vat_rate": "20.00",
                    },
                    {
                        "designation": (
                            "Fourniture et pose d'étagères sur mesure "
                            "(logement de plus de deux ans)"
                        ),
                        "quantity": "1",
                        "unit_price_ht": "1250.00",
                        "vat_rate": "10.00",
                    },
                    {
                        "designation": "Ouvrage documentaire imprimé",
                        "quantity": "3",
                        "unit_price_ht": "24.50",
                        "vat_rate": "5.50",
                    },
                ],
            }
        )
    )

    customer_id: uuid.UUID = Field(
        description="Identifiant renvoyé par POST /customers, à remplacer par le vôtre."
    )
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


class SimulateStatusIn(BaseModel):
    """Pilotage du mock PA, démonstration et dev uniquement."""

    # Montant et date alignés sur la facture d'exemple d'InvoiceCreate.
    model_config = ConfigDict(
        json_schema_extra=_example(
            {
                "status": "encaissee",
                "paid_amount": "2700.54",
                "paid_at": "2026-10-10",
                "reason": None,
            }
        )
    )

    status: str
    paid_amount: Decimal | None = None
    paid_at: date | None = None
    reason: str | None = None


class StatusEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    position: int
    status: str
    status_code: str
    paid_amount: Decimal | None
    paid_at: date | None
    reason: str | None
    pa_event_ref: str | None
    out_of_graph: bool
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
    model_config = ConfigDict(
        json_schema_extra=_example(
            {
                "tenant_name": "Atelier Bertin",
                "email": "marie.bertin@atelier-bertin.fr",
                "password": EX_PASSWORD,
            }
        )
    )

    tenant_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra=_example(
            {
                "tenant_id": EX_TENANT_ID,
                "email": "marie.bertin@atelier-bertin.fr",
                "password": EX_PASSWORD,
            }
        )
    )

    tenant_id: uuid.UUID = Field(
        description="Identifiant renvoyé par POST /auth/signup, à remplacer par le vôtre."
    )
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra=_example({"refresh_token": EX_JWT}))

    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class MfaChallengeOut(BaseModel):
    """Authentification partielle : mot de passe validé, second facteur
    exigé. Le jeton intermédiaire n'ouvre aucune ressource."""

    mfa_required: bool = True
    mfa_token: str


class MfaVerifyIn(BaseModel):
    model_config = ConfigDict(json_schema_extra=_example({"mfa_token": EX_JWT, "code": "123456"}))

    mfa_token: str
    code: str = Field(min_length=6, max_length=20)


class MfaEnrollOut(BaseModel):
    otpauth_uri: str
    qr_svg: str


class MfaCodeIn(BaseModel):
    # Code à six chiffres de l'application d'authentification, ou l'un des
    # codes de secours remis à l'activation.
    model_config = ConfigDict(json_schema_extra=_example({"code": "123456"}))

    code: str = Field(min_length=6, max_length=20)


class MfaActivateOut(BaseModel):
    # Affichés UNE SEULE FOIS : jamais restitués ensuite.
    backup_codes: list[str]


class SignupResponse(TokenPair):
    tenant_id: uuid.UUID
    user_id: uuid.UUID


class UserCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra=_example(
            {
                "email": "paul.durand@atelier-bertin.fr",
                "password": EX_PASSWORD,
                "role": "comptable",
            }
        )
    )

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
