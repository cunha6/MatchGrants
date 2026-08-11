"""User service layer — operates on django.contrib.auth User."""

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from common.ctt import ctt_lookup
from common.email_validation import email_error_label
from common.nuts import nuts_for
from common.pagination import paginate_queryset
from . import notifications
from .models import UserProfile

_VALID_ROLES = {
    UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC,
    UserProfile.CLIENT,
}

# Papéis INTERNOS (equipa). Uma conta destas identifica-se só por utilizador/nome/email/papel:
# os campos de entidade (NIF, CAE, morada, NUTS…) descrevem uma EMPRESA candidata e não se
# aplicam. Ver `_serialize` e `_apply_profile`.
_STAFF_ROLES = (
    UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC,
)

# Minimum password length (must be more than 8 characters). Continua a aplicar-se a quem
# DEFINE a password pelo link recebido por email (ver reset_password_with_token) — ninguém
# volta a passar uma password no corpo do pedido de criação/registo (ver create_user).
_PASSWORD_MIN_LENGTH = 8

# Entity fields accepted on create/update (role is handled separately by validation).
# city/county/region são normalmente DERIVADOS do postal_code (ver
# _fill_location_from_postal_code) — continuam aceites aqui para permitir uma correção
# manual (ex: um admin a editar o perfil) sem depender de reenviar o código postal.
_PROFILE_FIELDS = (
    "entity_type", "entity_size", "incorporation_date",
    "nif", "main_cae", "secondary_cae", "address", "postal_code",
    "city", "county", "region", "nuts_ii", "nuts_iii",
)


def get_all_users(role=None, active=True, filters=None, page=1, page_size=50,
                   exclude_superuser=False) -> dict:
    """
    List users, filtered by role, state and extra fields, with pagination.
    role: a single role string, or a list/tuple of roles (role__in).
    active=True -> only active; active=False -> only inactive; active=None -> all.
    filters: dict {orm_lookup: value} already built by the view
    (e.g. {"profile__main_cae__startswith": "62"}).
    exclude_superuser: esconde superusers do Django — só o admin os vê (get_role trata
    superuser como admin, mas o profile.role em si fica 'client' por omissão, por isso um
    superuser pode calhar dentro de um filtro por role sem esta exclusão explícita).
    Returns {total, page, page_size, num_pages, users}.
    """
    # select_related/prefetch_related evitam o N+1 do _serialize (poucas queries no total,
    # em vez de 1+N por página — matched_grants é M2M, select_related não chega para ele).
    users = User.objects.select_related("profile") \
        .prefetch_related("profile__matched_grants").order_by("id")
    if active is not None:
        users = users.filter(is_active=active)
    if role:
        if isinstance(role, (list, tuple, set)):
            users = users.filter(profile__role__in=role)
        else:
            users = users.filter(profile__role=role)
    if exclude_superuser:
        users = users.exclude(is_superuser=True)
    if filters:
        users = users.filter(**filters)

    return paginate_queryset(users, page, page_size, _serialize, "users")


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


def create_user(serialized) -> dict:
    """Cria o utilizador. Devolve-o serializado.

    NINGUÉM define a password na criação — nem quem se regista a si próprio (registo
    público), nem quem cria a conta de outrem (um admin/comercial autenticado). A conta
    nasce sempre SEM password utilizável e a pessoa recebe por email o link para a
    escolher (`request_password_reset`, o mesmo mecanismo de sempre — ver essa função para
    o porquê de a resposta nunca revelar se a conta já existia). Uma `password` que venha
    no pedido é ignorada em silêncio.
    """
    if not serialized.get("username"):
        raise ValueError("The 'username' field is required.")
    if not serialized.get("email"):
        raise ValueError("The 'email' field is required.")
    email_error = email_error_label(serialized.get("email"))
    if email_error:
        raise ValueError(email_error)

    _validate_required_data(serialized, is_update=False)
    _validate_profile_fields(serialized)

    username = serialized.get("username")
    if User.objects.filter(username=username).exists():
        raise ValueError("username already exists")

    email = serialized.get("email")
    if email and User.objects.filter(email=email).exists():
        raise ValueError("This email is already registered.")

    # Atómico: user + perfil criados juntos (nunca fica um user sem perfil aplicado).
    # O IntegrityError cobre a corrida entre o .exists() acima e o create (usernames
    # concorrentes) — vira 400 em vez de 500.
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username,
                # NENHUMA password é aceite na criação, de ninguém — ver docstring.
                # `create_user(password=None)` deixa a conta com password inutilizável.
                password=None,
                email=email,
                # `first_name` é o NOME da pessoa — um dos campos que identificam qualquer
                # conta, incluindo as internas (admin/comercial), que não têm mais nenhum.
                first_name=serialized.get("first_name") or "",
            )
            # The signal already creates the profile (client); apply role + entity fields
            _apply_profile(user, serialized)
    except IntegrityError:
        raise ValueError("username already exists")

    # Fora da transação: um email não se desfaz com um rollback, por isso só se envia depois
    # de a conta estar mesmo gravada. Best-effort — ver notifications.
    request_password_reset(user.email)
    return _serialize(user)


