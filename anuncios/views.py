import json
import logging
import mimetypes
import os

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

from common.files import safe_media_path
from common.pagination import paginate
from users.models import UserProfile
from users.permissions import require_role
from . import services
from .models import Notice

# Pasta (relativa ao BASE_DIR) onde ficam os cadernos de encargos descarregados.
_ANUNCIOS_DIR = "pdf_Anuncios"

logger = logging.getLogger(__name__)
# Trilho de auditoria das edições de anúncios (quem alterou o quê) → consola + logs/anuncios.log.
audit_logger = logging.getLogger("anuncios.audit")

# Editar um anúncio — só ADMIN e COMMERCIAL (mesma regra dos avisos).
_EDIT_ROLES = (UserProfile.ADMIN, UserProfile.COMMERCIAL)

# "Tudo menos o id": qualquer campo próprio do anúncio pode ser editado, exceto a chave primária.
_EDITABLE_FIELDS = frozenset(f.name for f in Notice._meta.fields if not f.primary_key)


def _audit_value(value, limit: int = 300) -> str:
    """Representação curta de um valor para o log de auditoria (trunca listas/textos longos)."""
    s = repr(value)
    return s if len(s) <= limit else s[:limit] + "…[truncado]"


@csrf_exempt
@require_POST
def import_notices(request, num_days=15):
    """Query the base.gov.pt API, filter by keywords and store notices in the DB.

    POST only — it has side effects (writes to the DB and spawns the extraction process),
    so it must not be a safe GET. Registers the notices immediately (fast) and then kills
    any previous extraction and (re)launches the tender-specifications download in a
    SEPARATE process (survives the runserver reloads). num_days comes from the route:
    /anuncios/importar/ (15 by default) or /anuncios/importar/<n>/.
    """
    try:
        # 1) Fast notice registration (no specifications download) — quick response.
        summary = services.import_notices(num_days, download_specs=False)
        # 2) Kill the previous extraction and (re)launch the specifications download in a
        #    separate process (no second API fetch — the notices are already registered).
        services.spawn_specifications_download()
        summary["specifications"] = (
            "extraction (re)started in a separate process — progress in the runserver console"
        )
        return JsonResponse(summary, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except services.BaseGovError as e:
        return JsonResponse({"error": str(e)}, status=502)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def list_notices(request):
    """Listagem ENXUTA dos anúncios (não expirados), com filtros, ordenação e paginação.

    Query params: act_type, procedure_type, contract_type, order_by, page, page_size
    (default 50, máx. 200). See services.filter_notices / services.ORDERING. O detalhe
    completo vem de GET /anuncios/<id>/.
    """
    qs = services.filter_notices(request.GET)
    payload = paginate(request, qs, services.serialize_notice_summary, items_key="notices")
    return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})


def notice_detail(request, pk):
    """Detalhe COMPLETO de um anúncio (público)."""
    notice = Notice.objects.filter(pk=pk).first()
    if notice is None:
        return JsonResponse({"error": "Anúncio não encontrado."}, status=404)
    return JsonResponse(
        services.serialize_notice(notice),
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


@csrf_exempt
@require_http_methods(["PUT", "PATCH"])
@require_role(*_EDIT_ROLES)
def notice_edit(request, pk):
    """Atualiza um anúncio (por id, inalterável) — SÓ admin e commercial.
    PUT/PATCH {campo: valor, ...}. Campos fora da whitelist são ignorados."""
    notice = Notice.objects.filter(pk=pk).first()
    if notice is None:
        return JsonResponse({"error": "Anúncio não encontrado."}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Corpo JSON inválido."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Esperado um objeto JSON de campos a alterar."}, status=400)

    updated = [f for f in data if f in _EDITABLE_FIELDS]
    ignored = [f for f in data if f not in _EDITABLE_FIELDS]
    # Auditoria: captura o valor ANTIGO antes de aplicar, para registar antigo -> novo.
    changes = {}
    for f in updated:
        old = getattr(notice, f, None)
        if old != data[f]:
            changes[f] = (old, data[f])
        setattr(notice, f, data[f])
    if updated:
        try:
            # Valida SÓ os campos alterados (400 com detalhe, em vez de 500): campos não
            # tocados ficam de fora — registos antigos podem ter valores que já não validam.
            # validate_unique=False aqui; a unicidade do notice_number é apanhada pelo
            # IntegrityError abaixo (e evita uma query extra em cada edição).
            notice.full_clean(
                exclude=[f.name for f in Notice._meta.fields if f.name not in updated],
                validate_unique=False,
            )
            notice.save()
        except ValidationError as e:
            return JsonResponse(
                {"error": "Valores inválidos.", "details": e.message_dict}, status=400,
                json_dumps_params={"ensure_ascii": False},
            )
        except (IntegrityError, DataError, ValueError, TypeError) as e:
            return JsonResponse(
                {"error": f"Não foi possível gravar as alterações: {e}"}, status=400,
                json_dumps_params={"ensure_ascii": False},
            )
        if changes:
            audit_logger.info(
                "EDIÇÃO anúncio %s (id=%s) por %s: %s",
                notice.notice_number or "?", notice.pk, request.user.username,
                " | ".join(
                    f"{f}: {_audit_value(old)} -> {_audit_value(new)}"
                    for f, (old, new) in changes.items()
                ),
            )
        else:
            audit_logger.info(
                "EDIÇÃO anúncio %s (id=%s) por %s: sem alterações efetivas (valores iguais).",
                notice.notice_number or "?", notice.pk, request.user.username,
            )
    return JsonResponse(
        {"id": notice.id, "notice_number": notice.notice_number,
         "updated": updated, "ignored": ignored},
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


def serve_notice_specifications(request, pk):
    """Serve o caderno de encargos (pdf_Anuncios, de specifications_path) para abrir no browser
    (inline). Público. O front-end liga a este URL com target=\"_blank\"."""
    notice = Notice.objects.filter(pk=pk).only("id", "specifications_path").first()
    if notice is None:
        return JsonResponse({"error": "Anúncio não encontrado."}, status=404)
    path = safe_media_path(notice.specifications_path, _ANUNCIOS_DIR)
    if not path:
        return JsonResponse({"error": "Caderno de encargos não disponível."}, status=404)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(open(path, "rb"), content_type=ctype,
                        as_attachment=False, filename=os.path.basename(path))
