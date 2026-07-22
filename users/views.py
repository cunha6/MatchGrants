import json

from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from common.pagination import parse_pagination
from . import service
from .models import UserProfile
from .permissions import require_role, get_role

# All roles (any authenticated user)
_ALL_ROLES = (UserProfile.ADMIN, UserProfile.COMMERCIAL, UserProfile.COMPOSER, UserProfile.CLIENT)
# Roles that may only create 'client' users
_CLIENT_ONLY_CREATORS = (UserProfile.COMMERCIAL, UserProfile.COMPOSER)

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
    data = _payload(request)
    username = (data.get("username") or "").strip()

    cache_key = f"login_attempts:{username.lower()}"
    if username and cache.get(cache_key, 0) >= _LOGIN_MAX_ATTEMPTS:
        return JsonResponse(
            {"error": "Too many failed login attempts. Try again later."}, status=429
        )

    user = authenticate(request, username=username, password=data.get("password"))
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


@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL)
@require_http_methods(["GET"])
def users_all(request):
    """
    List users (paginated via ?page and ?page_size).
    - Commercial: only active clients; can filter every field except address.
    - Admin: filters by ?role= and ?active=true|false|all (default: active only) plus all fields.
    """
    try:
        page, page_size = parse_pagination(request)
        if get_role(request.user) == UserProfile.COMMERCIAL:
            allowed = set(_FILTER_FIELDS) - {"address"}
            filters = _build_filters(request, allowed)
            data = service.get_all_users(
                role=UserProfile.CLIENT, active=True, filters=filters,
                page=page, page_size=page_size,
            )
        else:  # admin — all filters
            filters = _build_filters(request, set(_FILTER_FIELDS))
            role_filter = request.GET.get("role") or None
            active_param = (request.GET.get("active") or "true").lower()
            active = None if active_param == "all" else (active_param != "false")
            data = service.get_all_users(
                role=role_filter, active=active, filters=filters,
                page=page, page_size=page_size,
            )
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
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
@require_role(UserProfile.ADMIN)
@require_http_methods(["POST"])
def users_activate(request, user_id):
    """Reactivate a user (is_active=True). Admin only."""
    ok = service.set_active(user_id, True)
    if ok is None:
        return JsonResponse({"error": "User not found"}, status=404)
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
    - admin: any user
    - commercial: clients only (plus themselves)
    - composer / client: themselves only
    """
    try:
        data = service.get_user_detail(user_id)
        if data is None:
            return JsonResponse({"error": "User not found"}, status=404)

        role = get_role(request.user)
        is_self = request.user.id == int(user_id)
        if role == UserProfile.ADMIN or is_self:
            allowed = True
        elif role == UserProfile.COMMERCIAL:
            allowed = data.get("role") == UserProfile.CLIENT
        else:
            allowed = False
        if not allowed:
            return JsonResponse(
                {"error": "You do not have permission to view this user."}, status=403
            )
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def users_create(request):
    """Create a user, validating the role according to who creates it:
    - admin: any role
    - commercial / composer: may only create 'client'
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

        # only admin keeps the requested role; others (commercial/composer) become client
        if requester_role != UserProfile.ADMIN:
            payload = {k: v for k, v in payload.items() if k != "role"}

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
    try:
        payload = _payload(request)

        requester_role = get_role(request.user)
        is_admin = (requester_role == UserProfile.ADMIN)

        if not is_admin and request.user.id != int(user_id):
            return JsonResponse(
                {"error": "You do not have permission to edit other users."},
                status=403
            )

        if not is_admin:
            # pass the requested role through, but only admin may change it
            payload = {k: v for k, v in payload.items() if k != "role"}

        data = service.update_user(user_id, payload)

        if data is None:
            return JsonResponse({"error": "User not found"}, status=404)

        return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_role(*_ALL_ROLES)
@require_http_methods(["POST"])
def users_change_password(request, user_id):
    """Change password. Each user changes their own (current validated); admin resets any."""
    try:
        payload = _payload(request)
        is_admin = get_role(request.user) == UserProfile.ADMIN
        # non-admins may only change their own password
        if not is_admin and request.user.id != user_id:
            return JsonResponse(
                {"error": "You do not have permission to change another user's password."},
                status=403,
            )
        ok = service.change_password(
            user_id,
            payload.get("password"),
            current_password=payload.get("current_password"),
            by_admin=is_admin,
        )
        if ok is None:
            return JsonResponse({"error": "User not found"}, status=404)
        # changed own password -> keep the current session valid
        if request.user.id == user_id:
            request.user.refresh_from_db()
            update_session_auth_hash(request, request.user)
        return JsonResponse({"message": "Password updated"})
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


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
