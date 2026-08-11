# App `users`

Gestão de utilizadores, autenticação (sessão/cookie) e autorização por **role**.
Prefixo de todas as rotas: **`/users/`**

---

## Autenticação

Usa **sessão do Django** (cookie `sessionid`). Faz-se `login` uma vez; os pedidos
seguintes reenviam o cookie automaticamente (no Postman é transparente).

As views da API são `@csrf_exempt` (API JSON consumida por cliente não-browser),
por isso **não** é preciso token CSRF.

## Roles

Cada utilizador tem um perfil (`UserProfile`) com um de cinco roles:

- `admin` — acesso total a tudo.
- `commercial_grants` — comercial especialista em avisos (grants): vê/edita avisos, match,
  plano anual, newsletter, e gere utilizadores `viewer`/`client`.
- `commercial_public` — comercial de contratação pública: acumula avisos **e** anúncios, mais
  match, plano anual, newsletter, e gere utilizadores `viewer`/`client`.
- `client` — vê (sem editar) avisos, anúncios e match.
- `viewer` — conta inativa criada automaticamente por quem consulta o match **sem login** (lead).
  Sem acesso a login até ser promovido a `client` (`POST /match/promote/<nif>/`).

**Sem login**: só o match (`POST /match/evaluate-nif/` e afins) e o detalhe dos avisos que
esse match devolveu (`GET /avisos/<id>/` e `/document/`, restritos à própria sessão) são
acessíveis — nunca a listagem de avisos/anúncios/utilizadores.

**Campos por papel** — um `admin` ou comercial é uma pessoa da equipa: identifica-se por
`username`, `first_name` (nome), `email` e `role`, e mais nada. Os campos de ENTIDADE (NIF, CAE,
morada, região, NUTS, `job_title`) descrevem uma **empresa candidata** e só existem em `client`
e `viewer` — no detalhe de uma conta interna nem sequer aparecem a null, estão ausentes. Se
forem enviados ao criar/atualizar um papel interno são **ignorados em silêncio**, não dão erro.

**Criar sem password** — uma conta criada por um admin/comercial nasce **sem password
utilizável** e a pessoa recebe logo o email para a escolher (uma `password` no corpo é
ignorada). Só o **registo público** — a pessoa a registar-se a si própria, sem sessão — define
a password na hora, porque obrigá-la a ir ao email para poder entrar seria atrito sem ganho.
Promover um `viewer` a `client` envia o mesmo email, já que a promoção ativa a conta sem
password.

**Definir password** — o `POST /users/<id>/password/` não recebe passwords: dispara o link de
redefinição para o email já registado na conta. Ninguém (nem o próprio, nem um admin) escolhe a
password de ninguém, pelo que ela nunca passa no corpo do pedido nem pelas mãos de quem gere a
conta — só quem controla a caixa de correio a consegue definir. Devolve 400 se a conta não tiver
email ou estiver inativa.

**Contas `viewer` e credenciais** — o email de um `viewer` é escrito por um pedido **anónimo**
(o pop-up de contacto do match) e o `username` é o NIF, que é público. Por isso esse email
não é de confiança enquanto a conta não for promovida, e três regras seguram a cadeia:
o `/users/password-reset/` ignora contas inativas; a promoção invalida qualquer password
definida na janela de `viewer` (devolve `has_login: false` — as credenciais definem-se
**depois**, já com a conta ativa); e um pedido anónimo só **preenche** contacto em branco,
nunca substitui o contacto já registado de um lead.

**Superuser**: um superuser do Django (`createsuperuser`) é **sempre tratado como `admin`**
nas permissões, independentemente do role do perfil (bypass de bootstrap) — mas só o `admin`
consegue **ver** superusers na listagem/detalhe de utilizadores (os comerciais nunca veem).

---

## Rotas

| Rota                    | Método  | Permissão                          | Descrição                                               |
|-------------------------|---------|-------------------------------------|-----------------------------------------------------------|
| `/users/login/`         | POST    | **Público**                         | Inicia sessão.                                          |
| `/users/logout/`        | POST    | Público                             | Termina a sessão atual.                                 |
| `/users/password-reset/`| POST    | Público                             | Pede o email de redefinição (resposta sempre igual, nunca revela se a conta existe). Só chega a contas **ativas** — o email de um `viewer` vem de um pedido anónimo, ver nota abaixo. |
| `/users/password-reset/confirm/` | POST | Público                     | Define a nova password (uid+token do link recebido).   |
| `/users/me/`            | GET     | Autenticado (qualquer role)         | Perfil do utilizador autenticado.                       |
| `/users/`               | GET     | **admin, commercial_grants, commercial_public** | Lista. Admin filtra por `?role=` e `?active=true\|false\|all`; comerciais veem só `viewer`/`client`. |
| `/users/<id>/activate/` | POST    | **admin, commercial_grants, commercial_public** | Reativa o utilizador (`is_active=True`) — comerciais só um `viewer`/`client`. |
| `/users/create/`        | POST    | Público/comercial/admin             | admin→qualquer role; comercial→só `client`; client→403; público→`client`. |
| `/users/<id>/`          | GET     | Autenticado (varia por role)        | Detalhe de um utilizador.                               |
| `/users/<id>/update/`   | PUT     | Autenticado (varia por role)        | Atualiza dados. Alterar `role` → **só admin**.          |
| `/users/<id>/password/` | POST    | A própria conta, comercial (viewer/client) ou admin | **Envia o email** para a pessoa definir a password. Não define nenhuma — ver nota abaixo. |
| `/users/<id>/`          | DELETE  | **Só admin**                        | Desativa o utilizador (soft-delete: `is_active=False`). |

