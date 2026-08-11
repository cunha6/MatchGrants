# Decisões de Arquitetura (ADRs)

Registo das decisões não óbvias do MatchGrants e do *porquê*. Cada uma segue o formato
**Contexto → Decisão → Consequências**.

---

## ADR-1 — JSONB no `Grant` vs tabelas normalizadas

**Contexto.** A pipeline de IA extrai de cada aviso ~50 campos, muitos deles listas/estruturas
aninhadas (indicadores, despesas elegíveis, critérios de seleção, legislação…). A tentação é
normalizar tudo em tabelas.

**Decisão.** Regra única: **normaliza-se o que a base *consulta/filtra*; fica em JSONB o que é
lido em bloco com o aviso.**
- **JSONB** (no próprio `Grant`): `output_indicators`, `eligible_expenses`,
  `project_selection_criteria`, `applicable_legislation`, etc. — são sempre lidos como parte do
  documento do aviso, nunca são critério de query. Normalizá-los seria só *joins* e migrações sem
  retorno.
- **Tabelas-filhas**: `Phase`, `CoveredArea`, `PhaseArea`, `FinancingRate`,
  `BeneficiaryByAction` — participam no cálculo de taxa/dotação efetivas e na ordenação, logo
  precisam de ser consultáveis e indexáveis.

**Consequências.** Menos tabelas e menos *joins* no caminho de leitura; o *trade-off* é que os
campos JSONB não são pesquisáveis por SQL — aceite porque nenhum deles é critério de match.

---

## ADR-2 — Normalizar o CAE (tabela `GrantCae`), mas **não** as regiões

**Contexto.** O CAE e a região são os dois grandes filtros de elegibilidade. Ambos vivem como
JSONB de texto livre (`included_caes`/`excluded_caes`, `eligible_regions`). Com o histórico a
crescer (dezenas de milhares de avisos), carregar todos para filtrar em Python não escala.

**Decisão.**
- **CAE → normalizado.** O CAE tem uma hierarquia **limpa de prefixos numéricos** (`64***` ⊃
  `641**` ⊃ `64110`). Criou-se `GrantCae(grant, prefix, kind)` como **índice derivado** dos JSONB
  (fonte de verdade), mantido em sincronia por *signal* (`avisos/signals.py`). O match faz um
  **prefiltro em SQL** (`Exists`/`OuterRef` sobre os prefixos do cliente) que devolve só os avisos
  CAE-candidatos. Está **provado por teste** que o resultado é idêntico ao filtro puro em Python.
- **Região → NÃO normalizada.** A lógica de localização é *fuzzy*: correspondência por *contains*
  bidirecional **mais** um *fallback* que procura a zona do cliente no texto livre de
  elegibilidade. Não há hierarquia limpa que permita um prefiltro SQL provadamente idêntico.
  Acresce que, no domínio atual, **todos os avisos são de âmbito nacional** → o filtro de região
  nunca exclui ninguém. Uma tabela `GrantRegion` + *signal* + migração seria infraestrutura para
  uma regra que é sempre verdadeira: **over-engineering**, por isso foi deliberadamente evitada.

**Consequências.** Ganha-se escala onde ela é real (CAE) sem pagar complexidade onde não traz
retorno (região). Se no futuro surgirem avisos regionais com regras discretas, reavalia-se.

---

## ADR-3 — Índice **parcial** para o conjunto quente

**Contexto.** O match, a cada pedido, filtra `WHERE ai_processed AND active`. O custo é
proporcional ao **conjunto ativo** (~1–2 k avisos), não ao histórico total (potencialmente 10–50 k
avisos inativos que ficam na tabela).

**Decisão.** Índice **parcial** `grant_active_processed_idx` com
`condition=Q(ai_processed=True, active=True)`. O índice só contém as linhas do conjunto quente.

**Consequências.** A query do match mantém-se instantânea mesmo com o histórico a crescer, sem
particionar a tabela nem arquivar avisos. Foi a resposta à pergunta *"e se amanhã entrarem 50 000
avisos?"* — o histórico só tocava o match aqui, e este índice resolve-o sem redesenho.

---

## ADR-4 — *Embeddings* especializados por aviso (`GrantEmbedding`)

**Contexto.** Um único vetor por aviso misturava o domínio setorial ("gestão de resíduos") com
contexto genérico (região, tipo de entidade), diluindo o sinal setorial que é o que mais importa
para a relevância.

**Decisão.** Um `GrantEmbedding` por **tipo** (`GENERAL`, `SECTOR`), com índice **HNSW** e cache
por `text_hash` (não recalcula se o texto não mudou). A relevância final é uma média ponderada:
**0.60 setorial + 0.40 geral**, renormalizada quando falta uma componente.

