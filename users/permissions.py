"""Role-based authorization decorator for function-based views."""

from functools import wraps

from django.http import JsonResponse

from .models import UserProfile


def get_role(user) -> str | None:
    """Return the user's role (admin/commercial/composer/client) or None if not authenticated.

    A Django superuser is always treated as admin (bootstrap bypass).
    """
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return UserProfile.ADMIN
    profile = getattr(user, "profile", None)
    return profile.role if profile else None


def require_role(*roles):
    """
    Restrict the view to the given roles.
    401 if not authenticated, 403 if the role is not allowed.
    Usage: @require_role("admin", "commercial")
    """
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({"error": "Authentication required."}, status=401)
            if get_role(request.user) not in roles:
                return JsonResponse(
                    {"error": "You do not have permission to perform this action."}, status=403
                )
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
