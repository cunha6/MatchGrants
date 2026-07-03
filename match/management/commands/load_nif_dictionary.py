"""
Load dictionary_by_nif.json into the 'nif' SQLite database (NifCompany).

The JSON is a dict keyed by NIF (~454k entries). Keys are matched by accent-insensitive
substring so the exact accented spelling does not matter.

Usage:
    python manage.py load_nif_dictionary
    python manage.py load_nif_dictionary --path dictionary_by_nif.json
"""

import json
import unicodedata
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand

from match.models import NifCompany
from match.scoring_rules import classify_dimension

BATCH = 5000


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def _find_key(record: dict, *needles) -> str | None:
    """First key whose accent-insensitive form contains all needles."""
    for key in record:
        nk = _norm(key)
        if all(n in nk for n in needles):
            return key
    return None


def _to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip().replace(" ", "").replace(",", ".")))
    except (ValueError, TypeError):
        return None


def _to_decimal(value):
    if value in (None, ""):
        return None
    s = str(value).strip().replace(" ", "").replace("€", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


class Command(BaseCommand):
    help = "Load dictionary_by_nif.json into the 'nif' SQLite database."

    def add_arguments(self, parser):
        parser.add_argument("--path", default="dictionary_by_nif.json",
                            help="Path to the JSON file (default: dictionary_by_nif.json).")

    def handle(self, *args, **options):
        path = settings.BASE_DIR / options["path"]
        self.stdout.write(f"Loading {path} ...")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.stdout.write(f"{len(data)} entries; resolving keys and inserting...")

        # Resolve the accented column keys once, from the first record.
        sample = next(iter(data.values()), {})
        k_name = _find_key(sample, "nome")
        k_loc = _find_key(sample, "localidade")
        k_mun = _find_key(sample, "concelho")
        k_dist = _find_key(sample, "distrito")
        k_reg = _find_key(sample, "regiao")
        k_post = _find_key(sample, "codigo", "postal")
        k_emp = _find_key(sample, "empregados")
        k_rev = _find_key(sample, "proveitos")
        k_year = _find_key(sample, "ano", "dispon")

        def val(rec, key):
            return rec.get(key) if key else None

        NifCompany.objects.all().delete()  # full reload (idempotent)

        batch, total = [], 0
        for nif, rec in data.items():
            rec = rec or {}
            employees = _to_int(val(rec, k_emp))
            operating_revenue = _to_decimal(val(rec, k_rev))
            batch.append(NifCompany(
                nif=str(nif)[:20],
                name=str(val(rec, k_name) or "")[:255],
                locality=str(val(rec, k_loc) or "")[:255],
                municipality=str(val(rec, k_mun) or "")[:255],
                district=str(val(rec, k_dist) or "")[:255],
                region=str(val(rec, k_reg) or "")[:255],
                postal_code=str(val(rec, k_post) or "")[:20],
                employees=employees,
                operating_revenue=operating_revenue,
                dimension=classify_dimension(employees, operating_revenue),
                last_year=str(val(rec, k_year) or "")[:20],
            ))
            if len(batch) >= BATCH:
                NifCompany.objects.bulk_create(batch, batch_size=BATCH)
                total += len(batch)
                batch = []
                self.stdout.write(f"  {total} inserted...", ending="\r")
        if batch:
            NifCompany.objects.bulk_create(batch, batch_size=BATCH)
            total += len(batch)

        self.stdout.write(self.style.SUCCESS(f"\nDone — {total} NIF records loaded."))