**Consequências.** Ordenação mais fiel ao alinhamento setorial. Custo: mais uma linha por tipo e
uma chamada de embedding por texto novo — mitigado pela cache.

---

## ADR-5 — Camada final de validação por LLM (com *cap* e degradação graciosa)

**Contexto.** O filtro rígido + semântica ainda deixa passar avisos "tecnicamente elegíveis mas
não adequados" ao cliente. Um LLM consegue esse juízo qualitativo.

**Decisão.** Depois de ordenar, os avisos passam por um LLM gratuito (OpenRouter,
`nvidia/nemotron-3-ultra-550b-a55b:free`) que marca cada um como adequado/não-adequado; os não
adequados são removidos. Duas salvaguardas:
- **Cap top-10** — só os 10 mais relevantes vão ao LLM (limita custo/latência). Os restantes
  passam sem validação e ficam no fundo, onde já estavam por relevância.
- **Degradação graciosa** — sem `OPENROUTER_API_KEY`, em erro HTTP ou resposta ilegível, **nada é
  filtrado**: o match devolve a lista da semântica. A camada nunca pode *esconder* avisos por
  falha da dependência externa.

**Consequências.** Melhor precisão no topo sem bloquear o fluxo se o serviço externo falhar.
Rejeitou-se explicitamente um *threshold* de similaridade semântica como filtro: medições reais
mostraram que qualquer corte útil (ex. 40 %) eliminava avisos legítimos — o CAE e o LLM são o
mecanismo certo, não um limiar no cosseno.

---

## ADR-6 — Rotação de chaves de APIs externas (nif.pt e OpenRouter)

**Contexto.** Ambas as APIs têm limites por chave — o nif.pt por quota contratada, o OpenRouter
pelo limite do plano gratuito. O projeto dispõe de várias chaves de cada
(`NIF_KEY`/`NIF_KEY1..4` e `OPENROUTER_API_KEY`/`OPENROUTER_API_KEY1`).

**Decisão.** *Round-robin* ao nível do processo: cada chamada usa a chave seguinte, uma de cada
vez. O índice é global e protegido por *lock* (seguro entre *threads* do gunicorn). Lê-se a
lista das `settings` a cada chamada para respeitar `override_settings` nos testes. O mesmo
padrão em `match/services.py:_next_nif_key` e `match/llm_validation.py:_next_api_key`.

**Consequências.** Reparte a carga pelas chaves sem coordenação externa: com N chaves, o limite
efetivo é N vezes maior. Uma chave explícita (ex. nos testes) contorna a rotação. Como o índice
é por processo, dois workers do gunicorn podem usar a mesma chave em paralelo — aceitável, já
que o objetivo é repartir, não garantir exclusividade.

---

## ADR-7 — Tabela derivada mantida por *signal* (fonte de verdade única)

**Contexto.** `GrantCae` é derivada dos JSONB `included_caes`/`excluded_caes`. Derivados podem
dessincronizar-se da fonte.

**Decisão.** Os JSONB são a **única fonte de verdade** (é o que a extração escreve e a edição
altera). Um `post_save` em `Grant` reconstrói as linhas `GrantCae` sempre que os campos CAE mudam;
*saves* com `update_fields` que não tocam nos CAE (ex. o *save* do embedding) são ignorados para
evitar reconstruções inúteis. Uma migração de dados populou a tabela para os avisos preexistentes.

**Consequências.** O resto do código não precisa de saber que `GrantCae` existe — escreve nos
JSONB e a tabela segue. A regra fina do CAE ("o prefixo mais específico ganha") continua no
Python; a tabela só serve para o Postgres *estreitar* o conjunto.

---

## ADR-8 — Docling e cliente OpenAI genérico movidos para `common/`

**Contexto.** `/anuncios/<id>/detail/` precisa de converter um PDF (caderno de encargos) em
markdown e pedir um JSON ao OpenAI — exatamente o que os avisos já faziam, mas até aqui esse
código vivia todo dentro de `avisos/` (`avisos/Docling/`, `avisos/IA/openai_client.py`).

**Decisão.** Separou-se pela genuinidade da dependência a domínio, não por onde o código
"parecia" pertencer:
- `common/docling/` (`converter.py` + `docling_ocr.py`) — download de PDF, deteção de convite,
  conversão para markdown, recuperação de fórmulas de mérito. Movido tal e qual; a única
  função ficada para trás foi `download_document()`, código morto (zero chamadores) com
  import rígido a `avisos.IA.pipeline`/`avisos.db_service` que só faria sentido em avisos.
