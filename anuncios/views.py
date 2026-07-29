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

# Editar um anúncio — só ADMIN e COMMERCIAL_PUBLIC (é o comercial "dono" deste domínio;
# COMMERCIAL_GRANTS não tem acesso a anúncios de todo).
_EDIT_ROLES = (UserProfile.ADMIN, UserProfile.COMMERCIAL_PUBLIC)

# Ver anúncios (listagem/detalhe/documentos) — admin, commercial_public e client. Anúncios não
# têm relação nenhuma com o match: quem não tem login só acede ao match, nunca aos anúncios.
_VIEW_ROLES = (UserProfile.ADMIN, UserProfile.COMMERCIAL_PUBLIC, UserProfile.CLIENT)

# `last_update_source`/`last_updated_by` são geridos pelo próprio código (não pelo payload do
# cliente) — ver notice_edit.
_NON_EDITABLE_FIELDS = frozenset({"last_update_source", "last_updated_by"})

# "Tudo menos o id" (e os campos regenerados acima): qualquer outro campo próprio do anúncio
# pode ser editado.
_EDITABLE_FIELDS = frozenset(
    f.name for f in Notice._meta.fields
    if not f.primary_key and f.name not in _NON_EDITABLE_FIELDS
)


def _audit_value(value, limit: int = 300) -> str:
    """Representação curta de um valor para o log de auditoria (trunca listas/textos longos)."""
    s = repr(value)
    return s if len(s) <= limit else s[:limit] + "…[truncado]"


@csrf_exempt
@require_POST
def import_notices(request):
    """POST /anuncios/?num_days=N — query the base.gov.pt API, filter by keywords and store
    notices in the DB. `num_days` é opcional (15 por omissão).

    POST only — it has side effects (writes to the DB and spawns the extraction process),
    so it must not be a safe GET. Registers the notices immediately (fast) and then kills
    any previous extraction and (re)launches the tender-specifications download in a
    SEPARATE process (survives the runserver reloads).
    """
    try:
        num_days = int(request.GET.get("num_days", 15))
    except (TypeError, ValueError):
        return JsonResponse({"error": "'num_days' tem de ser um inteiro."}, status=400)
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


@require_role(*_VIEW_ROLES)
def list_notices(request):
    """Listagem ENXUTA dos anúncios (não inativos por omissão), com pesquisa, filtros,
    ordenação e paginação. Exige sessão — anúncios não fazem parte do match.

    Query params: q (pesquisa em descrição/entidade/número — sobre TODOS os anúncios, não só a
    página), status ('active'/'inactive'/'to_fix' — por omissão mostra ativos + por corrigir,
    escondendo os inativos), act_type, procedure_type, contract_type, order_by, page, page_size
    (default 50, máx. 200). See services.filter_notices / services.ORDERING. O detalhe completo
    vem de GET /anuncios/<id>/.
    """
    qs = services.filter_notices(request.GET)
    payload = paginate(request, qs, services.serialize_notice_summary, items_key="notices")
    return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})


@require_role(*_VIEW_ROLES)
def notice_filters(request):
    """GET /anuncios/filters/ — valores de act_type/contract_types REALMENTE presentes entre os
    anúncios navegáveis, para popular os selects de filtro dinamicamente (nunca uma opção sem
    resultados, nunca um valor novo em falta). Exige sessão."""
    return JsonResponse(services.filter_options(), json_dumps_params={"ensure_ascii": False, "indent": 2})


@require_role(*_VIEW_ROLES)
def notice_detail(request, pk):
    """Detalhe COMPLETO de um anúncio. Exige sessão — ao contrário dos avisos, os anúncios não
    são acedidos a partir do match, por isso não há aqui exceção para quem não tem login."""
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
        # Qualquer escrita por este endpoint é uma edição MANUAL — marca a origem e quem a fez,
        # para se distinguir do que a importação (base.gov.pt) grava.
        notice.last_update_source = Notice.SOURCE_MANUAL
        notice.last_updated_by = request.user
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


def _serve_notice_document(pk, field: str, missing_msg: str):
    """Serve um documento local do anúncio (o campo `field` guarda o caminho relativo, sob
    pdf_Anuncios/) para abrir no browser (inline). Base comum ao caderno de encargos e ao
    programa de concurso — o front-end liga a estes URLs com target=\"_blank\"."""
    notice = Notice.objects.filter(pk=pk).only("id", field).first()
    if notice is None:
        return JsonResponse({"error": "Anúncio não encontrado."}, status=404)
    path = safe_media_path(getattr(notice, field), _ANUNCIOS_DIR)
    if not path:
        return JsonResponse({"error": missing_msg}, status=404)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(open(path, "rb"), content_type=ctype,
                        as_attachment=False, filename=os.path.basename(path))


@require_role(*_VIEW_ROLES)
def serve_notice_specifications(request, pk):
    """Serve o caderno de encargos (de specifications_path). Exige sessão."""
    return _serve_notice_document(pk, "specifications_path", "Caderno de encargos não disponível.")


@require_role(*_VIEW_ROLES)
def serve_notice_program(request, pk):
    """Serve o programa de concurso (de program_path). Exige sessão."""
    return _serve_notice_document(pk, "program_path", "Programa de concurso não disponível.")
