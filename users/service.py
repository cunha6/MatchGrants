"""User service layer — operates on django.contrib.auth User."""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from .models import UserProfile

_VALID_ROLES = {UserProfile.ADMIN, UserProfile.COMMERCIAL, UserProfile.COMPOSER, UserProfile.CLIENT}

# Minimum password length (must be more than 8 characters).
_PASSWORD_MIN_LENGTH = 8

# Entity fields accepted on create/update (role is handled separately by validation)
_PROFILE_FIELDS = (
    "entity_type", "entity_size", "incorporation_date",
    "nif", "main_cae", "secondary_cae", "address", "region", "nuts_ii", "nuts_iii",
)


def get_all_users(role=None, active=True, filters=None, page=1, page_size=50) -> dict:
    """
    List users, filtered by role, state and extra fields, with pagination.
    active=True -> only active; active=False -> only inactive; active=None -> all.
    filters: dict {orm_lookup: value} already built by the view
    (e.g. {"profile__main_cae__startswith": "62"}).
    Returns {total, page, page_size, num_pages, users}.
    """
    users = User.objects.all().order_by("id")
    if active is not None:
        users = users.filter(is_active=active)
    if role:
        users = users.filter(profile__role=role)
    if filters:
        users = users.filter(**filters)

    total = users.count()
    paginator = Paginator(users, page_size)
    page_obj = paginator.get_page(page)
    return {
        "total": total,
        "page": page_obj.number,
        "page_size": page_size,
        "num_pages": paginator.num_pages,
        "users": [_serialize(u) for u in page_obj],
    }


def set_active(user_id: int, active: bool) -> bool | None:
    """Activate/deactivate a user. Returns None if it does not exist."""
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return None
    user.is_active = active
    user.save(update_fields=["is_active"])
    return True


def get_user_detail(user_id: int) -> dict | None:
    user = User.objects.filter(pk=user_id).first()
    return _serialize(user) if user else None


def create_user(data) -> dict:

    if not data.get("username"):
        raise ValueError("The 'username' field is required.")
    if not data.get("email"):
        raise ValueError("The 'email' field is required.")
    if not data.get("password"):
        raise ValueError("The 'password' field is required for new users.")
    _validate_password(data.get("password"))

    _validate_required_data(data, is_update=False)
    _validate_profile_fields(data)

    username = data.get("username")
    if User.objects.filter(username=username).exists():
        raise ValueError("username already exists")

    email = data.get("email")
    if email and User.objects.filter(email=email).exists():
        raise ValueError("This email is already registered.")

    user = User.objects.create_user(
        username=username,
        email=data.get("email"),
        password=data.get("password"),
    )
    # The signal already creates the profile (client); apply role + entity fields
    _apply_profile(user, data)
    return _serialize(user)


def update_user(user_id: int, data) -> dict | None:
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return None

    profile = getattr(user, "profile", None)
    current_role = profile.role if profile else UserProfile.CLIENT

    _validate_required_data(data, current_role=current_role, is_update=True)
    _validate_profile_fields(data)
    if data.get("password"):
        _validate_password(data["password"])

    new_username = data.get("username")
    if new_username and User.objects.filter(username=new_username).exclude(pk=user_id).exists():
        raise ValueError("username already exists")

    new_email = data.get("email")
    if new_email and User.objects.filter(email=new_email).exclude(pk=user_id).exists():
        raise ValueError("email already exists")

    for field in ("username", "email"):
        value = data.get(field)
        if value is not None:
            setattr(user, field, value)

    if data.get("password"):
        user.set_password(data["password"])

    user.save()
    _apply_profile(user, data)
    return _serialize(user)


def delete_user(user_id: int) -> bool:
    """Soft-delete: deactivates the user (is_active=False) instead of deleting it."""
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return False
    user.is_active = False
    user.save(update_fields=["is_active"])
    return True


def change_password(user_id: int, new_password, current_password=None, by_admin: bool = False) -> bool | None:
    """
    Change the password (hashed). Returns None if the user does not exist.
    by_admin=True -> direct reset. Otherwise the current password is validated.
    """
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return None
    if not new_password:
        raise ValueError("password is required")
    _validate_password(new_password)
    if not by_admin and not user.check_password(current_password or ""):
        raise ValueError("current password is incorrect")
    user.set_password(new_password)
    user.save()
    return True