- `common/openai_client.py` — só `create_client()` e `call_openai_text()` (prompt de texto
  livre, sem *chunks*). Ganhou um `json_mode: bool` para o pedido em JSON dos anúncios.
- Ficou em `avisos/IA/openai_client.py`: `call_openai()` (parametrizado a `chunks: list[dict]`
  e a `build_messages_from_chunks`) e `classify_ambiguous_chunks()` (acoplado a
  `ROUTER_SYSTEM`/à taxonomia de categorias P1-P6 do avisos) — específicos do *pipeline* de
  7 prompts dos avisos, não genéricos.

**Consequências.** `anuncios/specifications_ai.py` reutiliza o mesmo Docling/OpenAI dos avisos
sem duplicar código nem depender de `avisos/`. Os 5 pontos de importação internos ao avisos
(`service.py`, `scrape_portugal.py`, `tests.py`, `IA/chunker.py`, `IA/pipeline.py`) passaram a
apontar para `common.docling`/`common.openai_client` — comportamento inalterado, confirmado
pela suite completa (495 testes) antes e depois da mudança.

---

## ADR-9 — Geração do detalhe IA em thread de fundo, não em processo separado

**Contexto.** `POST /anuncios/<id>/detail/` converte um PDF (Docling) e chama o OpenAI — pode
demorar vários segundos. Bloquear o pedido HTTP até terminar dava uma má experiência (e um
timeout fácil de atingir); mas o projeto já tinha um padrão para trabalho longo em background:
`spawn_specifications_download` (ADR indireto, ver `anuncios/services.py`) lança um PROCESSO
separado (`subprocess.Popen` + ficheiro de lock/PID), pensado para um único job global de
scraping que tem de sobreviver aos reloads do `runserver`.

**Decisão.** Esse padrão não serve aqui: o detalhe IA é *por anúncio*, disparado a pedido, e
dura segundos — não faz sentido arrancar um processo Python/Django inteiro por pedido. Em vez
disso, `generate_detail_async` (em `anuncios/specifications_ai.py`) usa uma `threading.Thread`
daemon dentro do próprio processo do gunicorn: um `UPDATE` atómico
(`.exclude(status=GENERATING).update(status=GENERATING)`) garante que só quem "ganha" a corrida
arranca a thread — um pedido concorrente ao mesmo anúncio só encontra `status=generating` e não
duplica o trabalho (nem o custo). A thread fecha a sua própria ligação à BD no fim
(`connection.close()`, obrigatório — cada thread nova do Django abre a sua ligação lazy).
`specifications_ai_status` (pending/generating/done/error) guarda o estado; ERROR é tratado
como PENDING numa chamada seguinte — uma falha transitória do OpenAI resolve-se sozinha ao
reabrir o anúncio, sem endpoint de retry dedicado.

**Consequências.** `POST /anuncios/<id>/detail/` passou a devolver 202 (a gerar) ou 200 (pronto)
em vez de bloquear. Threads não sobrevivem a um restart do gunicorn (ao contrário do processo
separado do scraping): se o worker reiniciar a meio, o estado fica preso em `generating` — para
isso não bloquear o anúncio para sempre, `generate_detail_async` trata uma `generating` mais
antiga que `_STALE_GENERATING_AFTER` (5 min, generoso face à duração normal) como reclamável,
tal como o `_LOCK_MAX_AGE` já fazia para o lock de importação em `anuncios/services.py` — o
mesmo padrão de "lock com validade", aqui em cima de `updated_at` em vez de um ficheiro.

---

## ADR-10 — Retry na validação LLM do match + verificação de domínio do email

**Contexto.** Duas fragilidades encontradas ao investigar por que o match "ignorava" avisos
para um NIF real: (1) a camada final de validação por LLM (`match/llm_validation.py`, modelo
gratuito OpenRouter) falhava com frequência de forma TRANSITÓRIA (rate-limit da Nvidia,
resposta ilegível) — cada falha desistia à primeira tentativa e devolvia TODOS os avisos
elegíveis sem filtro semântico, em vez do resultado mais preciso; (2) o gate de contacto do
match (`match/services.py:_missing_contact_fields`) só validava o FORMATO do email
(`django.core.validators.validate_email`), nunca se o domínio existia — um email com domínio
inventado ou mal escrito passava como "válido".