### Respostas de autorização
- **401** `Authentication required` — sem sessão iniciada.
- **403** `Forbidden` — autenticado mas sem o role necessário.

---

## Detalhe de cada rota

### `POST /users/login/` — Público
Body:
```json
{ "username": "ana", "password": "secret" }
```
Devolve os dados do utilizador (com `role`) e define o cookie de sessão.
**401** se as credenciais forem inválidas.

### `POST /users/logout/` — Público
Sem body. Termina a sessão.

### `GET /users/me/` — Autenticado
Devolve o perfil do **próprio** utilizador autenticado (não precisa de saber o id). **401** se não autenticado.

### `GET /users/` — admin, commercial_grants, commercial_public
Lista: `{ "total": N, "users": [ ... ] }`.

**Admin** — todos os filtros:
- `?role=admin|commercial_grants|commercial_public|client|viewer`
- `?active=true` (default) | `false` | `all`
- por campos: `entity_type`, `entity_size`, `nif`, `main_cae`, `secondary_cae`,
  `region`, `address`, `incorporation_date`, `username`, `email`, `nuts_ii`, `nuts_iii`.
- É o único que vê utilizadores `admin`/`commercial_*` e superusers.

**Comercial** (`commercial_grants` ou `commercial_public`) — vê **sempre só** `viewer` e
`client` (sem filtro de estado — os `viewer` ficam inativos até serem promovidos); nunca um
superuser; pode filtrar **todos os campos exceto `address`** (`?role=`/`?active=`/`?address=`
são ignorados).

**Notas de filtros:**
- `main_cae` / `secondary_cae` → **prefixo**: `?main_cae=62` devolve todos os CAE que começam por `62` (e `?main_cae=62010` o exato).
- `region`, `address`, `username`, `email` → contém (case-insensitive).
- `nuts_ii`/`nuts_iii` → `true`/`false`. Restantes → igualdade exata.
- Combináveis: ex. `?role=client&active=false&region=Norte`.

Outros roles recebem **403**.

### `POST /users/<id>/activate/` — admin, commercial_grants, commercial_public
Reativa um utilizador soft-deleted/inativo (`is_active=True`). Admin: qualquer um. Comercial:
só se o alvo for `viewer` ou `client` (nunca admin/outro comercial/superuser) → senão **403**.
Devolve o perfil atualizado. **404** se o id não existir.

### `POST /users/create/` — Público
Body (campos opcionais exceto `username`/`password`):
```json
{
  "username": "ana", "password": "secret", "email": "ana@x.pt",
  "entity_type": "empresa", "entity_size": "media",
  "incorporation_date": "2015-03-10", "nif": "500123456",
  "main_cae": "62010", "secondary_cae": "70220",
  "address": "Rua X, Porto", "region": "Norte",
  "nuts_ii": true, "nuts_iii": false
}
```
Regras de `role` consoante quem cria:
- **Admin** (ou superuser): pode definir qualquer role.
- **commercial_grants** / **commercial_public**: só podem criar `client`; se pedirem outro role → **403** `Só pode criar utilizadores 'client'`.
- **Client**: não pode criar utilizadores → **403** `Não pode criar utilizadores`.
- **Público** (sem login): cria sempre `client` (o `role` pedido é ignorado).
- **400** se `username` faltar ou já existir (username é único).

### `GET /users/<id>/` — Autenticado
Detalhe do utilizador `<id>`. Admin: qualquer um. Comercial: só `viewer`/`client` (nunca
superuser) — mais o próprio perfil. Client: só o próprio perfil. Fora disso → **403**.
**404** se não existir.

### `PUT /users/<id>/update/` — Autenticado
Atualiza qualquer campo (username, email, dados de entidade, password...).
```json
{ "email": "ana@x.pt", "region": "Centro", "role": "commercial_public" }
```
- Mesma regra de alcance do `GET /users/<id>/` (admin: todos; comercial: `viewer`/`client`; os
  restantes: só o próprio).
- O `role` **só é aplicado se quem faz o pedido for admin** (caso contrário é ignorado).
- **400** se mudar `username` para um já existente.
- **404** se o utilizador não existir.

### `POST /users/<id>/password/` — Própria conta, comercial (viewer/client) ou admin
- **A própria password**: exige a atual.
  ```json
  { "current_password": "antiga", "password": "nova" }
  ```
  A sessão mantém-se ativa após a mudança.
- **Admin ou comercial a redefinir outro utilizador** (comercial só se o alvo for
  `viewer`/`client`): não precisa da atual.
  ```json
  { "password": "nova" }
  ```
  A sessão do utilizador-alvo é invalidada (terá de voltar a entrar).
- **403** se não tiver permissão para mexer nessa conta.
- **400** se a password atual estiver errada ou faltar a nova.

### `DELETE /users/<id>/` — Só admin
**Soft-delete**: não apaga o registo — põe `is_active=False`. Efeitos:
- O utilizador deixa de aparecer em `GET /users/` (lista só ativos).
- Deixa de conseguir fazer login (o Django rejeita contas inativas → 401).
- O registo mantém-se na BD (pode ser reativado pondo `is_active=True`).

**204** em sucesso, **404** se o id não existir.

---

## Notas

- As passwords são sempre guardadas com **hash** (`set_password`); nunca em texto simples.
- O `username` é **único** (garantido pela BD e validado com erro 400 amigável).
- Ao criar um User (em qualquer sítio — `createsuperuser`, admin, registo) é criado automaticamente o `UserProfile` (role `client`) via signal.
- **Bootstrap do primeiro admin**: cria um superuser (`python manage.py createsuperuser`). Como o superuser é tratado como admin, já consegue criar/promover outros admins.