def _validate_password(password) -> None:
    """Password policy. For now: must have more than 8 characters."""
    if password is None or len(password) <= _PASSWORD_MIN_LENGTH:
        raise ValueError("Password must have more than 8 characters.")


def _normalize_secondary_cae(value):
    """Normalize secondary_cae into a list of 5-char CAE codes.

    Accepts a list of codes, or a single string (wrapped into a list).
    None/empty becomes an empty list. Raises ValueError if invalid.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not (isinstance(c, str) and len(c) == 5) for c in value):
        raise ValueError("secondary_cae must be a list of 5-character CAE codes.")
    return value


def _validate_profile_fields(data) -> None:
    """
    Validate profile fields (max_length, choices) via full_clean on a transient
    instance — before persisting. Converts ValidationError -> ValueError (-> 400).
    """
    # secondary_cae is a JSON list, validated separately
    if "secondary_cae" in data:
        _normalize_secondary_cae(data["secondary_cae"])

    if "role" in data and data["role"] not in _VALID_ROLES:
        raise ValueError("Invalid role provided.")
    
    fields = {}
    if data.get("role") in _VALID_ROLES:
        fields["role"] = data["role"]
        
    for field in _PROFILE_FIELDS:
        if field == "secondary_cae":
            continue
        if field in data and data[field] is not None:
            fields[field] = data[field]
            
    if not fields:
        return

    probe = UserProfile(**fields)
    try:
        probe.full_clean(exclude=["user"])
    except ValidationError as e:
        msg = "; ".join(f"{f}: {', '.join(errs)}" for f, errs in e.message_dict.items())
        raise ValueError(msg)


def _apply_profile(user: User, data) -> None:
    """Update the user's profile (role + entity fields) from `data`."""
    defaults = {}
    if data.get("role") in _VALID_ROLES:
        defaults["role"] = data["role"]
    for field in _PROFILE_FIELDS:
        if field in data:
            if field == "secondary_cae":
                defaults[field] = _normalize_secondary_cae(data[field])
            else:
                defaults[field] = data[field]
    if defaults:
        profile, _ = UserProfile.objects.update_or_create(user=user, defaults=defaults)
        user.profile = profile  # refresh the reverse-relation cache (otherwise stale)


def _serialize(user: User) -> dict:
    profile = getattr(user, "profile", None)
    data = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": profile.role if profile else None,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "date_joined": user.date_joined.isoformat() if user.date_joined else None,
    }
    if profile:
        data.update({
            "entity_type": profile.entity_type,
            "entity_size": profile.entity_size,
            "incorporation_date": (
                profile.incorporation_date.isoformat()
                if hasattr(profile.incorporation_date, "isoformat")
                else profile.incorporation_date
            ),
            "nif": profile.nif,
            "main_cae": profile.main_cae,
            "secondary_cae": profile.secondary_cae,
            "address": profile.address,
            "region": profile.region,
            "nuts_ii": profile.nuts_ii,
            "nuts_iii": profile.nuts_iii,
        })
    return data


def _validate_required_data(data, current_role=None, is_update=False):
    """Validate the required fields according to the user's role."""
    role = data.get("role", current_role or UserProfile.CLIENT)

    if role in (UserProfile.ADMIN, UserProfile.COMMERCIAL, UserProfile.COMPOSER):
        required_fields = ["username", "email"]
        if not is_update:
            required_fields.append("password")

        for field in required_fields:
            # On update only validate the fields actually sent (supports partial updates)
            if not is_update or field in data:
                if not data.get(field):
                    raise ValueError(f"The '{field}' field is required for the '{role}' profile.")

    elif role == UserProfile.CLIENT:
        client_fields = ["entity_type", "entity_size", "nif", "main_cae", "address", "region"]
        for field in client_fields:
            # On update only validate if the key was sent (supports partial updates)
            if not is_update or field in data:
                if not data.get(field):
                    raise ValueError(f"The '{field}' field is required")

        # Special handling for booleans (False would fail the 'not data.get()' check)
        for field in ["nuts_ii", "nuts_iii"]:
            if not is_update or field in data:
                if data.get(field) is None:
                    raise ValueError(f"The '{field}' field is required")
