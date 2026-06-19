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

Cada utilizador tem um perfil (`UserProfile`) com um de quatro roles:

- `admin` — acesso total à gestão de utilizadores.
- `commercial` — pode listar utilizadores e criar utilizadores `client`.
- `composer` — pode criar utilizadores `client` (não lista).
- `client` — acesso mínimo (default no registo público).

**Superuser**: um superuser do Django (`createsuperuser`) é **sempre tratado como `admin`**,
independentemente do role do perfil (bypass de bootstrap).

---

## Rotas

| Rota                    | Método  | Permissão                     | Descrição                                               |
|-------------------------|---------|-------------------------------|---------------------------------------------------------|
| `/users/login/`         | POST    | **Público**                   | Inicia sessão.                                          |
| `/users/logout/`        | POST    | Público                       | Termina a sessão atual.                                 |
| `/users/me/`            | GET     | Autenticado (qualquer role)   | Perfil do utilizador autenticado.                       |
| `/users/`               | GET     | **admin, commercial**         | Lista. Admin filtra por `?role=` e `?active=true\|false\|all`; commercial vê só `client` ativos. |
| `/users/<id>/activate/` | POST    | **Só admin**                  | Reativa o utilizador (`is_active=True`).                |
| `/users/create/`        | POST    | Público/commercial/composer/admin | admin→qualquer role; commercial/composer→só `client`; client→403; público→`client`. |
| `/users/<id>/`          | GET     | Autenticado (qualquer role)   | Detalhe de um utilizador.                               |
| `/users/<id>/update/`   | POST/PUT| Autenticado (qualquer role)   | Atualiza dados. Alterar `role` → **só admin**.          |
| `/users/<id>/password/` | POST    | A própria conta **ou** admin  | Muda a password.                                        |
| `/users/<id>/`   | DELETE  | **Só admin**                  | Desativa o utilizador (soft-delete: `is_active=False`). |

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

### `GET /users/` — admin, commercial
Lista: `{ "total": N, "users": [ ... ] }`.

**Admin** — todos os filtros:
- `?role=admin|commercial|composer|client`
- `?active=true` (default) | `false` | `all`
- por campos: `entity_type`, `entity_size`, `nif`, `main_cae`, `secondary_cae`,
  `region`, `address`, `incorporation_date`, `username`, `email`, `nuts_ii`, `nuts_iii`.

**Commercial** — vê **sempre só** `client` **ativos**; pode filtrar **todos os campos exceto `address`**
(`?role=`/`?active=`/`?address=` são ignorados).

**Notas de filtros:**
- `main_cae` / `secondary_cae` → **prefixo**: `?main_cae=62` devolve todos os CAE que começam por `62` (e `?main_cae=62010` o exato).
- `region`, `address`, `username`, `email` → contém (case-insensitive).
- `nuts_ii`/`nuts_iii` → `true`/`false`. Restantes → igualdade exata.
- Combináveis: ex. `?role=client&active=false&region=Norte`.

Outros roles recebem **403**.

### `POST /users/<id>/activate/` — Só admin
Reativa um utilizador soft-deleted (`is_active=True`). Devolve o perfil atualizado. **404** se o id não existir.

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
- **Admin** (ou superuser): pode definir qualquer role (`admin` | `commercial` | `composer` | `client`).
- **Commercial** / **Composer**: só podem criar `client`; se pedirem outro role → **403** `Só pode criar utilizadores 'client'`.
- **Client**: não pode criar utilizadores → **403** `Não pode criar utilizadores`.
- **Público** (sem login): cria sempre `client` (o `role` pedido é ignorado).
- **400** se `username` faltar ou já existir (username é único).

### `GET /users/<id>/` — Autenticado
Detalhe do utilizador `<id>`. **404** se não existir.

### `POST|PUT /users/<id>/update/` — Autenticado
Atualiza qualquer campo (username, email, dados de entidade, password...).
```json
{ "email": "ana@x.pt", "region": "Centro", "role": "consultant" }
```
- O `role` **só é aplicado se quem faz o pedido for admin** (caso contrário é ignorado).
- **400** se mudar `username` para um já existente.
- **404** se o utilizador não existir.

### `POST /users/<id>/password/` — Própria conta ou admin
- **A própria password**: exige a atual.
  ```json
  { "current_password": "antiga", "password": "nova" }
  ```
  A sessão mantém-se ativa após a mudança.
- **Admin a redefinir outro utilizador**: não precisa da atual.
  ```json
  { "password": "nova" }
  ```
  A sessão do utilizador-alvo é invalidada (terá de voltar a entrar).
- **403** se um não-admin tentar mudar a password de outra conta.
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