def update_user(user_id: int, serialized) -> dict | None:
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return None

    profile = getattr(user, "profile", None)
    current_role = profile.role if profile else UserProfile.CLIENT

    _validate_required_data(serialized, current_role=current_role, is_update=True)
    _validate_profile_fields(serialized, current_role=current_role)

    new_username = serialized.get("username")
    if new_username and User.objects.filter(username=new_username).exclude(pk=user_id).exists():
        raise ValueError("username already exists")

    new_email = serialized.get("email")
    if new_email and User.objects.filter(email=new_email).exclude(pk=user_id).exists():
        raise ValueError("email already exists")

    for field in ("username", "email", "first_name"):
        value = serialized.get(field)
        if value is not None:
            setattr(user, field, value)

    # Uma `password` no corpo é IGNORADA, de propósito: a password só se define pelo link
    # enviado por email (ver users.views.users_change_password e reset_password_with_token).
    # Enquanto isto aqui a aceitava, um comercial punha a password que quisesse na conta de
    # qualquer client/viewer que gere — e entrava nela —, contornando por completo a regra de
    # que só quem controla a caixa de correio a consegue definir (ver ADR-11 e ADR-16).

    try:
        with transaction.atomic():
            user.save()
            _apply_profile(user, serialized)
    except IntegrityError:
        raise ValueError("username already exists")
    return _serialize(user)


def delete_user(user_id: int) -> bool:
    """Soft-delete: deactivates the user (is_active=False) instead of deleting it."""
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return False
    user.is_active = False
    user.save(update_fields=["is_active"])
    return True


def request_password_reset(email: str) -> None:
    """Envia (best-effort) um email de reset/definição de password a quem tiver `email`.
    NUNCA revela se a conta existe — a view responde sempre a mesma mensagem genérica; esta
    função não devolve nada que distinga os casos.

    Funciona mesmo para quem NUNCA teve password (ex: uma conta criada por um admin, ou um
    viewer já promovido): serve também como forma de DEFINIR a primeira password, não só de
    repor uma esquecida — daí não haver aqui um filtro por has_usable_password().

    Só chega a contas ATIVAS. Contas inativas ficam de fora porque um viewer nasce de um
    pedido ANÓNIMO e é esse pedido que lhe escreve o email (ver
    match.leads.create_or_update_viewer): sem este filtro, quem soubesse o NIF público de
    uma empresa apontava a conta ao seu próprio email, recebia aqui o link e deixava uma
    password sua à espera da promoção. Uma conta inativa recebe as credenciais no momento em
    que é ativada, não antes (ver match.leads.promote_viewer_to_client).
    """
    email = (email or "").strip()
    if not email:
        return
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        return
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    reset_url = f"{settings.FRONTEND_URL}/reset-password?uid={uidb64}&token={token}"
    notifications.send_password_reset_email(user.email, reset_url)


def reset_password_with_token(uidb64: str, token: str, new_password: str) -> bool:
    """Valida uid+token (django.contrib.auth.tokens — assinado, ligado ao hash da password
    atual, expira em PASSWORD_RESET_TIMEOUT) e, se válido, aplica `new_password`.

    Devolve True em sucesso. Levanta ValueError (→ 400 na view) em qualquer falha — link
    inválido/expirado ou password que não cumpre a política — sem distinguir qual, para não
    dar pistas sobre contas existentes a quem só tenha um uid adivinhado.
    """
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)))
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        raise ValueError("Link de redefinição inválido ou expirado.")
    if not default_token_generator.check_token(user, token):
        raise ValueError("Link de redefinição inválido ou expirado.")
    _validate_password(new_password)
    user.set_password(new_password)
    user.save()
    return True


