import json

from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from common.pagination import parse_pagination
from . import service
from .models import UserProfile
from .permissions import require_role, get_role

# All roles (any authenticated user)
_ALL_ROLES = (UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC,
              UserProfile.CLIENT)
# The two comerciais: gerem utilizadores do tipo viewer/client (ver/editar), cada um no seu
# domínio de dados (avisos vs. avisos+anúncios) — só o ADMIN tem mais do que isto.
_COMMERCIAL_ROLES = (UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)
# Roles de utilizador que os comerciais podem ver/gerir (nunca admin, nunca outro comercial).
_COMMERCIAL_MANAGED_ROLES = (UserProfile.VIEWER, UserProfile.CLIENT)
# Roles that may only create 'client' users
_CLIENT_ONLY_CREATORS = _COMMERCIAL_ROLES


def _can_manage(role: str | None, target_role: str | None, is_self: bool,
                 is_superuser: bool = False) -> bool:
    """Pode `role` gerir (ver/editar) um utilizador com `target_role`?
    Admin e o próprio utilizador: sempre. Um comercial: só viewer/client, nunca um superuser."""
    if is_self:
        return True
    if role == UserProfile.ADMIN:
        return True
    if role in _COMMERCIAL_ROLES:
        return target_role in _COMMERCIAL_MANAGED_ROLES and not is_superuser
    return False

# Login brute-force throttle (per username).
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCK_SECONDS = 300

# Filterable fields on the list -> ORM lookup.
# main_cae uses a prefix match (first characters of the CAE are enough).
# secondary_cae is a JSON list -> exact membership (the list contains the code).
_FILTER_FIELDS = {
    "entity_type": "profile__entity_type",
    "entity_size": "profile__entity_size",
    "nif": "profile__nif",
    "main_cae": "profile__main_cae__startswith",
    "secondary_cae": "profile__secondary_cae__contains",
    "region": "profile__region__icontains",
    "address": "profile__address__icontains",
    "incorporation_date": "profile__incorporation_date",
    "username": "username__icontains",
    "email": "email__icontains",
    "nuts_ii": "profile__nuts_ii",
    "nuts_iii": "profile__nuts_iii",
}
_BOOL_FILTERS = {"nuts_ii", "nuts_iii"}


def _build_filters(request, allowed_fields) -> dict:
    """Build the ORM filter dict from the query string, only for the allowed fields."""
    filters = {}
    for field in allowed_fields:
        raw = request.GET.get(field)
        if raw in (None, ""):
            continue
        if field in _BOOL_FILTERS:
            filters[_FILTER_FIELDS[field]] = raw.lower() in ("true", "1", "yes")
        else:
            filters[_FILTER_FIELDS[field]] = raw
    return filters


def _payload(request):
    """Read the request body as JSON; fall back to request.POST if not JSON."""
    if request.content_type == "application/json" and request.body:
        try:
            return json.loads(request.body)
        except json.JSONDecodeError:
            return {}
    return request.POST


@csrf_exempt
@require_http_methods(["POST"])
def login_view(request):
    """POST {username, password} -> start a session (sessionid cookie)."""
    payload = _payload(request)
    username = (payload.get("username") or "").strip()

    cache_key = f"login_attempts:{username.lower()}"
    if username and cache.get(cache_key, 0) >= _LOGIN_MAX_ATTEMPTS:
        return JsonResponse(
            {"error": "Too many failed login attempts. Try again later."}, status=429
        )

    user = authenticate(request, username=username, password=payload.get("password"))
    if user is None:
        if username:
            cache.set(cache_key, cache.get(cache_key, 0) + 1, _LOGIN_LOCK_SECONDS)
        return JsonResponse({"error": "Invalid credentials"}, status=401)

    cache.delete(cache_key)  # reset throttle on success
    login(request, user)
    return JsonResponse(service.get_user_detail(user.id), json_dumps_params={"ensure_ascii": False})


@csrf_exempt
@require_http_methods(["POST"])
def logout_view(request):
    """End the current session."""
    logout(request)
    return JsonResponse({"message": "Logged out"})


# Throttle do pedido de reset (por email) — evita encher a caixa de correio de alguém.
_RESET_MAX_ATTEMPTS = 3
_RESET_LOCK_SECONDS = 900  # 15 min


