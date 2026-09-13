"""Calcul de TVA et numérotation séquentielle légale.

Décimal de bout en bout, arrondi half-up explicite, TVA arrondie PAR TAUX
(et non par ligne) pour éviter la dérive d'arrondi ligne à ligne.

Distinction stricte taux zéro / pas de taux : Decimal("0") est un taux du
régime réel qui alimente la ventilation avec une TVA nulle ; None (franchise
en base) n'alimente aucune ventilation. Le test est toujours « is None »,
jamais la valeur.
"""

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

_CENT = Decimal("0.01")

MENTION_293B = "TVA non applicable, art. 293 B du CGI"

# Taux français en vigueur. Le taux zéro est un vrai taux (exonérations,
# exports), distinct de l'absence de taux en franchise en base.
ALLOWED_VAT_RATES = {
    Decimal("0.00"),
    Decimal("2.10"),
    Decimal("5.50"),
    Decimal("10.00"),
    Decimal("20.00"),
}


def _round2(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def line_total_ht(quantity: Decimal, unit_price_ht: Decimal) -> Decimal:
    return _round2(quantity * unit_price_ht)


@dataclass(frozen=True)
class Totals:
    total_ht: Decimal
    total_tva: Decimal
    total_ttc: Decimal
    # {"20.00": {"base": "100.00", "tva": "20.00"}, ...}, valeurs en str
    # pour un JSONB sans flottants.
    breakdown: dict[str, dict[str, str]]


def compute_totals(lines: list[tuple[Decimal, Decimal, Decimal | None]]) -> Totals:
    """Totaux et ventilation par taux depuis (quantité, PU HT, taux | None)."""
    total_ht = Decimal("0.00")
    bases: dict[Decimal, Decimal] = {}
    for quantity, unit_price, vat_rate in lines:
        amount = line_total_ht(quantity, unit_price)
        total_ht += amount
        if vat_rate is not None:
            rate_key = vat_rate.quantize(_CENT)
            bases[rate_key] = bases.get(rate_key, Decimal("0.00")) + amount

    breakdown: dict[str, dict[str, str]] = {}
    total_tva = Decimal("0.00")
    for rate in sorted(bases):
        base = bases[rate]
        tva = _round2(base * rate / 100)
        total_tva += tva
        breakdown[str(rate)] = {"base": str(base), "tva": str(tva)}

    return Totals(
        total_ht=total_ht,
        total_tva=total_tva,
        total_ttc=total_ht + total_tva,
        breakdown=breakdown,
    )


def next_invoice_number(session: Session, tenant_id: uuid.UUID) -> int:
    """Prochain numéro du tenant, sous verrou de ligne sur son compteur.

    L'UPSERT prend le verrou jusqu'à la fin de la transaction : les émissions
    concurrentes d'un même tenant se sérialisent, un rollback rend le numéro
    (sans trou car transactionnel, sans doublon car verrouillé). À appeler le
    plus tard possible dans la transaction d'émission.
    """
    return session.execute(
        text(
            "INSERT INTO invoice_counters (tenant_id, last_number) VALUES (:tenant_id, 1) "
            "ON CONFLICT (tenant_id) DO UPDATE "
            "SET last_number = invoice_counters.last_number + 1 "
            "RETURNING last_number"
        ),
        {"tenant_id": str(tenant_id)},
    ).scalar_one()