def _validate_password(password) -> None:
    """Password policy: mais de 8 caracteres + AUTH_PASSWORD_VALIDATORS do Django
    (rejeita passwords demasiado comuns, só numéricas, etc.)."""
    if password is None or len(password) <= _PASSWORD_MIN_LENGTH:
        raise ValueError("Password must have more than 8 characters.")
    try:
        validate_password(password)
    except ValidationError as e:
        raise ValueError("; ".join(e.messages))


def _normalize_secondary_cae(value):
    """Normalize secondary_cae into a list of 5-char CAE codes.

    Accepts a list of codes, or a single string (wrapped into a list).
    None/empty becomes an empty list. Raises ValueError if invalid.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not (isinstance(cae_code, str) and len(cae_code) == 5) for cae_code in value):
        raise ValueError("secondary_cae must be a list of 5-character CAE codes.")
    return value


# Primeiro dígito do NIF que identifica uma pessoa SINGULAR (1, 2) ou um NIF reservado (3,
# não atribuído atualmente) — nunca uma empresa/entidade coletiva. O registo é só para
# entidades, por isso um NIF pessoal é rejeitado aqui.
_PERSONAL_NIF_PREFIXES = ("1", "2", "3")


def _is_valid_company_nif(nif: str) -> bool:
    """9 dígitos, dígito de controlo válido (algoritmo oficial mod 11) e NÃO é NIF de pessoa
    singular (ver `_PERSONAL_NIF_PREFIXES`). Não confirma que o NIF EXISTE (isso é o nif.pt,
    ver match/services.py) — só que tem uma forma válida de NIF de empresa/entidade."""
    nif = (nif or "").strip()
    if not (nif.isdigit() and len(nif) == 9):
        return False
    if nif[0] in _PERSONAL_NIF_PREFIXES:
        return False
    checksum = sum(int(digit) * weight for digit, weight in zip(nif[:8], range(9, 1, -1)))
    remainder = checksum % 11
    check_digit = 0 if remainder < 2 else 11 - remainder
    return check_digit == int(nif[8])


def _fill_location_from_postal_code(defaults: dict) -> None:
    """Deriva city/county/region a partir do código postal (API CTT + NUTS) — mesma lógica
    de match/company_metadata.py:_location (CTT→NUTS), aqui aplicada ao código postal que a
    própria pessoa introduz no registo, em vez do vindo do nif.pt. (O CTT devolve também o
    distrito, mas `UserProfile` não tem esse campo — só city/county/region.)

    Só atua quando o código postal está de facto a ser definido NESTE pedido — não
    reprocessa código postal já gravado em pedidos que não o tocam (ver `_apply_profile`).
    Degrada em silêncio (sem CTT_KEY, código inválido, ou falha de rede) — mesma filosofia
    do resto do projeto: um problema transitório nosso não pode bloquear o registo.
    """
    postal_code = defaults.get("postal_code")
    if not postal_code:
        return
    city, county, _district = ctt_lookup(postal_code)
    if city:
        defaults["city"] = city
    if county:
        defaults["county"] = county
    _, _, region_old = nuts_for(city, county) if (city or county) else (None, None, None)
    if region_old:
        defaults["region"] = region_old


def _validate_profile_fields(serialized, current_role=None) -> None:
    """
    Validate profile fields (max_length, choices, NIF) via full_clean on a transient
    instance — before persisting. Converts ValidationError -> ValueError (-> 400).

    Os campos de ENTIDADE (NIF, CAE, morada…) só são validados quando o papel EFETIVO não
    é interno (`_STAFF_ROLES`) — um admin/comercial que os envie por hábito não pode ver a
    criação/atualização falhar por causa deles: `_apply_profile` já os ignora em silêncio
    para esse papel, a validação tem de ser coerente com isso.
    """
    # secondary_cae is a JSON list, validated separately
    if "secondary_cae" in serialized:
        _normalize_secondary_cae(serialized["secondary_cae"])

    if "role" in serialized and serialized["role"] not in _VALID_ROLES:
        raise ValueError("Invalid role provided.")

    effective_role = (serialized["role"] if serialized.get("role") in _VALID_ROLES
                      else (current_role or UserProfile.CLIENT))
    if effective_role in _STAFF_ROLES:
        return

    if serialized.get("nif") and not _is_valid_company_nif(serialized["nif"]):
        raise ValueError(
            "NIF inválido — tem de ser um NIF de empresa/entidade (não pessoal), "
            "com 9 dígitos e dígito de controlo correto."
        )

    fields = {"role": effective_role}
    for field in _PROFILE_FIELDS:
        if field == "secondary_cae":
            continue
        if field in serialized and serialized[field] is not None:
            fields[field] = serialized[field]

    probe = UserProfile(**fields)
    try:
        probe.full_clean(exclude=["user"])
    except ValidationError as e:
        msg = "; ".join(f"{f}: {', '.join(errs)}" for f, errs in e.message_dict.items())
        raise ValueError(msg)


def _apply_profile(user: User, serialized) -> None:
    """Atualiza o perfil (papel + campos de entidade) a partir do pedido.

    Num papel INTERNO (admin/comercial) os campos de entidade são IGNORADOS em silêncio: não
    descrevem a pessoa, descrevem uma empresa candidata. Ignorar em vez de rejeitar evita
    partir clientes que ainda os enviem por hábito — ver `_STAFF_ROLES`.
    """
    defaults = {}
    if serialized.get("role") in _VALID_ROLES:
        defaults["role"] = serialized["role"]

    existing_profile = getattr(user, "profile", None)
    effective_role = (defaults.get("role")
                      or (existing_profile.role if existing_profile else UserProfile.CLIENT))

    if effective_role not in _STAFF_ROLES:
        for field in _PROFILE_FIELDS:
            if field in serialized:
                if field == "secondary_cae":
                    defaults[field] = _normalize_secondary_cae(serialized[field])
                else:
                    defaults[field] = serialized[field]
        _fill_location_from_postal_code(defaults)
    if defaults:
        profile, _ = UserProfile.objects.update_or_create(user=user, defaults=defaults)
        user.profile = profile  # refresh the reverse-relation cache (otherwise stale)
        _fill_entity_size_from_sqlite(profile)


def _fill_entity_size_from_sqlite(profile) -> None:
    """Se a dimensão (entity_size) não foi indicada, vai buscá-la ao NifCompany (SQLite) pelo
    NIF. Só preenche quando está em falta — um valor indicado manualmente prevalece.
    Tolerante: se a BD 'nif' não existir/estiver por carregar, não faz nada."""
    if profile.entity_size or not profile.nif:
        return
    try:
        from match.models import NifCompany
        company = NifCompany.objects.filter(nif=profile.nif).first()
    except Exception:
        company = None
    if company and company.dimension:
        profile.entity_size = company.dimension
        profile.save(update_fields=["entity_size"])


def _serialize(user: User) -> dict:
    """Utilizador em JSON. Os campos de ENTIDADE (NIF, CAE, morada, NUTS…) só saem para
    `client`/`viewer`.

    Um admin ou comercial é uma pessoa da equipa, não uma empresa candidata: esses campos
    nunca são preenchidos para eles, e devolvê-los a null só fazia o front-end desenhar um
    formulário de empresa numa conta interna. Ver `_STAFF_ROLES`.
    """
    profile = getattr(user, "profile", None)
    serialized = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "role": profile.role if profile else None,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "date_joined": user.date_joined.isoformat() if user.date_joined else None,
    }
    if profile and profile.role not in _STAFF_ROLES:
        serialized.update({
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
            "postal_code": profile.postal_code,
            "city": profile.city,
            "county": profile.county,
            "region": profile.region,
            "nuts_ii": profile.nuts_ii,
            "nuts_iii": profile.nuts_iii,
            "job_title": profile.job_title,
            "matched_grants": [
                {"id": grant.id, "grant_code": grant.grant_code, "title": grant.title}
                for grant in profile.matched_grants.all()
            ],
        })
    return serialized


def _validate_required_data(serialized, current_role=None, is_update=False):
    """Validate the required fields according to the user's role."""
    role = serialized.get("role", current_role or UserProfile.CLIENT)

    if role in (UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC):
        for field in ("username", "email"):
            # On update only validate the fields actually sent (supports partial updates)
            if not is_update or field in serialized:
                if not serialized.get(field):
                    raise ValueError(f"The '{field}' field is required for the '{role}' profile.")

    elif role == UserProfile.CLIENT:
        # postal_code substitui region: a região é DERIVADA do código postal (ver
        # _fill_location_from_postal_code), não pedida diretamente a quem se regista.
        client_fields = ["entity_type", "entity_size", "nif", "main_cae", "address", "postal_code"]
        for field in client_fields:
            # On update only validate if the key was sent (supports partial updates)
            if not is_update or field in serialized:
                if not serialized.get(field):
                    raise ValueError(f"The '{field}' field is required")
