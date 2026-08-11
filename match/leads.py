"""Ciclo de vida do lead (viewer) gerado por um match SEM login.

Um `viewer` é a conta mínima que regista quem consultou os apoios: nasce de um pedido
ANÓNIMO, fica inativa e sem password, e só passa a `client` quando um comercial a promove.

Está separado do motor de matching (match/services.py) porque é uma responsabilidade
diferente — persistência do lead, não cálculo de elegibilidade — e porque é aqui que vivem
as guardas de segurança da conta, que valem por si:

- os dados do nif.pt atualizam qualquer perfil (é informação pública, autoritativa);
- o CONTACTO (email/nome/função) só entra em contas que ainda são `viewer`, e só preenche
  campos vazios — nunca substitui um contacto já registado;
- promover uma conta INVALIDA qualquer password definida na janela de viewer.

As três seguram a mesma cadeia: o NIF de uma empresa é público, e o email é o seletor do
/users/password-reset/. Ver users/service.py:request_password_reset.
"""

from django.contrib.auth.models import User
from django.db import transaction

from users.models import UserProfile
from . import notifications


def _get_or_create_lead_account(nif: str) -> tuple[User, UserProfile, bool]:
    """(user, profile, is_new) para o NIF — idempotente, nunca duplica.

    Procura primeiro pelo PERFIL (o NIF é a chave natural do lead). Só quando não existe é
    que cria a conta: inativa e sem password utilizável, porque um viewer é apenas um registo
    de acesso ao match, não uma conta com que se possa entrar.
    """
    profile = UserProfile.objects.filter(nif=nif).select_related("user").first()
    if profile:
        return profile.user, profile, False

    # get_or_create tolera um User pré-existente com username=NIF (ex: perfil ainda sem nif)
    # — sem isto a unicidade do username rebentava com IntegrityError.
    user, is_new = User.objects.get_or_create(username=nif, defaults={"is_active": False})
    if is_new:
        user.set_unusable_password()
        user.save()
    # O signal post_save cria o perfil (role=client); get_or_create cobre contas antigas
    # que possam não o ter.
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return user, profile, True


def _apply_company_data(profile: UserProfile, metadata: dict) -> None:
    """Escreve no perfil os dados DIRETOS do nif.pt (públicos e autoritativos).

    Deliberadamente NÃO grava: `entity_type` (é INFERIDO por nós do nome+natureza, não um
    campo do nif.pt), capital social, faturação/nº de empregados, telefone/site/fax — quem
    faz o match sem login não deixa esses dados na BD.
    """
    profile.nif = metadata["nif"]
    # `or` preserva um valor manual já existente quando a origem não traz nada.
    profile.entity_size = metadata.get("dimension") or profile.entity_size
    profile.activity = metadata.get("activity") or profile.activity

    main_cae = metadata.get("main_cae")
    if main_cae and len(main_cae) == 5:
        profile.main_cae = main_cae
    profile.secondary_cae = [
        c for c in (metadata.get("secondary_cae") or []) if len(str(c)) == 5
    ]
    profile.nature = metadata.get("nature")

    profile.address = metadata.get("address") or profile.address
    profile.city = metadata.get("city")
    profile.county = metadata.get("county")
    profile.parish = metadata.get("parish")
    profile.postal_code = metadata.get("postal_code")
    # `region` guarda a NUTS II RESOLVIDA (nuts.json, a mesma taxonomia dos avisos), não o
    # texto bruto do nif.pt — cai para este só se a resolução de NUTS falhar.
    profile.region = (
        metadata.get("nuts_ii_old") or metadata.get("nuts_ii") or metadata.get("region")
        or profile.region
    )


def _apply_viewer_role(user: User, profile: UserProfile, is_new: bool) -> bool:
    """Garante viewer+inativo em contas novas ou que ainda sejam viewer. Devolve `is_viewer`.

    Uma conta já promovida a client mantém role e is_active: uma nova avaliação do NIF
    atualiza os dados, mas NUNCA a rebaixa.
    """
    is_viewer = is_new or profile.role == UserProfile.VIEWER
    if is_viewer:
        profile.role = UserProfile.VIEWER
        if user.is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])
    return is_viewer