**Decisão.**
- `validate_matches` tenta agora até `_MAX_ATTEMPTS=2` vezes antes de desistir, rodando para a
  chave OpenRouter seguinte a cada tentativa (mesmo `_next_api_key` de sempre) — só devolve `{}`
  (nada filtrado) se TODAS as tentativas falharem, preservando a degradação graciosa já existente.
  `_TIMEOUT` baixou de 90s para 60s. Números calibrados por medição real (ver auditoria de
  desempenho abaixo), não por adivinhação: o modelo gratuito (`nemotron-3-ultra`, "reasoning")
  demora ~45s **mesmo numa chamada bem-sucedida** — um `_MAX_ATTEMPTS` alto combinado com um
  `_TIMEOUT` alto multiplica-se depressa para minutos de espera no pior caso (3×90s ≈ 4,5min);
  2×60s (~2min) foi o equilíbrio escolhido entre tolerar falhas transitórias e não fazer o
  utilizador esperar tempo de mais.
- `_is_valid_email` foi substituída por `_email_error_label`, usando `email_validator`
  (`pip install email-validator`) com `check_deliverability=True` — verifica formato E que o
  domínio tem registos MX/A por DNS. Devolve uma label ESPECÍFICA por motivo: formato inválido
  → "Email inválido" (igual a antes); domínio sem registos → mensagem dedicada a pedir um email
  profissional da empresa; continua a verificar `is_disposable_email` a seguir (webmail
  genérico/descartável), sem substituir essa camada.
- Uma falha de REDE/DNS ao consultar o domínio (não confundir com "o domínio não tem registos")
  degrada para "válido" — mesma filosofia do resto do projeto (nif.pt, OpenRouter): um problema
  transitório nosso não pode bloquear um lead real.
- A consulta DNS usa um `dns_resolver` PARTILHADO (`email_validator.caching_resolver`), não o
  resolver por omissão da biblioteca: timeout de 3s (não os 15s por omissão — um domínio
  inacessível não pode prender o pedido 15s para uma verificação que normalmente demora
  <0.4s) e cache LRU em memória (o mesmo domínio, testado várias vezes seguidas — comum em
  dev/QA — só faz a consulta DNS real da primeira vez; medido: 2ª consulta cai de dezenas de
  ms para <1ms).

