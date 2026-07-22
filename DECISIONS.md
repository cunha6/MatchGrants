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

## ADR-6 — Rotação de chaves da API nif.pt

**Contexto.** A API nif.pt tem limites por chave. O projeto dispõe de várias chaves
(`NIF_KEY`, `NIF_KEY1..4`).

**Decisão.** *Round-robin* ao nível do processo: cada consulta usa a chave seguinte, uma de cada
vez. O índice é global e protegido por *lock* (seguro entre *threads* do gunicorn). Lê-se
`settings.NIF_KEYS` a cada chamada para respeitar `override_settings` nos testes.

**Consequências.** Reparte a carga pelas chaves sem coordenação externa. Uma chave explícita
(ex. nos testes) contorna a rotação.

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
