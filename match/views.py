"""Endpoints da app match. As views são magras — delegam no NifMatchingService."""

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from common.session_access import allow_grants
from users.models import UserProfile
from users.permissions import require_role
from .services import (
    NifMatchingService, NifValidationError, NifServiceError, MissingClientDataError,
    promote_viewer_to_client,
)


def _payload(request) -> dict:
    """Lê o corpo do pedido como JSON; cai para request.POST se não for JSON."""
    if request.content_type == "application/json" and request.body:
        try:
            return json.loads(request.body)
        except json.JSONDecodeError:
            return {}
    return request.POST


@csrf_exempt
@require_http_methods(["POST"])
def evaluate_nif(request):
    """POST {"nif": "...", "cae"?, "region"?, "dimension"?, "entity_type"?,
    "email"?, "name"?, "job_title"?} -> matches ordenados.

    Os campos opcionais cae/region/dimension/entity_type servem para responder a um pedido
    de mais informações: quando o nif.pt não traz CAE ou localização, a resposta 422 diz
    o que falta; o cliente reenvia o mesmo pedido já com esses campos preenchidos.

    email/name/job_title: GATE de contacto, só para quem NÃO tem sessão. A procura corre
    sempre já na 1ª chamada (nunca espera pelo contacto), mas sem estes 3 campos os
    `matches` ficam retidos — a resposta é o mesmo 422 `needs_more_info` de cima, com
    missing_fields=[email, name, job_title]. O cliente mostra o pop-up e reenvia o mesmo
    NIF já com email/name/job_title preenchidos para receber os `matches`.

    O viewer (lead) só é registado quando o pedido vem SEM autenticação — é aí que faz
    sentido guardar quem consultou os apoios. Um utilizador autenticado (admin, client…)
    está a consultar, não a gerar lead: nesse caso `viewer_user_id` vem a None, email/
    name/job_title são ignorados, e os `matches` nunca ficam retidos.

    - 200: {company, nif, viewer_user_id (None se autenticado), matches[]}
    - 400: NIF em falta, inválido, inexistente ou contribuinte inativo
    - 422: faltam dados obrigatórios -> {error, needs_more_info, missing_fields[]}
    - 502: falha de configuração/comunicação com a API externa
    """
    data = _payload(request)
    nif = str(data.get("nif") or "").strip()
    if not nif:
        return JsonResponse({"error": "O campo 'nif' é obrigatório."}, status=400)

    overrides = {
        "cae": data.get("cae") or data.get("cae_codes"),
        "region": data.get("region"),
        "dimension": data.get("dimension"),
        "entity_type": data.get("entity_type"),
    }
    contact = {
        "email": data.get("email"),
        "name": data.get("name"),
        "job_title": data.get("job_title"),
    }
    try:
        result = NifMatchingService().evaluate(
            nif, overrides=overrides, contact=contact,
            # Só quem NÃO está autenticado gera um viewer (lead) e passa pelo gate de contacto.
            create_viewer=not request.user.is_authenticated,
        )
    except NifValidationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except MissingClientDataError as exc:
        return JsonResponse(
            {"error": str(exc), "needs_more_info": True, "missing_fields": exc.fields},
            status=422, json_dumps_params={"ensure_ascii": False},
        )
    except NifServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    if not request.user.is_authenticated:
        # Sem sessão: desbloqueia o detalhe SÓ dos avisos que este match devolveu (ver
        # common/session_access.py e avisos/views.grants_detail) — impede ver outros avisos
        # trocando o id na URL.
        allow_grants(request, (m.get("opportunity_id") for m in result.get("matches", [])))

    return JsonResponse(result, json_dumps_params={"ensure_ascii": False, "indent": 2})


@csrf_exempt
@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)
@require_http_methods(["POST"])
def promote_viewer(request, nif):
    """POST /match/promote/<nif>/ -> promove o viewer existente a client (is_active=True).

    Restrito a admin/comerciais: promover ATIVA a conta (is_active=True) — aberto,
    qualquer pessoa podia reativar contas desativadas.

    - 200: {user_id, nif, role, is_active, has_login}
    - 401/403: sem sessão / sem permissão
    - 404: não existe nenhum viewer/registo com esse NIF
    """
    result = promote_viewer_to_client(str(nif).strip())
    if result is None:
        return JsonResponse(
            {"error": "Não existe nenhum viewer/registo com esse NIF."}, status=404
        )
    return JsonResponse(result, json_dumps_params={"ensure_ascii": False})
