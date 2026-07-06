from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.audit_chain import ChainVerification, verify_chain
from app.db import get_current_tenant, get_db
from app.identity import require_role
from app.models import Role
from app.schemas import AuditVerifyOut

router = APIRouter(tags=["audit"])


@router.get(
    "/audit/verify",
    response_model=AuditVerifyOut,
    dependencies=[Depends(require_role(Role.admin))],
)
def verify_audit_chain(db: Annotated[Session, Depends(get_db)]) -> ChainVerification:
    """Recalcule la chaîne d'audit du tenant courant depuis audit_log seul
    et rend son intégrité, ou le point exact de rupture."""
    return verify_chain(db, get_current_tenant())