def _apply_contact(user: User, profile: UserProfile, contact: dict) -> bool:
    """Preenche o contacto do lead. Devolve True se o email foi definido AGORA (1ª vez).

    Só PREENCHE campos vazios — o 1º contacto fica. Um pedido anónimo não pode substituir o
    contacto já registado de um lead: senão bastava saber o NIF público para apontar a ficha
    ao seu próprio email, roubando à equipa comercial o contacto real e passando a receber
    tudo o que fosse enviado àquele lead. Corrigir um contacto errado é um passo AUTENTICADO
    (gestão de utilizadores).
    """
    user_fields = []
    email_is_new = bool(contact.get("email")) and not user.email
    if email_is_new:
        user.email = str(contact["email"]).strip()
        user_fields.append("email")
    if contact.get("name") and not user.first_name:
        user.first_name = str(contact["name"]).strip()
        user_fields.append("first_name")
    if user_fields:
        user.save(update_fields=user_fields)
    if contact.get("job_title") and not profile.job_title:
        profile.job_title = str(contact["job_title"]).strip()
    return email_is_new


@transaction.atomic
def create_or_update_viewer(metadata: dict, contact: dict | None = None) -> User:
    """Cria/atualiza o lead (role=viewer) com os dados MÍNIMOS do NIF. Idempotente.

    `contact` (email/nome/função) NÃO vem do nif.pt — vem do pop-up que o `evaluate` pede
    antes de revelar os resultados a quem não tem sessão. Só é aplicado em contas que ainda
    são viewer: numa conta já promovida o contacto vem da gestão de utilizadores, nunca de um
    pedido anónimo (ver o módulo para a cadeia de ataque que isto fecha).

    Um utilizador AUTENTICADO nunca chega aqui — ver `NifMatchingService.evaluate`.
    """
    user, profile, is_new = _get_or_create_lead_account(metadata["nif"])

    _apply_company_data(profile, metadata)
    is_viewer = _apply_viewer_role(user, profile, is_new)
    email_is_new = _apply_contact(user, profile, (contact or {}) if is_viewer else {})

    profile.save()

    # Boas-vindas: só na 1ª vez que este lead deixa o email (não reenvia a cada match
    # seguinte do mesmo NIF). Nunca rebenta o fluxo — ver notifications.
    if email_is_new:
        notifications.send_welcome_email(user.email)

    return user


def promote_viewer_to_client(nif: str) -> dict | None:
    """Promove um viewer a client: muda o role e ativa a conta (is_active=True).

    Idempotente. Devolve os dados atualizados ou None se não existir perfil com o NIF.
    Nota: as credenciais (username/password) são definidas mais tarde, num passo próprio
    — aqui a conta fica ativa mas ainda sem password utilizável até esse passo.

    SEGURANÇA: ao ATIVAR a conta, qualquer password definida enquanto ela ainda era um
    viewer é invalidada. Um viewer nasce de um pedido ANÓNIMO, e o email desse pedido é o
    seletor do /users/password-reset/ — sem esta linha, quem soubesse o NIF público podia
    deixar uma password sua "à espera" numa conta inativa e entrar assim que alguém a
    promovesse. Definir a password é um passo DEPOIS da promoção, já com a conta ativa e o
    email sob controlo de quem a gere.
    """
    profile = UserProfile.objects.filter(nif=nif).select_related("user").first()
    if profile is None:
        return None

    user = profile.user
    if profile.role != UserProfile.CLIENT:
        profile.role = UserProfile.CLIENT
        profile.save(update_fields=["role"])

    just_activated = not user.is_active
    if just_activated:
        user.set_unusable_password()
        user.is_active = True
        user.save(update_fields=["is_active", "password"])

    # A conta ficou ativa mas SEM password utilizável — sem este email ficaria por aí sem
    # forma de entrar até alguém se lembrar de a disparar à mão. Só na ATIVAÇÃO: promover de
    # novo uma conta já ativa não incomoda o cliente com outro email.
    # Import local: users.service já toca em match.models (enriquecimento por NIF).
    if just_activated and user.email:
        from users.service import request_password_reset
        request_password_reset(user.email)

    return {
        "user_id": user.id,
        "nif": nif,
        "role": profile.role,
        "is_active": user.is_active,
        "has_login": user.has_usable_password(),
    }