@csrf_exempt
@require_http_methods(["POST"])
def password_reset_request_view(request):
    """POST {email} -> pede o email de reset/definição de password. Resposta SEMPRE igual
    (200, mesma mensagem) quer o email exista ou não — nunca revela se uma conta existe.
    Funciona mesmo para quem nunca teve password (viewers). Ver service.request_password_reset."""
    payload = _payload(request)
    email = (payload.get("email") or "").strip()
    message = "Se existir uma conta com esse email, foi enviado um link de redefinição."

    cache_key = f"password_reset_attempts:{email.lower()}"
    if email and cache.get(cache_key, 0) >= _RESET_MAX_ATTEMPTS:
        return JsonResponse({"message": message})  # mesma resposta — não denuncia o throttle

    if email:
        cache.set(cache_key, cache.get(cache_key, 0) + 1, _RESET_LOCK_SECONDS)
        service.request_password_reset(email)
    return JsonResponse({"message": message})


@csrf_exempt
@require_http_methods(["POST"])
def password_reset_confirm_view(request):
    """POST {uid, token, password} -> define a nova password se o link for válido.
    200 em sucesso; 400 se o link for inválido/expirado ou a password não cumprir a
    política (mesma mensagem para os dois — ver service.reset_password_with_token)."""
    payload = _payload(request)
    try:
        service.reset_password_with_token(
            payload.get("uid") or "", payload.get("token") or "", payload.get("password"),
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"message": "Password redefinida com sucesso."})


@require_role(UserProfile.ADMIN, *_COMMERCIAL_ROLES)
@require_http_methods(["GET"])
def users_all(request):
    """
    List users (paginated via ?page and ?page_size).
    - Commercial (grants ou public): utilizadores viewer e client — os viewers (leads do match
      sem login) ficam sempre is_active=False, por isso sem filtro de estado; nunca superusers;
      pode filtrar todos os campos exceto address.
    - Admin: filters by ?role= and ?active=true|false|all (default: active only) plus all
      fields; é o único que vê superusers e outros admins/comerciais.
    """
    try:
        page, page_size = parse_pagination(request)
        if get_role(request.user) in _COMMERCIAL_ROLES:
            allowed = set(_FILTER_FIELDS) - {"address"}
            filters = _build_filters(request, allowed)
            payload = service.get_all_users(
                role=_COMMERCIAL_MANAGED_ROLES, active=None, filters=filters,
                page=page, page_size=page_size, exclude_superuser=True,
            )
        else:  # admin — all filters
            filters = _build_filters(request, set(_FILTER_FIELDS))
            role_filter = request.GET.get("role") or None
            active_param = (request.GET.get("active") or "true").lower()
            active = None if active_param == "all" else (active_param != "false")
            payload = service.get_all_users(
                role=role_filter, active=active, filters=filters,
                page=page, page_size=page_size,
            )
        return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_role(*_ALL_ROLES)
