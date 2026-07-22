"""
base.gov.pt API integration (GetInfoAnuncio) and the import/filter engine.

Flow: fetch the API -> filter by keywords in the description -> upsert into the DB (only
matches) -> list with filters/orderings. Notices past their proposal deadline are hidden
from the listing. The tender-specifications download lives in specifications.py.
"""

import os
import logging
import subprocess
import sys
import time
from datetime import date
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import connection
from django.db.models import Q

from common.dates import parse_date as _parse_date
from common.files import safe_media_path
from .models import Notice
from .specifications import SPECS_DIR, build_chrome, fetch_specifications, normalize

logger = logging.getLogger(__name__)

API_URL = "https://www.base.gov.pt/APIBase2/GetInfoAnuncio"
TIMEOUT = 30  # seconds

# Keywords of interest — only notices whose description contains one are stored.
# Comparison is accent-insensitive and case-insensitive (see specifications.normalize).
KEYWORDS = [
    "Consultoria", "Consultadoria", "Assessoria", "Candidatura", "Estudo", "Plano",
    "Impacto", "Competitividade", "Fundos", "Incentivos", "Avaliação", "Viabilidade",
    "Empreendedorismo", "Investimento", "Estratégia", "Estratégica", "Estratégico",
    "Sustentabilidade", "Sustentável", "Capacitação", "Internacionalização",
    "Desfavorecidas", "Social", "Inovação", "Clima", "Climática", "Climático",
    "Território", "Territorial", "Ecologia", "Ecológica", "Ecológico",
    "Compras públicas", "Compras sustentáveis", "Compras ecológicas", "ECO 360",
    "Turismo", "Interior", "Valorização", "Recursos", "Projeto", "Verdes",
    "Ambiental", "Ambiente", "Pegada Ecológica", "Pegada Carbono", "Serviços",
]
_NORM_KEYWORDS = [normalize(k) for k in KEYWORDS]

# Orderings supported by the listing (?order_by=).
ORDERING = {
    "publication_recent": "-publication_date",
    "publication_oldest": "publication_date",
    "deadline_latest": "-proposal_deadline",
    "deadline_earliest": "proposal_deadline",
    "price_highest": "-base_price",
    "price_lowest": "base_price",
}

_LOCK_FILE = "anuncios_import.lock"
_LOCK_MAX_AGE = 10 * 60 
_PID_FILE = "anuncios_import.pid"


class BaseGovError(Exception):
    """Configuration or communication failure with the base.gov.pt API."""


def matched_keywords(description: str) -> list[str]:
    """Return the keywords (original spelling) present in the description."""
    n = normalize(description)
    return [kw for kw, nk in zip(KEYWORDS, _NORM_KEYWORDS) if nk in n]