**Auditoria de desempenho** (pedida diretamente: "verifica se o match está a acontecer o mais
rápido possível"). Medido com `time.perf_counter`/`cProfile` sobre um NIF real: nif.pt
`fetch_company` 0.24s, CTT (código postal→localidade/NUTS) 0.05s, embeddings OpenAI 1.6s,
elegibilidade+ranking de 28 avisos (100% em memória, sem rede) <0.3s — total **~2.3s sem
LLM**. A única etapa lenta é mesmo a validação LLM (confirmado por `cProfile`: 100% do tempo é
`_ssl._SSLSocket.read`, zero overhead de código) — não há nada a otimizar nas outras etapas.

**Consequências.** `check_deliverability=True` faz uma consulta DNS real por validação (~0.1-
0.3s, medido) — desprezável face ao resto do fluxo. Os testes mockam sempre esta consulta
(`validate_email` patchado para `check_deliverability=False` em `ViewerCreationTests.setUp`)
— descobriu-se, ao integrar, que um dos domínios placeholder já usados nos testes de
segurança (`dominio-mau.pt`, o "email do atacante") não tem DNS real, o que teria partido
esses testes sem o mock. (Nota: `_email_error_label`/`validate_email` mudaram de sítio no
ADR-11 — `match.services` → `common.email_validation` — o resto desta decisão mantém-se.)

---

## ADR-11 — Registo público: sem password na hora, código postal em vez de região, NIF validado

**Contexto.** Três problemas no registo público (`POST /users/create/` sem sessão): (1) a
validação exigia `nuts_ii`/`nuts_iii` (booleanos) mesmo já não fazendo parte do formulário —
"The 'nuts_ii' field is required" mesmo com tudo o resto preenchido; (2) pedia `region` como
texto livre ao utilizador, quando já existia `postal_code` no modelo, por preencher; (3) não
validava o NIF nenhum (só o comprimento, via `MinLengthValidator(9)`), nem o email (nem
formato nem domínio) — um "999999999" ou um "x" qualquer passavam.

**Decisão.**
- `nuts_ii`/`nuts_iii` saíram da lista de campos obrigatórios (os campos do modelo continuam
  lá, só deixaram de ser exigidos — evita partir quem ainda os edite manualmente).
- `region` saiu dos obrigatórios; `postal_code` entrou no lugar. `_fill_location_from_postal_code`
  (chamada em `_apply_profile`) deriva `city`/`county`/`region` a partir do código postal —
  MESMA lógica de `match/company_metadata.py:_location` (CTT→NUTS, ver ADR abaixo sobre a
  extração para `common/`), aqui aplicada ao valor que a própria pessoa introduz, não ao vindo
  do nif.pt. (O CTT devolve também o distrito, mas `UserProfile` nunca teve esse campo.)
- NIF: `_is_valid_company_nif` — 9 dígitos, dígito de controlo válido (algoritmo mod 11
  oficial), e o primeiro dígito não pode ser 1/2/3 (pessoa singular). Só se aplica quando o
  papel EFETIVO não é interno (`_STAFF_ROLES`) — um admin a criar um comercial que envie um
  `nif` por hábito não pode ver a criação falhar por causa de um campo que `_apply_profile` já
  ignora em silêncio para esse papel; a validação tinha de ficar coerente com isso.
- Email: `create_user` passou a chamar `common.email_validation.email_error_label` — mesma
  regra do gate de contacto do match (formato + domínio existe + não é webmail/descartável).
- Password: alinhado com o que já valia para contas criadas por um admin/comercial — NINGUÉM
  escolhe a password na criação, nem sequer quem se regista a si próprio. A conta nasce sem
  password utilizável e recebe o link por email (`request_password_reset`, incondicional
  agora). Isto tornou o parâmetro `created_by_staff` de `create_user`/`_validate_required_data`
  morto (só distinguia a exigência de password) — removido, não deixado como vestígio.

**Consequências.** `common/ctt.py`, `common/nuts.py` e `common/disposable_email.py` foram
extraídos de `match/` para `common/` na mesma leva (mesmo padrão do ADR-8: um SEGUNDO app,
`users`, passou a precisar deles) — `match/company_metadata.py` e `match/services.py`
apontam agora para lá, comportamento inalterado (confirmado pela suite completa antes/depois).
Descobriu-se pelo caminho que vários NIFs placeholder dos testes ("999999999") não passavam
o checksum oficial — corrigidos para NIFs válidos gerados propositadamente; e que "mail.com"
(usado como domínio de teste em vários sítios) está mesmo na lista de webmail genérico — os
testes que o usavam para chegar a `POST /users/create/` mudaram para "client.com" (já usado
de propósito noutros testes deste ficheiro, confirmado como seguro).

## ADR-12 — Histórico do último match gravado no perfil do client

**Contexto.** `POST /match/evaluate-nif/` nunca persistia o RESULTADO do match para quem já
tem conta — só o `viewer` (lead) de quem consulta sem sessão é que ficava gravado
(`create_viewer=not request.user.is_authenticated`). Um client autenticado a consultar o
próprio NIF via o site perdia os `matches` assim que saísse da página; não havia como voltar a
ver "que avisos me saíram" sem repetir o pedido.

**Decisão.** `UserProfile.matched_grants` — `ManyToManyField('avisos.Grant', ...)` (string
lazy: evita um import direto `users`→`avisos`, que antes não existia). Em `evaluate_nif`
(`match/views.py`), depois de `NifMatchingService.evaluate()` devolver os `matches`: se o
utilizador autenticado tem `role == CLIENT` **e** o NIF pedido é o do PRÓPRIO perfil
(`profile.nif == result["nif"]`), `matched_grants.set(...)` substitui o conjunto pelo
resultado desta chamada (é o último match, não um histórico acumulado). Um admin/comercial a
testar um NIF (o seu ou alheio) nunca escreve no próprio perfil — está a consultar, não a
gerar o histórico da própria conta (mesmo raciocínio do `create_viewer` original). E um client
a testar um NIF que não é o seu também não "herda" avisos alheios.

`_serialize` (`users/service.py`) expõe `matched_grants` (lista de `{id, grant_code, title}`)
no mesmo bloco dos outros campos de entidade (só client/viewer) — dá ao front-end o suficiente
para listar e linkar cada aviso a `GET /avisos/<id>/` sem pedido extra.

**Consequências.** `get_all_users` (listagem paginada) ganhou
`.prefetch_related("profile__matched_grants")` — sem isso, `_serialize` reintroduzia o N+1 que
o `select_related("profile")` já evitava (1 query M2M por utilizador da página em vez de uma
só). Testado em `match.tests.ClientMatchHistoryTests` (grava no NIF próprio, não grava em NIF
alheio nem para staff, substitui — não acumula) e `users.tests` (campo aparece no detalhe do
client, ausente no de staff).

## ADR-13 — Botão "bulletproof" (VML) nos emails, para o Outlook clássico

**Contexto.** Os botões "Marcar reunião" (`welcome.tsx`) e "Alterar password"
(`resetPassword.tsx`) não apareciam de todo no Outlook clássico (motor Word do Outlook
desktop) — não só sem estilo, ausentes. O `<Button>` do `@react-email/components` só tem o
truque dos caracteres invisíveis (`mso-font-width` + `<i hidden>`) para simular PADDING
horizontal num `<a>`; não existe nenhuma alternativa nativa para o Word quando este ignora
`display:inline-block`/`border-radius`/`background-color` no `<a>`, o que pode resultar no
elemento a não pintar nada.

**Decisão.** Técnica "bulletproof button" (VML + downlevel-revealed comments), gerada por uma
função `bulletproofButtonHtml(href, label, widthPx)` injetada via `dangerouslySetInnerHTML`
num único bloco (dividir por vários elementos React partia a fronteira dos comentários
condicionais, cada `<div>`/elemento fecha-se a si próprio): um `<v:roundrect>` VML — só o
Outlook o interpreta, via `<!--[if mso]>...<![endif]-->` — com o MESMO href/texto, e um `<a>`
normal (visualmente idêntico ao `<Button>` anterior) para todos os outros clientes, escondido
do Outlook via `<!--[if !mso]><!--> ... <!--<![endif]-->` (para não mostrar os DOIS botões no
Outlook). O `resetPassword.tsx` mantém o href placeholder `https://example.com` — aparece
DUAS vezes agora (VML + `<a>`), mas `users/notifications.py` já substituía todas as
ocorrências (`str.replace`, não só a 1ª), por isso não mudou.

**Consequências.** O HTML compilado (`emails/html/*_email.html`) teve de ser regenerado à
mão fora do `render_emails` (Node não está no container Docker — só no host); o comando em si
continua a ser o caminho normal quando corrido no host com Node instalado.
`test_request_reset_sends_email_for_valid_user` (já existente) confirma as DUAS ocorrências
do placeholder são substituídas. Verificação visual em Outlook clássico real fica por conta
de quem revir o email a seguir — não há como testar isto automaticamente.

## ADR-14 — Contacto do match: descartável/malformado rejeita ANTES de tocar em nif.pt

**Contexto.** No pop-up de contacto (email/nome/função) de quem não tem sessão,
`common.email_validation.email_error_label` já rejeita um domínio descartável/webmail em
milisegundos (é uma consulta local, ver ADR anterior sobre a ordem desta verificação). Mas
`NifMatchingService.evaluate` só levantava o 422 correspondente DEPOIS de calcular o match
inteiro — nif.pt, embeddings E a validação LLM (a parte lenta, medida em dezenas de segundos
por chamada, ver ADR-10). Resultado: submeter o pop-up com um email óbvio (ex: gmail.com,
mailinator.com) entrava no "pop-up de procura" do front-end e só voltava passado alguns
segundos com o erro — mesmo sendo uma validação local, instantânea.

Uma 1ª versão desta correção só saltava a chamada ao LLM (`skip_llm`), mantendo nif.pt +
embeddings — reduzia a espera de dezenas de segundos para poucos, mas não eliminava por
completo: nif.pt sozinho já é uma chamada de rede (~1-2s). Não chega quando o objetivo é
"instantâneo".

**Decisão.** `evaluate` valida o contacto ANTES de tudo o resto — antes até de olhar para a
cache: se `contact` tem pelo menos um campo preenchido (`any(contact.values())` — ou seja, o
pop-up FOI submetido, não é a 1ª chamada só com o NIF) e `_missing_contact_fields(contact)`
não fica vazio, levanta `MissingClientDataError` imediatamente — nada de nif.pt, embeddings,
cache ou LLM. `_missing_contact_fields` não depende de metadata (só do próprio `contact`), por
isso corre sem saber ainda quem é a empresa.

Um pedido com contacto ainda VAZIO (a 1ª chamada, só o NIF, antes do pop-up aparecer) continua
a correr a pesquisa por inteiro como sempre — é o comportamento já existente e testado, faz
sentido computar e cachear aí, porque é aí que o contacto ainda não foi sequer pedido.

**Consequências.** O lead (dados da empresa) deixa de ser registado quando o contacto vem
submetido-mas-inválido — só fica registado quando a pesquisa chega mesmo a correr (contacto
vazio, ou já válido). `test_disposable_email_domain_is_gated_as_invalid` foi ajustado: já não
espera um `User` criado nesse caso, só confirma `fetch_company` nunca chamado. O efeito
colateral positivo: repetir com o mesmo email inválido não gasta NENHUM pedido a nif.pt
(antes gastava 1, na 1ª tentativa) — só a correção final, com um contacto válido, é que chama
nif.pt pela primeira vez (`test_retrying_with_still_invalid_email_does_not_call_nif`).

## ADR-15 — Configuração de produção falha alto; endpoints de automação ficam abertos por decisão

**Contexto.** Uma revisão de segurança à configuração encontrou o ambiente a correr com
`DJANGO_DEBUG=false` (modo produção) mas **sem** `DJANGO_SECRET_KEY` definida — ou seja, a
assinar sessões e tokens de reset de password com a chave de desenvolvimento que está
versionada em `main/settings.py`. Como `default_token_generator` (ver
`users.service.request_password_reset`) deriva o token dessa chave, quem lesse o repositório
forjava um token de reset para qualquer `user_id`, chamava `POST /users/password-reset/confirm/`
e tomava a conta — incluindo a de admin. O `ALLOWED_HOSTS` tinha o mesmo problema por outra
via: a versão por ambiente estava comentada e substituída por `["*"]` fixo no código, o que
anulava o `DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,matchgrants` que o `docker-compose.yml`
já passava ao container.

**Decisão.** Estas duas variáveis passam a ser **obrigatórias quando `DEBUG=false`**: a app
levanta `ImproperlyConfigured` no arranque em vez de degradar em silêncio para uma chave
pública ou um wildcard. Em `DEBUG=true` nada muda — a chave de dev continua a servir e o
`ALLOWED_HOSTS` continua a ser `*` por omissão, porque o container é acedido ora por
`localhost`, ora pelo IP da máquina, ora pelo nome do serviço. A chave real foi gerada com
`get_random_secret_key()` e vive só no `.env` (fora do git). Falhar no arranque é preferível
a arrancar inseguro: um deploy que não sobe é visível de imediato, uma chave pública em
produção não dá sinal nenhum.

**Endpoints de automação — risco ACEITE, não corrigido.** `POST /avisos/` (e
`/compete/`, `/portugal/`, `/prr/`), `POST /anuncios/` e `GET /planned-grants/sync/` não
exigem autenticação, e disparam trabalho caro: Selenium, Docling e chamadas **pagas** à
OpenAI. Exposto à internet, isto é um vetor de esgotamento de saldo e de CPU/RAM do container.
Ficam abertos **por decisão explícita** — são chamados por automação sem sessão e o serviço
não está exposto publicamente. Se algum dia for, a correção de menor atrito é um segredo
partilhado num header (comparado com `hmac.compare_digest`), que não obriga a automação a
manter sessão. Fica registado aqui para a escolha ser deliberada e não um esquecimento.

`GET /planned-grants/sync/` acumula um segundo problema, também não corrigido: é um **GET com
efeitos de escrita**, portanto disparável por um `<img src>` ou por prefetch de browser/proxy.
O vizinho `list_planned_grants` tem `@require_role`; este não.

**Importação de anúncios: passa a RECUSAR em vez de matar.** `spawn_specifications_download`
matava sempre o processo anterior e relançava — apesar de o módulo já ter, construído e
testado, um mecanismo de lock com heartbeat (`mark_import_start`/`_heartbeat_lock`/
`import_running`) que **nunca era consultado** por este caminho. Daí dois defeitos: (1) um 2º
`POST /anuncios/` deitava fora uma extração de dezenas de minutos a meio; (2) o PID guardado
era morto às cegas mesmo quando já não havia extração viva — e um PID sem dono pode ter sido
**reutilizado pelo sistema operativo** por um processo alheio (o Postgres, o próprio worker),
que assim levava um SIGTERM. Agora o spawn consulta `import_running()`: havendo extração viva
devolve False sem lançar nada nem interromper o que está a correr; não havendo, limpa os
ficheiros de PID/lock que sobraram **sem matar coisa nenhuma**. `kill_previous_import()` foi
removida — era a implementação da política antiga e ficaria sem chamadores. Uma extração que
morra sem se limpar liberta-se sozinha quando o lock envelhece (`_LOCK_MAX_AGE`, 10 min), por
isso não fica presa. O campo `specifications` da resposta distingue agora os dois casos.

**Consequências.** Trocar a `SECRET_KEY` invalidou as sessões abertas e quaisquer links de
reset de password ainda por usar — é o efeito pretendido, dado que a chave anterior era
pública. Corrigiu-se ainda, na mesma passagem, um `@csrf_exempt` que estava aplicado ao
helper `_parse_edit_body` em vez da view `grants_edit` (`avisos/views.py`): o gémeo
`anuncios.views.notice_edit` tinha-o, e sem ele o `PUT/PATCH /avisos/<id>/edit/` respondia 403
a partir do front-end. A suite não apanhava isto porque o test client do Django não impõe
CSRF por omissão.

## ADR-16 — A password só se define pelo email, sem exceções (fecho da porta no `/update/`)

**Contexto.** O projeto tem uma regra explícita, construída em duas fases: ninguém escolhe a
password na criação (ADR-11) e ninguém a escreve por outrem (`users.views.users_change_password`,
cuja docstring diz *"Ninguém — nem o próprio, nem um admin — escreve aqui uma password... só
quem controla a caixa de correio a consegue definir"*). Só que `update_user` continuava a
aceitar `password` no corpo do pedido, e `users_update` só filtrava a chave `role` para
não-admins. A regra estava afirmada em dois sítios e contrariada num terceiro.

Consequência prática: um `commercial_grants` — que por desenho gere contas `viewer`/`client` —
fazia `PUT /users/<client_id>/update/ {"password": "..."}` e entrava na conta desse cliente.
Não havia teste nenhum a cobrir este caminho, o que confirma que era resíduo da versão
anterior do fluxo de password, não uma funcionalidade.

**Decisão.** `update_user` ignora `password` em silêncio, tal como `create_user` já fazia — a
password define-se **exclusivamente** pelo link assinado enviado por email
(`reset_password_with_token`). Ignorar em vez de rejeitar mantém a coerência com o resto do
módulo (um cliente que ainda envie o campo por hábito não vê o pedido falhar) e com o que
`users_change_password` já faz ao corpo que recebe.

Removeu-se também `service.change_password()`: deixou de ter chamadores quando
`users_change_password` passou a só disparar o email, e era o único sítio, além do
`update_user`, onde uma password entrava por parâmetro. Mantê-la seria guardar carregada a
mesma arma que se acabou de desarmar.

**Consequências.** Não há forma de um administrador "pôr uma password" a alguém — só de lhe
disparar o link. É o custo pretendido da regra: perder o atalho para ganhar a garantia de que
uma conta só é acedida por quem controla o respetivo email. Dois testes fixam a invariante
(`test_update_route_cannot_set_a_password`, com admin — o caso mais forte — e
`test_commercial_cannot_set_a_client_password_via_update`), para que a porta não volte a
abrir-se sem alguém dar por isso.

## ADR-17 — Embeddings dos anúncios: infraestrutura pronta, ainda sem consumidor

**Contexto.** Uma varredura de código morto (análise AST a cruzar definições com usos reais)
mostrou que `Notice.activity_embedding` é **escrito** — pelo comando `embed_notices`, que paga
uma chamada à OpenAI por anúncio e guarda um vetor de 1536 dimensões por linha — e nunca
**lido**: o único leitor seria `anuncios.embeddings.search_notices()`, que nenhuma view,
serviço ou comando chama. A listagem (`GET /anuncios/list/`) pesquisa com `icontains` em SQL,
não semanticamente. `ensure_notice_embedding()` está na mesma situação.

Ao contrário dos avisos — onde os embeddings são o núcleo da ordenação do match (ADR-4) — nos
anúncios a cadeia está construída até meio: gera-se e guarda-se, mas não se consulta.

**Decisão.** **Manter**, sem ligar e sem remover. A pesquisa semântica em anúncios está
prevista; o que existe é preparação deliberada, não esquecimento. Fica registado aqui
precisamente porque, sem registo, a próxima leitura do código (ou a próxima varredura de
código morto) volta a levantar a mesma dúvida e arrisca remover trabalho intencional.

**Consequências.** Há um custo a correr sem retorno ainda: cada execução de `embed_notices`
gasta OpenAI e o vetor ocupa espaço em cada linha de `Notice`. Enquanto não houver
consumidor, **não vale a pena correr o comando** — os embeddings só rendem quando alguém os
consultar. Quem ligar a pesquisa semântica no futuro tem tudo pronto: `search_notices()` já
delega em `match.embeddings.semantic_search` (pgvector), o mesmo motor dos avisos.

Na mesma passagem removeu-se `fetch_specifications` (`anuncios/specifications.py`), um wrapper
"retrocompatível" de `fetch_documents` que não tinha um único chamador em produção — só os seus
4 testes o mantinham vivo, e todos os cenários que cobriam já estão cobertos, de forma mais
estrita, por `FetchDocumentsTests` (que verifica as duas chaves do dicionário devolvido, não só
uma). Retrocompatibilidade sem consumidor é dívida técnica, não compatibilidade.