@require_http_methods(["GET"])
def users_me(request):
    """Return the authenticated user's profile."""
    return JsonResponse(
        service.get_user_detail(request.user.id),
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


@csrf_exempt
@require_role(UserProfile.ADMIN, *_COMMERCIAL_ROLES)
@require_http_methods(["POST"])
def users_activate(request, user_id):
    """Reactivate a user (is_active=True). Admin: qualquer um. Comercial: só viewer/client
    (nunca outro admin/comercial/superuser) — mesma regra do users_detail/update."""
    target = service.get_user_detail(user_id)
    if target is None:
        return JsonResponse({"error": "User not found"}, status=404)

    role = get_role(request.user)
    if not _can_manage(role, target.get("role"), request.user.id == int(user_id),
                        target.get("is_superuser")):
        return JsonResponse(
            {"error": "You do not have permission to activate this user."}, status=403
        )

    service.set_active(user_id, True)
    return JsonResponse(
        service.get_user_detail(user_id),
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


@csrf_exempt
def user_by_id(request, user_id):
    """Router for /users/<id>/ by method: GET -> detail, DELETE -> delete."""
    if request.method == "DELETE":
        return users_delete(request, user_id)
    return users_detail(request, user_id)


@require_role(*_ALL_ROLES)
@require_http_methods(["GET"])
def users_detail(request, user_id):
    """
    User detail with role-based access:
    - admin: any user (só ele vê superusers)
    - commercial (grants ou public): viewer/client apenas (plus themselves), nunca superusers
    - client: themselves only
    """
    try:
        payload = service.get_user_detail(user_id)
        if payload is None:
            return JsonResponse({"error": "User not found"}, status=404)

        role = get_role(request.user)
        is_self = request.user.id == int(user_id)
        if not _can_manage(role, payload.get("role"), is_self, payload.get("is_superuser")):
            return JsonResponse(
                {"error": "You do not have permission to view this user."}, status=403
            )
        return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def users_create(request):
    """Create a user, validating the role according to who creates it:
    - admin: any role
    - commercial (grants ou public): may only create 'client'
    - client: may not create users
    """
    try:
        payload = _payload(request)
        requested_role = payload.get("role")
        requester_role = get_role(request.user)  # None if not authenticated

        if requester_role == UserProfile.CLIENT:
            return JsonResponse(
                {"error": "You do not have permission to create users."}, status=403
            )
        if requester_role in _CLIENT_ONLY_CREATORS and requested_role and requested_role != UserProfile.CLIENT:
            return JsonResponse(
                {"error": "You can only create 'client' users."}, status=403
            )

        # only admin keeps the requested role; others (the commercial roles) become client
        if requester_role != UserProfile.ADMIN:
            payload = {k: v for k, v in payload.items() if k != "role"}

        # Ninguém define a password na criação (admin/comercial a criar de outrem, ou o
        # próprio a registar-se) — a conta nasce sem password utilizável e a pessoa recebe
        # por email o link para a escolher (ver service.create_user).
        result = service.create_user(payload)
        return JsonResponse(result, json_dumps_params={"ensure_ascii": False, "indent": 2}, status=201)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_role(*_ALL_ROLES)
@require_http_methods(["PUT"])
def users_update(request, user_id):
    """- admin: qualquer utilizador, qualquer campo (incl. role).
    - commercial (grants ou public): qualquer utilizador viewer/client, exceto o próprio role.
    - client: só o próprio perfil, exceto o próprio role."""
    try:
        payload = _payload(request)

        requester_role = get_role(request.user)
        is_admin = (requester_role == UserProfile.ADMIN)
        is_self = request.user.id == int(user_id)

        target = service.get_user_detail(user_id)
        if target is None:
            return JsonResponse({"error": "User not found"}, status=404)
        if not _can_manage(requester_role, target.get("role"), is_self, target.get("is_superuser")):
            return JsonResponse(
                {"error": "You do not have permission to edit other users."},
                status=403
            )

        if not is_admin:
            # pass the requested role through, but only admin may change it
            payload = {k: v for k, v in payload.items() if k != "role"}

        payload = service.update_user(user_id, payload)

        if payload is None:
            return JsonResponse({"error": "User not found"}, status=404)

        return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_role(*_ALL_ROLES)
@require_http_methods(["POST"])
def users_change_password(request, user_id):
    """Envia o email para a pessoa DEFINIR ela própria a nova password.

    Ninguém — nem o próprio, nem um admin — escreve aqui uma password: o pedido só dispara o
    link de redefinição para o email JÁ REGISTADO na conta. Assim a password nunca passa pelo
    corpo do pedido nem pelas mãos de quem gere a conta, e só quem controla a caixa de correio
    a consegue definir. O link é o mesmo de /users/password-reset/ (assinado, ligado ao hash
    atual, expira em 3 dias).

    O corpo do pedido é ignorado — `password`/`current_password` deixaram de ter efeito.
    """
    role = get_role(request.user)
    is_self = request.user.id == user_id

    target = service.get_user_detail(user_id)
    if target is None:
        return JsonResponse({"error": "User not found"}, status=404)
    if not _can_manage(role, target.get("role"), is_self, target.get("is_superuser")):
        return JsonResponse(
            {"error": "You do not have permission to change another user's password."},
            status=403,
        )

    # Sem email não há para onde enviar; e uma conta inativa não recebe o link (ver
    # service.request_password_reset). Quem gere a conta já vê os dois estados no detalhe,
    # por isso dizê-lo aqui não revela nada de novo — evita só um silêncio confuso.
    if not target.get("email"):
        return JsonResponse(
            {"error": "Esta conta não tem email registado — não há para onde enviar o link."},
            status=400,
        )
    if not target.get("is_active"):
        return JsonResponse(
            {"error": "Conta inativa: ative-a primeiro para poder definir a password."},
            status=400,
        )

    service.request_password_reset(target["email"])
    return JsonResponse(
        {"message": "Enviámos um email com o link para definir a nova password."})


@csrf_exempt
@require_role(UserProfile.ADMIN)
@require_http_methods(["DELETE"])
def users_delete(request, user_id):
    """Soft-delete de um utilizador (is_active=False). Admin only — o decorator garante-o."""
    try:
        success = service.delete_user(user_id)
        if not success:
            return JsonResponse({"error": "User not found"}, status=404)
        return HttpResponse(status=204)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