# --- Tolerant parsers for API data ----------------------------------------
def _pick(raw: dict, *keys):
    """First non-empty value among key variants (case-insensitive).

    The base.gov API mixes camelCase and PascalCase (e.g. 'nAnuncio' but 'PrecoBase',
    'DataLimitePropostas'); comparing case-insensitively avoids losing fields.
    """
    if not raw:
        return None
    low = {k.lower(): v for k, v in raw.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _parse_decimal(value):
    if value in (None, ""):
        return None
    s = str(value).replace("€", "").replace(" ", "").strip()
    if "," in s and "." in s:  # PT format "1.234.567,89" -> "1234567.89"
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _parse_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("s", "sim", "true", "1", "x", "yes")


def _parse_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [value]


def _map_notice(raw: dict) -> dict:
    """Map a raw API record to the model field dict.

    API keys are tried in several capitalization variants; if the real response uses
    different names, this is the (only) place to adjust.
    """
    return {
        "incm_id": str(_pick(raw, "idINCM", "idIncm", "IdIncm", "id") or "")[:20],
        "notice_number": str(_pick(raw, "nAnuncio", "numAnuncio", "NumAnuncio") or "")[:20],
        "publication_date": _parse_date(_pick(raw, "dataPublicacao", "DataPublicacao", "dataPublicação")),
        "entity_nif": str(_pick(raw, "nifEntidade", "NifEntidade", "nif") or "")[:9],
        "entity_name": str(_pick(raw, "designacaoEntidade", "DesignacaoEntidade") or "")[:255],
        "description": str(_pick(raw, "descricaoAnuncio", "DescricaoAnuncio") or ""),
        "url": str(_pick(raw, "url", "URL", "urlAnuncio") or "")[:500],
        "dr_number": str(_pick(raw, "numDR", "numDr", "NumDR") or "")[:10],
        "series": str(_pick(raw, "serie", "Serie") or "")[:5],
        "act_type": str(_pick(raw, "tipoActo", "tipoAto", "TipoActo") or ""),
        "contract_types": _parse_list(_pick(raw, "tiposContrato", "tipoContrato", "TiposContrato")),
        "cpvs": _parse_list(_pick(raw, "cpvs", "CPVs", "cpv")),
        "lots": _parse_list(_pick(raw, "lotes", "Lotes")),
        "base_price": _parse_decimal(_pick(raw, "precoBase", "PrecoBase")),
        "procedure_type": str(_pick(raw, "modeloAnuncio", "ModeloAnuncio") or ""),
        "year": _parse_int(_pick(raw, "ano", "Ano")),
        "environmental_criteria": _parse_bool(_pick(raw, "criterAmbient", "criterioAmbiental", "CriterAmbient")),
        "proposal_period_days": _parse_int(_pick(raw, "prazoPropostas", "PrazoPropostas")),
        "procedure_documents_url": str(_pick(raw, "pecasProcedimento", "PecasProcedimento") or "")[:500],
        "proposal_deadline": _parse_date(_pick(raw, "dataLimiteProposta", "DataLimiteProposta", "dataLimitePropostas")),
    }


# --- API fetch -------------------------------------------------------------
def fetch_notices(num_days: int = 15) -> list[dict]:
    """Query the base.gov.pt API and return the raw notices list."""
    token = getattr(settings, "BASE_KEY", None)
    if not token:
        raise BaseGovError("BASE_KEY not configured in the environment (.env).")
    try:
        response = requests.get(
            API_URL, params={"numDias": num_days},
            headers={"_AcessToken": token}, timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise BaseGovError(f"Failed to contact the base.gov.pt API: {exc}")

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("anuncios", "Anuncios", "data", "result", "items", "value"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


# --- Persistence -----------------------------------------------------------

def _upsert_notice(data: dict) -> str:
    """Create/update the notice. Returns 'created', 'updated' or 'unchanged'.

    Since notice_number is unique, a rectification/amendment arrives with the SAME
    notice_number and must update the existing row. Rules:
    - empty values (None/""/[]) do NOT overwrite stored data (partial rectification);
    - only writes to the DB if some field actually changed.
    """
    obj = Notice.objects.filter(notice_number=data["notice_number"]).first()
    if obj is None:
        Notice.objects.create(**data)
        return "created"

    changed = []
    for field, value in data.items():
        if value in (None, "", []):
            continue
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed.append(field)

    if not changed:
        return "unchanged"

    obj.save(update_fields=changed + ["updated_at"])
    return "updated"


def existing_specifications_path(notice_number: str) -> str:
    """Return the already-saved specifications path for this notice, if the file exists."""
    if not notice_number:
        return ""
    path = (
        Notice.objects.filter(notice_number=notice_number)
        .values_list("specifications_path", flat=True)
        .first()
    )
    if path and (settings.BASE_DIR / path).exists():
        return path
    return ""


# --- Import ----------------------------------------------------------------

def import_notices(num_days: int = 15, download_specs: bool = True, should_stop=None) -> dict:
    """Import notices: filter by keywords and upsert only the matches.

    Idempotent by `notice_number`. Returns a summary.

    `download_specs`: if False, only registers the notices (fast, no browser) — used by
    the HTTP route so it does not time out. `should_stop`: optional callable; if it
    returns True the import stops (to cancel a previous run when the route is called again).
    """
    raw_list = fetch_notices(num_days)
    today = date.today()
    with_keywords = 0
    created = updated = unchanged = 0

    # Chrome shared across the whole import: starts lazily on the first notice that needs
    # rendering (SPA), is reused by the rest, and is closed at the end.
    driver_cache = {}

    def driver_factory():
        if "driver" not in driver_cache:
            driver_cache["driver"] = build_chrome()
        return driver_cache["driver"]

    try:
        for raw in raw_list:
            if should_stop and should_stop():
                logger.info("  [import_notices] cancelled (a new import started).")
                break
            description = _pick(raw, "descricaoAnuncio", "DescricaoAnuncio") or ""
            if not matched_keywords(description):
                continue
            with_keywords += 1

            _heartbeat_lock()

            data = _map_notice(raw)
            if not data["notice_number"]:
                continue  # no natural key -> cannot deduplicate

            deadline = data["proposal_deadline"]
            data["active"] = bool(deadline and deadline >= today)

            # Specifications (only when download_specs): reuse the already-downloaded file,
            # else try to download (sharing the Chrome). Log per notice.
            number = data["notice_number"]
            if not download_specs:
                data["specifications_path"] = ""  # "" does not overwrite a stored path
            else:
                reused = existing_specifications_path(number)
                if reused:
                    data["specifications_path"] = reused
                    logger.info(f"  [{number}] Specifications already downloaded -> {os.path.basename(reused)}")
                elif data["procedure_documents_url"]:
                    logger.info(f"  [{number}] Looking for specifications...")
                    connection.close()  # avoid a stale DB connection during the long download
                    path = fetch_specifications(data["procedure_documents_url"], driver_factory=driver_factory)
                    data["specifications_path"] = path
                    logger.info(f"  [{number}] {'OK -> ' + os.path.basename(path) if path else 'No specifications found.'}")
                else:
                    data["specifications_path"] = ""
                    logger.info(f"  [{number}] No procedure-documents link.")

            status = _upsert_notice(data)
            if status == "created":
                created += 1
            elif status == "updated":
                updated += 1
            else:
                unchanged += 1
    finally:
        driver = driver_cache.get("driver")
        if driver:
            driver.quit()

    # Sincroniza o estado: qualquer anúncio cujo prazo de proposta já passou fica active=False.
    deactivated = deactivate_expired()

    return {
        "num_days": num_days,
        "total_received": len(raw_list),
        "with_keywords": with_keywords,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "deactivated_expired": deactivated,
    }


def download_missing_specifications() -> dict:
    """Backfill: for notices with no specifications_path (but with a documents link),
    download the tender specifications and store the path in the DB.

    Works only over the DB (does not hit the API). Uses one shared Chrome. Returns a summary.
    """
    pending = list(
        Notice.objects.exclude(procedure_documents_url="")
        .filter(specifications_path="")
        .values_list("pk", "notice_number", "procedure_documents_url")
    )
    total = len(pending)
    downloaded = missing = 0
    logger.info(f"[backfill specs] {total} notices without specifications to process...")

    driver_cache = {}

    def driver_factory():
        if "driver" not in driver_cache:
            driver_cache["driver"] = build_chrome()
        return driver_cache["driver"]

    mark_import_start()
    try:
        for pk, number, docs_url in pending:
            _heartbeat_lock()
            connection.close()  # avoid a stale DB connection during the long download
            logger.info(f"  [{number}] Looking for specifications...")
            path = fetch_specifications(docs_url, driver_factory=driver_factory)
            if path:
                Notice.objects.filter(pk=pk).update(specifications_path=path)
                downloaded += 1
                logger.info(f"  [{number}] OK, saved -> {os.path.basename(path)}")
            else:
                missing += 1
                logger.info(f"  [{number}] No specifications found.")
    finally:
        driver = driver_cache.get("driver")
        if driver:
            driver.quit()
        mark_import_end()

    logger.info(f"[backfill specs] done — downloaded: {downloaded}, missing: {missing}")
    return {"pending": total, "downloaded": downloaded, "missing": missing}


# --- Background process (lock/PID + spawn) --------------------------------

def _lock_path():
    return settings.BASE_DIR / _LOCK_FILE


def _pid_path():
    return settings.BASE_DIR / _PID_FILE


def import_running() -> bool:
    """True if an import is alive (lock touched less than _LOCK_MAX_AGE ago)."""
    lock = _lock_path()
    if not lock.exists():
        return False
    try:
        return (time.time() - lock.stat().st_mtime) < _LOCK_MAX_AGE
    except OSError:
        return False


def mark_import_start():
    """Create the lock, owned by the current process (called by the command on start).

    The content is this process's PID so mark_import_end only removes its own lock;
    freshness is the file mtime (updated by _heartbeat_lock).
    """
    _lock_path().write_text(str(os.getpid()), encoding="utf-8")


def _heartbeat_lock():
    """Refresh the lock mtime (called per notice) to keep it 'alive'."""
    lock = _lock_path()
    if lock.exists():
        try:
            os.utime(lock, None)
        except OSError:
            pass


def mark_import_end():
    """Remove the lock, but only if it belongs to this process.

    Avoids a concurrent run (e.g. the backfill command) deleting another run's lock.
    """
    lock = _lock_path()
    try:
        owner = lock.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    if owner == str(os.getpid()):
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def kill_previous_import():
    """Kill the previous import (tree: python + chromedriver + chrome), if any.

    Uses the PID stored by spawn_specifications_download. On Windows 'taskkill /T' kills the
    whole tree; on POSIX the process group (start_new_session) is killed. Also clears the
    lock and the PID file.
    """
    pid_file = _pid_path()
    pid = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
    if pid:
        try:
            if os.name == "nt":
                # taskkill /T mata a árvore toda (python + chromedriver + chrome).
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                # POSIX: o processo foi lançado com start_new_session (grupo próprio),
                # por isso killpg mata o grupo inteiro — incluindo o Chrome filho.
                import signal
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        except Exception:  # noqa: BLE001 — best-effort; the process may already be dead
            pass
    for f in (pid_file, _lock_path()):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def spawn_specifications_download() -> bool:
    """(Re)launch 'manage.py download_specifications' as a SEPARATE process.

    Being its own process (not a runserver thread), it SURVIVES the runserver auto-reloads,
    so the long Vortal extraction runs to the end. Downloads only the missing specifications
    (no API re-fetch — the notices were already registered by the route). Always kills the
    previous run first. On POSIX the child gets its own session/process group so the whole
    tree (python + chromedriver + chrome) can be killed together. Progress shows in the
    runserver console (inherited stdout, unbuffered with '-u'). Always returns True.
    """
    kill_previous_import()
    manage_py = settings.BASE_DIR / "manage.py"
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True  # new process group -> killable as a tree
    proc = subprocess.Popen(
        [sys.executable, "-u", str(manage_py), "download_specifications"],
        cwd=str(settings.BASE_DIR),
        **popen_kwargs,
    )
    _pid_path().write_text(str(proc.pid), encoding="utf-8")
    return True


def deactivate_expired() -> int:
    """Mark active=False the notices whose proposal deadline has passed. Returns the count."""
    return Notice.objects.filter(active=True, proposal_deadline__lt=date.today()).update(active=False)


# --- Listing with filters --------------------------------------------------

def filter_notices(params):
    """Build the listing queryset from the request query params.

    Always excludes expired notices (proposal_deadline < today). Notices with no deadline
    are kept (nothing to expire).

    Supported params: act_type, procedure_type, contract_type (exact / membership),
    order_by (one of ORDERING).
    """
    today = date.today()
    qs = Notice.objects.filter(
        Q(proposal_deadline__gte=today) | Q(proposal_deadline__isnull=True)
    )

    act_type = params.get("act_type")
    if act_type:
        qs = qs.filter(act_type=act_type)

    procedure_type = params.get("procedure_type")
    if procedure_type:
        qs = qs.filter(procedure_type=procedure_type)

    contract_type = params.get("contract_type")
    if contract_type:
        qs = qs.filter(contract_types__contains=[contract_type])

    order_by = params.get("order_by")
    return qs.order_by(ORDERING.get(order_by, "-publication_date"))


def serialize_notice_summary(n: Notice) -> dict:
    """Resumo ENXUTO para a listagem (o front-end mostra só o essencial; o detalhe vem do
    GET /anuncios/<id>/). Análogo ao resumo dos avisos."""
    return {
        "id": n.id,
        "notice_number": n.notice_number,
        "entity_name": n.entity_name,
        "act_type": n.act_type,
        "base_price": float(n.base_price) if n.base_price is not None else None,
        "proposal_deadline": n.proposal_deadline.isoformat() if n.proposal_deadline else None,
        "active": n.active,
    }


def serialize_notice(n: Notice) -> dict:
    """Convert a Notice to a JSON-serializable dict (detalhe completo)."""
    return {
        "id": n.id,
        "notice_number": n.notice_number,
        "incm_id": n.incm_id,
        "publication_date": n.publication_date.isoformat() if n.publication_date else None,
        "entity_nif": n.entity_nif,
        "entity_name": n.entity_name,
        "description": n.description,
        "url": n.url,
        "dr_number": n.dr_number,
        "series": n.series,
        "act_type": n.act_type,
        "contract_types": n.contract_types,
        "cpvs": n.cpvs,
        "lots": n.lots,
        "base_price": float(n.base_price) if n.base_price is not None else None,
        "procedure_type": n.procedure_type,
        "year": n.year,
        "environmental_criteria": n.environmental_criteria,
        "proposal_period_days": n.proposal_period_days,
        "procedure_documents_url": n.procedure_documents_url,
        "specifications_path": n.specifications_path,
        # Link para abrir o caderno de encargos local (só se o ficheiro existir em disco).
        # O front-end abre-o com target="_blank".
        "specifications_url": (
            f"/anuncios/{n.id}/specifications/"
            if safe_media_path(n.specifications_path, SPECS_DIR) else None
        ),
        "proposal_deadline": n.proposal_deadline.isoformat() if n.proposal_deadline else None,
        "active": n.active,
    }
