from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_current_tenant, get_db
from app.identity import require_role
from app.models import CompanyProfile, Role
from app.schemas import CompanyProfileIn, CompanyProfileOut

router = APIRouter(tags=["company-profile"])


@router.get("/company-profile", response_model=CompanyProfileOut)
def get_company_profile(db: Annotated[Session, Depends(get_db)]) -> CompanyProfile:
    profile = db.get(CompanyProfile, get_current_tenant())
    if profile is None:
        raise HTTPException(status_code=404, detail="Profil entreprise non renseigné.")
    return profile


@router.put(
    "/company-profile",
    response_model=CompanyProfileOut,
    dependencies=[Depends(require_role(Role.admin))],
)
def upsert_company_profile(
    payload: CompanyProfileIn, db: Annotated[Session, Depends(get_db)]
) -> CompanyProfile:
    # Un profil enregistré est complet pour son régime par construction : le
    # schéma exige tous les champs, vat_number compris au régime réel.
    profile = db.get(CompanyProfile, get_current_tenant())
    if profile is None:
        profile = CompanyProfile(tenant_id=get_current_tenant(), **payload.model_dump())
        db.add(profile)
    else:
        for field, value in payload.model_dump().items():
            setattr(profile, field, value)
    db.flush()
    return profile
