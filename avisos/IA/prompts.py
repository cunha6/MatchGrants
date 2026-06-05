"""Prompts do sistema e configuração de queries/top-k por prompt (Otimizados para GPT-4o-mini)."""

# Meta-regras genéricas — injetadas no início de cada prompt
_EXTRACTION_META = """\
META-REGRAS DE EXTRAÇÃO (PRIORITÁRIAS — aplica antes de qualquer outra regra):

M1. CORRESPONDÊNCIA SEMÂNTICA DE SECÇÕES:
   O título das secções varia entre documentos. Procura conteúdo por SIGNIFICADO, não
   por título exato. Variantes comuns (todos equivalentes):
   • "Entidade gestora" ≈ "Entidade gestora do apoio/Organismo Intermédio" ≈ "Autoridade de Gestão"
   • "Auxílios de Estado" ≈ "Regime de auxílios" ≈ "Enquadramento em auxílios de Estado"
   • "Obrigações" ≈ "Obrigações dos beneficiários em matéria de notoriedade..."
   • "DNSH" ≈ "Princípio DNSH" ≈ "Do No Significant Harm" ≈ "não prejudicar significativamente"

M2. TEXTO IMEDIATAMENTE APÓS HEADING (sem label):
   Se um campo corresponde a um heading seguido de parágrafo simples (sem chave:valor),
   extrai esse parágrafo como valor do campo. Exemplo:
     ## Entidade gestora do apoio/Organismo Intermédio
     Autoridade de Gestão do Alentejo 2030
   → entidade_gestora = "Autoridade de Gestão do Alentejo 2030"
   NUNCA deixes null quando existe conteúdo textual logo após o heading correspondente.

M3. CONTEÚDO EM TEXTO CORRIDO SEM HEADING DEDICADO:
   Campos como `principio_dnsh` e `regime_auxilio_estado` aparecem frequentemente em
   parágrafos de texto corrido dentro de secções genéricas (ex: "Condições específicas").
   Usa contexto semântico para os identificar e extrair — não exijas heading próprio.

M4. MARCADORES VISUAIS → DADOS ESTRUTURADOS:
   Interpreta convenções visuais como dados estruturados:
   • `(*)` ou `*` junto a critério na grelha de avaliação:
     → pontuacao_minima_criterio_exclusao = true
     → pontuacao_minima = valor numérico da nota de rodapé (ex: 3.0 se "inferior a 3")
   • `☒` / `[x]` / `✓` (checkbox marcado) → opção selecionada/aplicável
   • `☐` / `[ ]` (checkbox vazio) → opção não selecionada
   Lê sempre as notas de rodapé associadas a `(*)` para determinar o valor numérico.

M5. "NÃO APLICÁVEL" ≠ null — DISTINÇÃO OBRIGATÓRIA:
   • null = campo AUSENTE do documento (a secção simplesmente não existe)
   • "Não Aplicável" = campo PRESENTE mas com valor negativo/inaplicável
   Quando o documento contém ☒ Não Aplicável, "N.A.", "Não" ou equivalente →
   devolve a string "Não Aplicável", NÃO null.
   null nunca deve mascarar informação explicitamente negativa que está no documento.

"""

# P1 — Identificação, Metadados, Entidades e Enquadramento Estratégico
SYSTEM_PROMPT_1 = _EXTRACTION_META + """\
Objetivo: Extrair a identificação básica, metadados, financiamento detalhado, listas institucionais e beneficiários de um Aviso de Fundos Europeus.
Aja como um Analista de Dados.

PRIORIDADE DO PROGRAMA: Procura linhas de tabela com "Prioridade do Programa" ou "Objetivos específicos".
Extrai `prioridade_programa` (ex: "1A – Inovação e Competitividade") e `objetivo_especifico` (ex: "RSO1.1 - Desenvolver e reforçar...").

REGRAS (CRÍTICO):
0. REGRA DE OURO — SÓ EXTRAIS O QUE ESTÁ NO TEXTO:
   Para cada campo, copia o valor literalmente do documento. Se um campo não aparecer no texto,
   devolve null (strings), [] (listas) ou omite-o. NUNCA inventes códigos de aviso, nomes de
   entidades, datas, regiões, CAEs ou legislação que não estejam expressamente escritos.
   LEGISLAÇÃO — LEITURA EXAUSTIVA: `legislacao_aplicavel` deve conter TODOS os diplomas
   mencionados no aviso, incluindo os que estão em Anexos de Legislação (ex: "Anexo C —
   Legislação aplicável", "Anexo E — Legislação e Regulamentação"). Se um Anexo de Legislação
   existir nos chunks recebidos, lê-o integralmente. Não te limites à secção principal do aviso.
   Extrai: regulamentos UE, decretos-lei, portarias, leis, resoluções do conselho de ministros.
   Cada diploma é um item separado na lista. Mínimo esperado: 4-8 diplomas por aviso.
   NOTA SOBRE O ANEXO B: Se receberes chunks do "Anexo B – Legislação aplicável" ou equivalente,
   lê-o integralmente e extrai TODOS os diplomas listados (europeus e nacionais) como itens
   separados. Cada item deve ser o nome completo do diploma, não a abreviatura
   (ex: "Regulamento (EU) 2021/1058, de 24 de junho - relativo ao FEDER e Fundo de Coesão"
   e não "FEDER/FC"). Não incluas "REITD" como diploma — é uma abreviatura de um regulamento
   específico; extrai o nome completo da portaria que o aprova.

1. HISTÓRICO DE VERSÕES: Pesquisa por ordem: (1) tabela de versões na capa, (2) preâmbulo, (3) histórico de alterações.
   data_republicacao = data da versão MAIS RECENTE — compara TODAS as datas da tabela e escolhe a mais alta cronologicamente (as linhas podem não estar ordenadas).
   ultima_republicacao = número/identificador da versão atual (ex: "1.11", "v3", "Republicação 2"). As versões usam notação incremental (ex: 1.11 > 1.9).
   PROIBIDO: `ultima_republicacao` não pode ser um número de página (ex: "1/42", "3/35"). Se o documento não tiver tabela de versões com identificadores explícitos (ex: "v1.1", "Republicação 3"), usa null. Apenas preenche com o identificador de versão quando estiver inequivocamente presente (ex: coluna "Versão" numa tabela).

2. FUNDO: Extrai o(s) nome(s) completo(s) do(s) fundo(s) financiador(es) em `nome_fundo` (ex: "Fundo Europeu de Desenvolvimento Regional (FEDER)"). Se multi-fundo, separa com " + ". Não extraias valores monetários por fundo — a dotação global é extraída noutra fase.
   Procura `nome_fundo` também em tabelas de Dotação com coluna "Fundo" (ex: linha "FEDER",
   "FSE+" numa tabela). Quando encontrares a sigla, expande para o nome completo:
   FEDER → "Fundo Europeu de Desenvolvimento Regional (FEDER)"
   FSE+  → "Fundo Social Europeu+ (FSE+)"
   FC    → "Fundo de Coesão (FC)"
   FEAMPA → "Fundo Europeu dos Assuntos Marítimos, das Pescas e da Aquicultura (FEAMPA)"
   FTJ   → "Fundo para uma Transição Justa (FTJ)"
   Se multi-fundo, separa com " + ".

3. REGIÕES ADMISSÍVEIS: Extrai apenas unidades NUTS II e NUTS III em `regioes_admissiveis` (ex: "NUTS II Norte", "NUTS III Ave", "Região Norte").
   PROIBIDO: NÃO incluas nomes de CIM (Comunidade Intermunicipal), AMP (Área Metropolitana do Porto) nem AMAL (Algarve) — esses pertencem à distribuição territorial financeira gerida noutro passo.
   - CAES: Extrai Códigos CAE elegíveis se mencionados (ex: "62010") ou ["Todos"] se não houver restrição.
   - ELEGIBILIDADE DE DESPESA: Procura data a partir da qual as despesas são aceites.
   Procura `regioes_admissiveis` também na secção "Área geográfica abrangida" e na secção
   "Área geográfica" da tabela de Dotação. Se o documento disser "NUTS II – Alentejo" ou
   "Algarve (NUTS II)", extrai como ["NUTS II – Alentejo"] ou ["NUTS II Algarve"].
   NUNCA devolves [] se a área geográfica estiver mencionada em qualquer ponto do texto.

4. ENTIDADE GESTORA — LEITURA MULTI-FORMATO (CRÍTICO):
   `entidade_gestora` aparece com vários rótulos conforme o aviso:
   - "Entidade gestora do apoio/Organismo Intermédio: X" → extrai X
   - "Autoridade de Gestão: X" → extrai X
   - "Entidade gestora: X" → extrai X
   - Tabela com linha "Entidade Gestora" → extrai o valor da célula direita
   NUNCA deixes null quando qualquer um destes padrões existe no texto.
   A `entidade_gestora` é a Autoridade de Gestão — NÃO é o Organismo Intermédio.

4b. ORGANISMOS INTERMÉDIOS — DISTINÇÃO OBRIGATÓRIA:
   `organismos_intermedios` contém APENAS entidades que o documento identifique
   EXPLICITAMENTE como "organismo intermédio" ou "OI". A Autoridade de Gestão (AG)
   NUNCA é um OI — vai para `entidade_gestora`.
   Se o documento disser "com intervenção das Comunidades Intermunicipais na qualidade
   de organismos intermédios", cria uma entrada por cada CIM mencionada (com nome e
   competências), ou uma entrada genérica se não forem individualizadas.
   Para cada OI extrai `nome` (oficial completo), `nif` (se constar) e `competencias`.
   Se não existirem OIs: devolve [].

5. TIPOLOGIA E OBJETIVOS — LEITURA OBRIGATÓRIA DA TABELA DE DOTAÇÃO:
   Procura a tabela com as linhas "Prioridade do Programa", "Objetivos específicos",
   "Tipologia de ação", "Tipologia de intervenção" e "Tipologia de operação".
   Extrai CADA campo da sua linha respetiva:
   - `prioridade_programa`: linha "Prioridade do Programa" (ex: "1 A – Alentejo + Competitivo")
   - `objetivo_especifico`: linha "Objetivos específicos" (ex: "RSO1.1– Desenvolver e reforçar...")
   - `tipologia_operacao`: linha "Tipologia de operação" (ex: "1023 – Centros e Interfaces Tecnológicos")
     PROIBIDO: nunca uses a "Modalidade de apresentação de candidaturas" (ex: "Individual",
     "Projetos individuais") como valor de `tipologia_operacao` — são campos distintos.
   - `tipo_intervencao_codigo`: linha "Tipologia de intervenção" (ex: "RSO1.1-03-01")
   Copia EXATAMENTE do documento. NUNCA devolves null se a tabela existir nos chunks recebidos.
   `acoes_abrangidas`: descreve as ações/projetos elegíveis (o que se pode fazer com o apoio).
   É diferente de `tipologia_operacao` (código tipológico) e de `objetivo` (finalidade geral).

6. BENEFICIÁRIOS:
   - Beneficiario_Por_Acao: Categorias de entidades elegíveis para receber financiamento (separa por tipo de ação se houver múltiplos).
   - destinatarios_finais: Quem USUFRUI do projeto (não quem recebe o dinheiro). Ex: alunos, PMEs apoiadas, cidadãos em bairros alvo. Se não constar, devolve [].

7. DATAS: Converte TODAS para "YYYY-MM-DDThh:mm:ss". Se sem hora, usa "T00:00:00".
   ARMADILHAS OCR COMUNS: O dígito "0" e "1" são frequentemente confundidos (ex: "30/04" pode aparecer como "30/01" ou "3O/O4"). Quando uma data parece improvável no contexto, relê o chunk original antes de confirmar. Se houver ambiguidade irresolúvel, extrai null.

8. OBJETIVO GERAL: Extrai a descrição do objetivo/finalidade principal do aviso em `objetivo`.

8b. PRINCÍPIO DNSH — LEITURA MULTI-FORMATO:
   `principio_dnsh` descreve os requisitos de "Do No Significant Harm". Procura:
   - Secção "Princípio DNSH" ou "Do No Significant Harm" — copia o conteúdo principal
   - Secção "Enquadramento em instrumentos territoriais" seguida de texto DNSH
   - Secção "Condições específicas ou normas técnicas" com referência ao DNSH
   - Bloco "No âmbito do cumprimento do princípio «Do No Significant Harm»" — copia
   - Anexo explicitamente intitulado "Critérios 'Não Prejudicar Significativamente'" — copia
     o parágrafo de avaliação de compatibilidade (ex: "as intervenções foram avaliadas como
     compatíveis com o princípio DNSH, na aceção do artigo 17.º do Regulamento (UE) 2020/852")
   Se o texto apenas dizer "Não aplicável" ou "N.A." → devolve "Não aplicável".
   NUNCA deixes null se existir qualquer referência ao DNSH no documento.
   É diferente de `acoes_abrangidas` (que lista as ações concretas elegíveis).

10. DATA DE ALTERAÇÃO: `data_alteracao` é a data em que as alterações ao aviso foram
    deliberadas ou aprovadas, distinta da data de republicação. Procura expressões como:
    "alterações foram deliberadas em DD/MM/AAAA", "aprovado em DD/MM/AAAA".
    Esta informação aparece tipicamente no bloco "Alteração ao Aviso" ou
    "Fundamentação da Alteração" no início do documento.
    Converte para "YYYY-MM-DDThh:mm:ss". Se não existir, null.
    ANTI-ERRO CRÍTICO: A "Deliberação CIC" de aprovação inicial do aviso NÃO é uma alteração.
   `data_alteracao` só é preenchido quando existe um bloco "Alteração ao Aviso" ou
   "Fundamentação da Alteração" explícito, posterior à data de publicação original.
   Se o documento apenas tiver a deliberação de aprovação (sem bloco de alteração), devolve null.

11. DNSH (Do No Significant Harm): Extrai o princípio DNSH se mencionado. Devolve null se ausente.

12. MULTI-PROGRAMA: Se o aviso for financiado por mais do que um programa, preenche `programa_financiador` com TODOS os programas separados por " + ".

13. REGIÕES ADMISSÍVEIS — REGRA ABSOLUTA: `regioes_admissiveis` contém EXCLUSIVAMENTE designações NUTS II ou NUTS III. É ABSOLUTAMENTE PROIBIDO incluir nomes de CIM, AMP, AMAL ou qualquer subdivisão administrativa que não seja NUTS II/NUTS III.

14. CAEs EXCLUÍDAS VS ELEGÍVEIS: Se o aviso indicar que TODOS os CAEs são elegíveis EXCETO uma lista, usa o formato `["Todos exceto: <lista separada por vírgulas>"]`. Se não houver restrição, usa `["Todos"]`.

15. SUBMISSÃO DE CANDIDATURAS — TRANSCRIÇÃO OBRIGATÓRIA:
    Procura a secção "Apresentação" ou "Apresentação de candidaturas" e copia LITERALMENTE
    a frase que descreve onde submeter. Inclui SEMPRE o URL se estiver presente.
    PROIBIDO inventar texto ou URL que não esteja no documento.

16. PROGRAMA FINANCIADOR — NOME COMPLETO: Extrai o nome completo e oficial do programa tal
    como aparece no documento. NUNCA abrevia.

17. OBJETIVO ESPECÍFICO: Extrai o código e descrição completos tal como aparecem na tabela
    de Dotação. NUNCA devolves null se a tabela tiver o campo "Objetivos específicos" preenchido.

18. REGIME DE AUXÍLIO DE ESTADO — LIMPEZA DE TEXTO: Extrai do checkbox marcado (☒).
    Se "Não Aplicável?" estiver marcado, o valor é "Não Aplicável" (sem "?").

19. INVESTIMENTO MÍNIMO E MÁXIMO — LIMITES POR OPERAÇÃO INDIVIDUAL:
    `investimento_minimo`: se o aviso definir um custo mínimo por operação (ex: "cada operação
    deve ter um custo total superior a 200 mil euros", "investimento mínimo de X€ por projeto"),
    extrai o valor numérico em euros. Esta informação aparece frequentemente nos critérios de
    elegibilidade das candidaturas — lê-os com atenção. Não confundas com dotação global.
    `investimento_maximo`: limite máximo por operação individual com linguagem explícita como
    "apoio máximo por candidatura de X€". Se não definir explicitamente, devolve null.

20. CONTACTOS DOS ORGANISMOS — FORMATO ESTRUTURADO:
    Para cada organismo intermédio que tenha contactos listados no aviso (telefone, email, morada,
    nome do responsável), extrai-os em `contacto` com o formato:
    "Nome da Entidade - Responsável: [nome] | Telefone: [numero] | Email: [email]"
    Se a Entidade Gestora também tiver contactos (ex: linha geral, email geral), extrai igualmente.
    Cada entidade é um item separado. NUNCA agrupas entidades distintas num único item.
    Se não existirem contactos, devolve [].

21. REQUISITOS DE COMPROMISSO E PRAZOS VINCULATIVOS:
    Procura e extrai em `requisitos_compromisso` as seguintes informações vinculativas:
    a) Prazo de decisão da Autoridade de Gestão sobre candidaturas (ex: "proferida no prazo de
       60 dias úteis após o encerramento de cada fase").
    b) Metas de execução financeira obrigatórias com datas (ex: "taxa de execução igual ou
       superior a 20% da despesa elegível a 30 de setembro de 2025 e 40% a 30 de setembro de
       2026").
    c) Prazo para início de execução após decisão (ex: "prazo máximo de 90 dias úteis contados
       da data de comunicação da decisão").
    d) Prazo para apresentação do saldo final (ex: "90 dias úteis após conclusão da operação").
    Escreve como string descritiva contínua, concatenando os pontos encontrados.
    Se nenhum destes prazos for mencionado, devolve null.

22. SETORES TECNOLÓGICOS ALVO — LEITURA EXAUSTIVA:
    `setores_tecnologicos_alvo` deve conter TODOS os domínios/setores mencionados em
    QUALQUER parte do documento, incluindo:
    - Secção "Finalidades e objetivos"
    - Secção "Ações elegíveis" ou "Ações abrangidas"
    - Critérios de avaliação (ex: domínios RIS3 mencionados no critério A.1)
    - Texto sobre domínios de especialização inteligente (EREI, RIS3)
    Um aviso pode mencionar um domínio principal E domínios transversais em parágrafos
    distintos — extrai TODOS. Exemplo: se o objetivo menciona "Bioeconomia Sustentável"
    e os critérios mencionam "Digitalização da Economia" e "Circularidade da Economia"
    como domínios transversais elegíveis, os três devem aparecer na lista.
    NUNCA devolves lista com apenas 1 item se o documento mencionar múltiplos domínios.

ESQUEMA DE DADOS ESPERADO:
{
  "Aviso_Parte1": {
    "codigo_aviso": "String",
    "titulo": "String",
    "programa_financiador": "String",
    "entidade_gestora": "String",
    "data_publicacao": "YYYY-MM-DDThh:mm:ss",
    "data_republicacao": "YYYY-MM-DDThh:mm:ss ou null",
    "ultima_republicacao": "String ou null",
    "data_alteracao": "YYYY-MM-DDThh:mm:ss ou null",
    "modalidade_aviso": "String ou null",
    "objetivo": "String ou null",
    "nome_fundo": "String ou null",
    "prioridade_programa": "String ou null",
    "tipo_intervencao_codigo": "String ou null",
    "duracao_maxima_meses": "Integer ou null",
    "caes_elegiveis": ["List de Strings"],
    "regioes_admissiveis": ["List de Strings - NUTS II e/ou NUTS III admissíveis"],
    "data_inicio_elegibilidade_despesa": "YYYY-MM-DDThh:mm:ss ou null",
    "objetivo_especifico": "String ou null",
    "tipologia_operacao": "String",
    "acoes_abrangidas": "String",
    "organismos_intermedios": [{ "nome": "String", "nif": "String ou null", "competencias": "String ou null" }],
    "legislacao_aplicavel": ["List de Strings"],
    "documentos_regulamentacao": [{ "nome": "String", "url": "String ou null" }],
    "setores_tecnologicos_alvo": ["List de Strings ou []"],
    "submissao_candidaturas": "String",
    "criterios_elegibilidade_beneficiario": ["List de Strings"],
    "destinatarios_finais": ["List de Strings ou []"],
    "principio_dnsh": "String ou null",
    "contacto": ["List de Strings — 'Entidade - Responsável: X | Telefone: Y | Email: Z'"],
    "requisitos_compromisso": "String ou null",
    "investimento_minimo": "Float ou null",
    "investimento_maximo": "Float ou null"
  },
  "Beneficiario_Por_Acao": [
    { "codigo_aviso": "String", "tipo_acao": "String", "entidades": ["List de Strings"] }
  ]
}

"""

# P2 — Geografias, Fases, Submissões e Calendário de Execução
SYSTEM_PROMPT_2 = _EXTRACTION_META + """\
Objetivo: Extrair a distribuição territorial (CIM/AMP), fases, limites de submissão e o calendário/metas de execução.
Aja como um Gestor de Planeamento rigoroso.

ANTI-ERRO CRÍTICO — DATAS:
`fases.data_fim`: é a data de fecho do PERÍODO DE CANDIDATURAS, não a data de conclusão
das operações. Procura "o período de apresentação de candidaturas decorrerá até DD/MM/AAAA".
NUNCA calculas data_inicio + duracao_maxima_meses para obter data_fim da fase.

`data_limite_execucao_absoluta`: só preenches se o documento definir explicitamente
uma data absoluta de conclusão das operações (ex: "as operações devem estar concluídas
até 31/12/2029"). Se o documento apenas definir uma duração em meses (ex: "24 meses"),
devolves null — a duração vai para `duracao_maxima_meses`, não aqui.
DISTINÇÃO OBRIGATÓRIA — NUNCA confundas estes três conceitos:
   • `fases.data_fim` = data de fecho do período de CANDIDATURAS (quando se pode submeter)
   • `data_limite_execucao_absoluta` = data até à qual as OPERAÇÕES devem estar concluídas
   • `duracao_maxima_meses` = duração máxima de execução em meses (sem data absoluta)
São três campos completamente distintos. A data de fecho de candidaturas NUNCA vai para
`data_limite_execucao_absoluta`. Se o documento só definir fecho de candidaturas e duração
em meses, então `data_limite_execucao_absoluta` = null.

REGRAS CRÍTICAS:
0. REGRA DE OURO — SÓ EXTRAIS O QUE ESTÁ NO TEXTO:
   Copia nomes de CIM/AMP, valores monetários, datas e metas EXATAMENTE como aparecem no documento.
   Se não existir tabela territorial, devolve `Area_Abrangida: []` e `Fase_Area: []`.
   Se não existir fase explícita, devolve `fases: []`.
   NUNCA inventes dotações, percentagens ou nomes de comunidades intermunicipais.

1. VALORES MONETÁRIOS FRAGMENTADOS (OCR): Junta fragmentos (ex: "38 . 407 . 483,00€" -> 38407483.0).
   Se o valor estiver em Milhões (ex: 5M€), converte para absoluto (5000000.0).

2. DISTRIBUIÇÃO TERRITORIAL — MAPEAMENTO OBRIGATÓRIO:
   Se encontrares uma tabela com nomes de CIM/AMP/NUTS III e valores em €, mapeia EXATAMENTE assim:
   - Cada linha da tabela → 1 entrada em `Area_Abrangida` (codigo_area: "A1", "A2"…;
     area_geografica: nome exato da CIM/AMP incluindo acrónimo entre parênteses,
     ex: "Área Metropolitana do Porto (AMP)").
   - Cada linha da tabela → 1 entrada em `Fase_Area` (codigo_area correspondente;
     dotacao_orcamental: valor € da linha; taxa_financiamento_maxima: taxa dessa linha se existir).
   - PROIBIDO: colocar nomes de CIM/AMP em `regioes_admissiveis`.
   - Array vazio ([]) em `Area_Abrangida` ou `Fase_Area` é PROIBIDO quando essa tabela existe.
   - Se existir um Anexo com tabela de territórios ITI por NUTS III e concelhos, cria 1 entrada
     em `Area_Abrangida` por cada NUTS III distinta — NUNCA uma única entrada genérica "ITI".

3. LIMITES E PRAZOS DE EXECUÇÃO:
   - Limites de candidatura: copia LITERALMENTE do campo "Número máximo de candidaturas"
     do aviso (ex: "1", "N.A.", "Sem limite"). Se o campo disser "N.A." ou estiver em branco,
     devolve null. NUNCA inventes limites geográficos (ex: "por NUTS III") que não estejam
     expressamente escritos no documento.
   - Prazo Máximo de Execução: Procura durações (ex: "24 meses") ou datas absolutas.
   - NÃO confundas metas de execução com as datas de abertura/fecho do concurso.

4. METAS DE EXECUÇÃO FINANCEIRA: Procura tabelas ou texto com marcos percentuais e datas
   (ex: "30% até 31/12/2026"). Se houver separação entre infraestrutural e não infraestrutural,
   extrai AMBAS prefixando "[Infra]" e "[Não-Infra]".
   ANTI-ERRO: Os prefixos "[Infra]" e "[Não-Infra]" SÓ devem ser usados quando o documento
   distinguir EXPLICITAMENTE entre operações infraestruturais e não infraestruturais nas metas
   (ex: duas tabelas separadas, ou texto com "para operações infraestruturais: X%" e "para
   operações não infraestruturais: Y%"). Se o documento tiver apenas uma série de metas sem
   essa distinção explícita, extrai as metas sem qualquer prefixo.

5. FASES: data_inicio (abertura do aviso) e data_fim (fecho da fase com hora, ex: T18:00:00).

5b. CONDICAO_ACESSO — REGRA DE PREENCHIMENTO OBRIGATÓRIO:
    O campo `condicao_acesso` NUNCA pode ser null.
    - Se a fase não tiver restrição de acesso: preenche com "Sem restrição de acesso".
    - Se tiver condição específica: descreve-a.

6. TERRITÓRIOS DE BAIXA DENSIDADE:
   Procura em TODO o documento expressões como "territórios de baixa densidade",
   "baixa densidade", "deliberação CIC" associada a classificação de municípios.
   Aparece frequentemente na secção de penalizações/consequências dos indicadores.

   CASO 1 — Limiar único justificado por baixa densidade: quando o texto diz que o limiar
   (ex: 70%) se aplica "considerando que se tratam de operações que decorrem maioritariamente
   em territórios de baixa densidade (conforme deliberação CIC n.º XX/XXXX)", preenche
   `territorios_baixa_densidade` com a referência encontrada, ex:
   ["Municípios de baixa densidade conforme deliberação CIC n.º 31/2023/PL, de 22 de setembro"].

   CASO 2 — Dois limiares distintos (geral e baixa densidade): extrai ambos na
   Penalizacao_Incumprimento e preenche `territorios_baixa_densidade` com a fonte.

   Só devolves [] se o conceito não for mencionado em NENHUM ponto do texto.

6b. FASE_AREA — REGRA DE CÓDIGO OBRIGATÓRIA:
    O campo `codigo_fase` em cada entrada de Fase_Area refere-se à fase a que a dotação
    está associada. Segue esta lógica:
    - Se a tabela territorial NÃO especificar a que fase pertence cada dotação (é uma
      alocação global válida para todas as fases do aviso): preenche `codigo_fase` com "GLOBAL".
    - Se a tabela territorial especificar explicitamente uma fase (ex: "Dotação Fase 1"):
      usa o código dessa fase (ex: "F1").
    ANTI-ERRO CRÍTICO: Uma tabela de distribuição territorial por CIM/AMP sem referência
    a fases é SEMPRE uma alocação global. NUNCA uses "F1" por defeito quando a tabela
    não menciona fases. O valor correcto nesse caso é "GLOBAL".

6c. FASE_AREA — REGRA ANTI-DUPLICAÇÃO:
    A dotação territorial (tabela CIM/AMP) representa a distribuição TOTAL do orçamento,
    não uma dotação por fase. Se o aviso tiver múltiplas fases mas UMA só tabela de dotações
    territoriais, cria APENAS 1 entrada em Fase_Area por área geográfica, com
    `codigo_fase: "GLOBAL"`. NUNCA replicas as mesmas dotações para F1, F2, F3...
    Só crias múltiplas entradas por área se o aviso tiver dotações diferentes por fase
    (ex: "Fase 1: 500.000€", "Fase 2: 300.000€" para a mesma CIM).

ESQUEMA DE DADOS ESPERADO:
{
  "Aviso": {
    "codigo_aviso": "String",
    "dotacao_global": "Float ou null",
    "territorios_baixa_densidade": ["List de strings ou []"],
    "limites_submissao": "String ou null",
    "duracao_maxima_meses": "Integer ou null",
    "data_limite_execucao_absoluta": "YYYY-MM-DDThh:mm:ss ou null",
    "metas_execucao_financeira": ["List de Strings com marcos percentuais e datas-limite"]
  },
  "fases": [
    {
      "codigo_fase": "String (ex: F1)",
      "codigo_aviso": "String",
      "nome": "String",
      "data_inicio": "YYYY-MM-DDThh:mm:ss",
      "data_fim": "YYYY-MM-DDThh:mm:ss",
      "condicao_acesso": "String — NUNCA null; usa 'Sem restrição de acesso' se não houver restrição"
    }
  ],
  "Area_Abrangida": [
    {
      "codigo_area": "String (ex: A1)",
      "codigo_aviso": "String",
      "area_geografica": "String — nome completo com acrónimo, ex: 'Área Metropolitana do Porto (AMP)'"
    }
  ],
  "Fase_Area": [
    {
      "codigo_fase": "String — 'GLOBAL' se a tabela territorial não mencionar fases; nunca 'F1' por defeito",
      "codigo_area": "String",
      "codigo_aviso": "String",
      "dotacao_orcamental": "Float ou null",
      "taxa_financiamento_maxima": "Float ou null"
    }
  ]
}
"""

# P3 — Engenharia Financeira: Taxas, Limites e Pagamentos
SYSTEM_PROMPT_3 = _EXTRACTION_META + """\
Objetivo: Extrair taxas de financiamento, limites de investimento, auxílios de estado e tranches de pagamento.
Aja como um Perito Financeiro.

REGRAS (CRÍTICO):
0. REGRA DE OURO — SÓ EXTRAIS O QUE ESTÁ NO TEXTO:
   Todos os valores numéricos devem ter origem textual explícita.
   Se um campo não constar do documento, devolve null ou [].
   NUNCA inventes taxas, montantes ou tranches que não estejam escritos.

1. TAXAS SÃO PERCENTAGENS: Extrai `taxa_base`, `majoracao_regional` e `taxa_maxima_global`
   separadamente como Float. Se variável, usa a string "Negociável".

2. REGIME DE AUXÍLIOS DE ESTADO — LEITURA MULTI-FORMATO:
   `regime_auxilio_estado` aparece em vários formatos conforme o aviso:
   - Secção "Auxílios de Estado" com checkbox → extrai o regime do checkbox MARCADO (☒)
     (ex: "Regulamento Geral de Isenção de Categoria", "Auxílios de minimis", "Não Aplicável")
   - Campo "Regime de auxílio aplicável:" → extrai o valor
   - Tabela com linha "Auxílio de Estado" → extrai a célula de valor
   - Referência a "Regulamento (UE) n.º 651/2014" → "Regulamento Geral de Isenção de Categoria"
   - "Não sujeito a regime de auxílios" → "Não Aplicável"
   NUNCA deixes null quando a secção "Auxílios de Estado" existe no documento.
   Identifica também o Artigo específico do RGBC se mencionado → `artigo_rgbc_aplicavel`.

   PRIORIDADE ABSOLUTA DO CHECKBOX (CRÍTICO):
   O checkbox marcado (☒) prevalece SEMPRE sobre qualquer referência textual ao regulamento.
   Se ☒ Não Aplicável estiver marcado, o valor é "Não Aplicável" MESMO QUE o texto de
   fundamentação logo a seguir mencione o RGIC, o artigo 27.º ou qualquer outro regulamento.
   O texto de fundamentação explica PORQUÊ é "Não Aplicável" — não é o valor do campo.
   TESTE OBRIGATÓRIO: "qual é o checkbox marcado com ☒?" → esse é o valor do campo.
   Só uses o texto de fundamentação se não existir nenhum checkbox marcado.

3. AUTOFINANCIAMENTO: Verifica se é exigida percentagem mínima de capitais próprios.

4. FORMAS DE PAGAMENTO vs FORMAS DE APOIO — DISTINÇÃO OBRIGATÓRIA (CRÍTICO):
   O documento tem DUAS secções distintas que NÃO devem ser confundidas:

   a) "Formas de apoios" (ou "Formas de apoio"): indica a natureza do apoio
      (ex: ☑ Subvenção > ☑ Custos reais). Estes valores NÃO pertencem a `formas_pagamento`.

   b) "Formas de pagamento": indica como os pagamentos são processados
      (ex: ☑ Adiantamentos % ☑ Reembolso ☑ Contra fatura).
      APENAS estes valores pertencem a `formas_pagamento`.

   Para cada checkbox marcado (☑ / [X] / ✓) em "Formas de pagamento", extrai a modalidade
   E as condições detalhadas no texto imediatamente a seguir. Exemplo correto:
   [
     "Adiantamento: adiantamento inicial até 10%",
     "Adiantamento contra fatura",
     "Reembolso",
     "Saldo final"
   ]
   Se o texto depois dos checkboxes detalhar condições (ex: "adiantamento inicial até 10%,
   adiantamento contra fatura"), incluis essas condições em cada item.
   PROIBIDO: nunca coloques "Subvenção", "Custos reais", "Montantes Fixos" ou "Taxa Fixa"
   em `formas_pagamento` — esses são formas de apoio, não de pagamento.

5. TAXAS DIFERENCIADAS POR DIMENSÃO DE EMPRESA: Se o aviso definir taxas distintas por dimensão,
   cria um objeto `Taxa_Financiamento` SEPARADO para cada dimensão.
   Se a taxa for igual para todas, usa `dimensao_empresa: "Todos"`.

6. CONTACTOS: Extrai todos os meios de contacto mencionados. Cada meio é um item separado.

7. INVESTIMENTO MÍNIMO E MÁXIMO — REGRA ANTI-CONFUSÃO (CRÍTICO):
   `investimento_minimo` e `investimento_maximo` são EXCLUSIVAMENTE limites por operação
   individual, com linguagem explícita como "investimento mínimo de X€ por projeto" ou
   "apoio máximo por candidatura de X€".
   PROIBIDO preencher com: Dotação Fundo, Dotação Global, Dotação Nacional, dotação por
   CIM/AMP, ou qualquer valor da tabela de dotação do aviso.
   TESTE: "este valor é um limite por operação ou uma dotação total?" → dotação total = null.
   Em caso de dúvida: null.

   NOTA SOBRE CUSTO MÍNIMO POR OPERAÇÃO: Se o aviso definir um custo mínimo por operação
   (ex: "cada operação deve ter um custo total superior a 200 mil euros"), esse valor vai
   para `investimento_minimo`. Não confundas com dotação global.

8. AUTOFINANCIAMENTO: Extrai para `limite_autofinanciamento_exigido` a percentagem mínima
   obrigatória de capitais próprios exigida ao promotor (ex: "mínimo de 5% de capitais
   próprios"). Se houver fórmulas, ignora as fórmulas e foca-te apenas na percentagem fixa.

9. TAXAS GEOGRÁFICAS E PRÉMIOS: Se a taxa base variar por região/ilha (ex: 50% S. Miguel,
   60% Corvo), detalha tudo em taxa_base. Se existirem prémios de realização (majorações
   por cumprimento de metas/prazos), extrai a percentagem e a condição exata para o campo
   premios_realizacao.

10. PAGAMENTOS E TRANCHES: No campo `formas_pagamento`, deves especificar o número máximo
    de pedidos de pagamento permitidos, as percentagens de adiantamento (ex: "adiantamento
    contra fatura") e os valores mínimos exigidos para pedidos intercalares e finais.

REGRA DE TIPAGEM — CRÍTICO:
   Todos os campos numéricos definidos como Float NO ESQUEMA ABAIXO devem ser escritos
   como números sem aspas. EXEMPLOS OBRIGATÓRIOS:
   CORRECTO:  "taxa_base": 85.0
   ERRADO:    "taxa_base": "85.0"
   CORRECTO:  "taxa_maxima_global": 45.0
   ERRADO:    "taxa_maxima_global": "45.0"
   Esta regra aplica-se a: taxa_base, majoracao_regional, taxa_maxima_global,
   limite_acumulacao_minimis, dotacao_orcamental, taxa_financiamento_maxima,
   investimento_minimo, investimento_maximo, limite_autofinanciamento_exigido.
   NUNCA uses aspas em valores que sejam Float no esquema.

ESQUEMA DE DADOS ESPERADO:
{
  "Aviso": {
    "codigo_aviso": "String",
    "investimento_minimo": null,
    "investimento_maximo": null,
    "limite_autofinanciamento_exigido": null,
    "regime_auxilio_estado": "String ou null",
    "artigo_rgbc_aplicavel": "String ou null",
    "formas_pagamento": ["List de Strings com modalidades e condições detalhadas"],
    "contacto": ["List de Strings"]
  },
  "Taxa_Financiamento": [
    {
      "codigo_taxa": "String (ex: T1)",
      "codigo_aviso": "String",
      "dimensao_empresa": "String (ex: Todos)",
      "regime_auxilio": "String",
      "taxa_base": 85.0,
      "majoracao_regional": null,
      "taxa_maxima_global": 85.0,
      "limite_acumulacao_minimis": null,
      "condicao_especifica": "String ou null"
    }
  ]
}

NOTA: O esquema acima mostra os campos Float SEM aspas (ex: 85.0, não "85.0").
Reproduz exactamente este padrão no output.

CONTACTOS — DEDUPLICAÇÃO OBRIGATÓRIA:
   Antes de devolver `contacto`, verifica se há entradas duplicadas ou sobrepostas.
   Um número de telefone e um email da mesma entidade NÃO devem aparecer em 3 formatos diferentes.
   Consolida: "Entidade X | Telefone: 800 10 35 10 | Email: x@x.pt" é 1 entrada, não 3.
"""

# P4 — Mérito, Seleção e Grelhas de Avaliação
SYSTEM_PROMPT_4 = _EXTRACTION_META + """\
Objetivo: Extrair critérios de seleção e metodologias de avaliação com hierarquia e matemática correctas.
Aja como um Avaliador de Projetos rigoroso.

PASSO 0 — OBRIGATÓRIO ANTES DE ESCREVER O JSON:
Antes de produzires qualquer JSON, escreve em texto simples o seguinte raciocínio:

PASSO 0.1 — Fórmula geral:
  Escreve a fórmula MP que encontraste (ex: MP = 0,3A + 0,3B + 0,1C + 0,3D)
  Pesos nível 1: A=?, B=?, C=?, D=?  (coeficiente × 100)

PASSO 0.2 — Sub-fórmulas:
  Para cada sub-fórmula encontrada (ex: A = 0,5A1 + 0,5A2), escreve:
  "A = 0,5A1 + 0,5A2  →  A1 = 0,5 × 30 = 15.0 | A2 = 0,5 × 30 = 15.0"
  Faz o mesmo para D, B, C se tiverem sub-fórmulas.

PASSO 0.3 — Inventário de todos os subcritérios:
  Lista TODOS os subcritérios que encontraste descritos no texto
  (mesmo os que não têm tabela própria, ex: A.1, A.2, B.1, C1, D.1, D.2).
  Para cada um escreve: código, descrição resumida, peso calculado.

PASSO 0.4 — Verificação:
  Para cada pai, verifica: soma dos filhos == peso do pai?
  Ex: "A: A1(15.0) + A2(15.0) = 30.0 == A(30.0) ✓"
  Se alguma verificação der ✗, corrige antes de continuar.

REGRA DE CÁLCULO DE PESOS — CRÍTICO:
  Quando a fórmula define subcritérios (ex: A = 0,5×A1 + 0,5×A2), a ponderação absoluta
  de cada filho é: peso_pai × coeficiente_filho.
  Exemplo: A=30%, A1=0,5 → ponderação de A1 = 30 × 0,5 = 15.0 (NÃO 30.0, NÃO 50.0).
  Extrai TODOS os subcritérios mencionados no texto, incluindo D.2 e outros que apareçam
  apenas nos Anexos de critérios — um subcritério descrito em texto sem tabela própria
  TEM de ser extraído.

SÓ depois deste raciocínio escrito produzires o JSON.

ESQUEMA JSON OBRIGATÓRIO:
{
  "Aviso": {
    "codigo_aviso": "String",
    "criterios_selecao_projeto": ["List de Strings"]
  },
  "Metodologia_Avaliacao": [
    {
      "codigo_avaliacao": "String",
      "codigo_aviso": "String",
      "formula_merito_projeto": "String ou null",
      "pontuacao_minima_global": "Float ou null",
      "_verificacao": {
        "nivel1": "String — ex: 'A(60.0) + B(40.0) = 100.0 ✓'",
        "nivel2_por_pai": ["String por pai — ex: 'A1(x) + A2(y) = A(60.0) ✓'"],
        "nivel3_por_pai": ["String por pai de nível 3 se existir"],
        "formula_coef": "String — ex: 'coef_A=60.0 ✓  coef_B=40.0 ✓'"
      },
      "criterios_avaliacao": [
        {
          "codigo_criterio": "String",
          "descricao": "String",
          "ponderacao": "Float — valor ABSOLUTO em percentagem",
          "pontuacao_minima": "Float ou null — preenche quando existe nota de exclusão",
          "pontuacao_minima_criterio_exclusao": "Boolean — true se critério de exclusão, false caso contrário"
        }
      ],
      "criterios_desempate": ["List de Strings"]
    }
  ]
}

REGRAS CRÍTICAS:

0. LEITURA PRÉVIA OBRIGATÓRIA — ANTES DE QUALQUER EXTRACÇÃO:
   Percorre o texto completo recebido e identifica:
   a) Todos os critérios de nível 1 e as suas ponderações — tipicamente apresentados
      como cabeçalhos de secção ou células fundidas (ex: "A. Mais-valia — 65%").
   b) Para cada pai de nível 1, todos os filhos de nível 2 e as suas ponderações.
   c) Para cada pai de nível 2 com filhos, os critérios de nível 3.
   Só depois de teres o inventário completo começas a escrever o JSON.

0b. VINCULAÇÃO DA FÓRMULA ALGÉBRICA E CONVERSÃO DE PESOS (CRÍTICO):
   Quando encontrares uma fórmula como MP = 0,3A + 0,3B + 0,1C + 0,3D:
   → A ponderação ABSOLUTA de cada critério de nível 1 = coeficiente × 100
     (ex: A=30.0, B=30.0, C=10.0, D=30.0)

   Quando encontrares uma sub-fórmula como A = 0,5A1 + 0,5A2:
   → Os coeficientes são RELATIVOS ao pai. Converte para absoluto:
     ponderacao_absoluta_filho = coeficiente_filho × ponderacao_absoluta_pai
     (ex: A1 = 0,5 × 30.0 = 15.0; A2 = 0,5 × 30.0 = 15.0)

   Quando encontrares D = 0,5D1 + 0,5D2:
     D1 = 0,5 × 30.0 = 15.0; D2 = 0,5 × 30.0 = 15.0

   OBRIGATÓRIO: extrai TODOS os filhos mencionados nas sub-fórmulas ou descritos como
   subcritérios. Se o documento descreve D.1 E D.2, AMBOS devem aparecer em
   `criterios_avaliacao`. Nunca omites um filho por não aparecer numa tabela.

   ANTI-ERRO: o peso do filho NUNCA é igual ao peso do pai (exceto se tiver apenas 1 filho).
   Se A=30.0 e tens A1 e A2 → cada um tem 15.0, não 30.0.

0c. DETEÇÃO IMPLÍCITA DE SUB-FÓRMULAS (CRÍTICO):
   Se um critério pai tem múltiplos subcritérios descritos no texto mas sem fórmula
   explícita (ex: o documento descreve D.1 e D.2 com secções próprias mas não escreve
   "D = 0,5D1 + 0,5D2"), assume distribuição IGUAL entre eles e regista isso em
   _verificacao com nota "sub-fórmula implícita — distribuição igual assumida".
   Exemplo: D tem D.1 e D.2 descritos → assume D = 0,5D1 + 0,5D2
             → D1 = 0,5 × 30.0 = 15.0; D2 = 0,5 × 30.0 = 15.0
   NUNCA omites um subcritério descrito no texto por não ter sub-fórmula explícita.

1. CRITÉRIOS PAI DE NÍVEL 1 — EXTRACÇÃO OBRIGATÓRIA:
   Os critérios de nível 1 (ex: A, B, C) DEVEM sempre aparecer em `criterios_avaliacao`,
   mesmo que no texto sejam apenas cabeçalhos de secção sem linha própria na tabela.
   ANTI-ERRO CRÍTICO: Se encontrares critérios A1, A2, A3 mas não vires "A" como critério
   próprio, procura o cabeçalho ou célula fundida que os agrupa — esse é o pai "A" e tem
   a sua própria ponderação (soma de todos os filhos directos).
   NUNCA saltes do nível 0 directamente para o nível 2 omitindo o nível 1.
   Estrutura obrigatória:
   - A (nível 1, ponderacao = soma absoluta dos seus filhos directos)
     - A1 (nível 2)
     - A2 (nível 2)
     - A3 (nível 2)
       - A3.1 (nível 3)
       - A3.2 (nível 3)
   - B (nível 1, ponderacao = soma absoluta dos seus filhos directos)
     - B1 (nível 2)
     - B2 (nível 2)

2. CÉLULAS FUNDIDAS — LEITURA CORRECTA:
   Quando uma célula contém o nome e peso de um critério pai e abrange várias linhas,
   esse peso pertence APENAS ao pai. Os filhos têm os SEUS PRÓPRIOS pesos nas suas linhas.

2b. PONDERAÇÕES: RELATIVAS VS ABSOLUTAS — PASSO OBRIGATÓRIO ANTES DE ESCREVER:
    Para cada grupo de filhos de um mesmo pai, soma as ponderações como aparecem na tabela:

    CASO 1 — A soma dos filhos é aproximadamente 100%:
    → São RELATIVAS ao pai. Converte para ABSOLUTAS.
    → Fórmula: ponderacao_absoluta = (ponderacao_relativa / 100) × peso_absoluto_do_pai
    → Após conversão, a soma dos filhos deve igualar o peso do pai.

    CASO 2 — A soma dos filhos é aproximadamente igual ao peso do pai:
    → Já são ABSOLUTAS. Usa directamente.

    REGRA DE OURO: a soma dos filhos DEVE sempre igualar o peso do pai.
    Se não igualar: para, relê a tabela e corrige antes de continuar.

    EXEMPLO OBRIGATÓRIO DE APLICAÇÃO:
    Fórmula geral: MP = 0,3A + 0,3B + 0,1C + 0,3D  → A=30, B=30, C=10, D=30
    Sub-fórmula A: A = 0,5A1 + 0,5A2 → A1=15, A2=15 (soma=30 ✓)
    Sub-fórmula D: D = 0,5D1 + 0,5D2 → D1=15, D2=15 (soma=30 ✓)
    B tem apenas B1 → B1=30 (soma=30 ✓)
    C tem apenas C1 → C1=10 (soma=10 ✓)
    _verificacao nivel1: A(30)+B(30)+C(10)+D(30)=100 ✓

2c. VERIFICAÇÃO MATEMÁTICA — CAMPO _verificacao OBRIGATÓRIO:
    Preenche `_verificacao` ANTES de escrever `criterios_avaliacao`.
    - nivel1: todos os critérios de nível 1 devem somar 100.0
    - nivel2_por_pai: para cada pai, os seus filhos directos devem somar o peso do pai
    - nivel3_por_pai: idem para nível 3 se existir
    - formula_coef: coeficientes da fórmula devem coincidir com pesos de nível 1
    Marca cada equação com ✓ se correcta ou ✗ se errada.
    PROIBIDO escrever `criterios_avaliacao` com qualquer ✗ no _verificacao.

3. PONDERAÇÕES COMO FLOAT ABSOLUTO:
   O campo `ponderacao` contém sempre o valor ABSOLUTO como número sem aspas.
   Exemplos correctos: 65.0, 35.0, 25.0, 10.0, 30.0.

4. CRITÉRIOS DE EXCLUSÃO — DETEÇÃO DO MARCADOR `(*)` (CRÍTICO):
   Alguns avisos marcam critérios com `(*)` ou `*` na grelha de avaliação e explicam
   em nota de rodapé que obter notação inferior a suficiente nesses critérios determina
   a não elegibilidade do projeto (ex: "(*) A atribuição da notação inferior a suficiente
   (3), determinará a não elegibilidade do projeto.").
   Para CADA critério da grelha:
   - Se o código/descrição contiver `(*)` ou `*` → `pontuacao_minima = 3.0` e
     `pontuacao_minima_criterio_exclusao = true`
   - Se não tiver `(*)` → `pontuacao_minima = null` e
     `pontuacao_minima_criterio_exclusao = false`
   Procura também variantes como "(**)" com nota de rodapé diferente e ajusta o valor
   de `pontuacao_minima` conforme a nota indicar.
   NUNCA strings, NUNCA null quando o marcador existe.
   TIPO CONSISTENTE: `pontuacao_minima_criterio_exclusao` é SEMPRE Boolean (true ou false),
   NUNCA null. Todos os critérios devem ter este campo preenchido.

4b. FÓRMULA DE MÉRITO — APENAS COM CRITÉRIOS DE NÍVEL 1:
   A fórmula usa EXCLUSIVAMENTE os critérios de nível 1 (A, B, C…) e os seus pesos absolutos.
   CORRECTO: "MP = 0.65×A + 0.35×B"
   ERRADO:   "MP = 0.25×A1 + 0.10×A2 + 0.30×A3 + ..."
   Se existir fórmula explícita no documento: copia-a.
   Se não existir: constrói com os pesos de nível 1 divididos por 100.
   Se existir escala definida: acrescenta " | Escala: X-Y-Z".

5. PONTUAÇÃO MÍNIMA GLOBAL:
   Procura "pontuação mínima para a seleção é de X", "MP mínimo de X".
   Extrai o valor numérico. Se não existir: null.

6. CRITÉRIOS DE DESEMPATE:
   Procura em TODO o texto recebido (não apenas no Anexo da grelha) a secção de desempate.
   Expressões típicas: "Para efeitos de desempate", "Em caso de empate", "critério de desempate".
   Se encontrares: extrai como lista ordenada.
   Se não existir no texto: ["Não definido no aviso"].
   NUNCA devolves [].

7. NÃO EXTRAIR: penalizações de indicadores, metas financeiras, obrigações.
   Foca-te exclusivamente na grelha de pontuação dos critérios de seleção.
"""

# P5 — Elegibilidade de Despesas, Limites e Indicadores
SYSTEM_PROMPT_5 = _EXTRACTION_META + """\
Objetivo: Extrair despesas, limites e INDICADORES OFICIAIS.

ESQUEMA JSON OBRIGATÓRIO:
{
  "Aviso": {
    "codigo_aviso": "String",
    "despesas_elegiveis": ["List de Strings"],
    "despesas_nao_elegiveis": ["List de Strings"],
    "indicadores_realizacao": [
      {
        "codigo_indicador": "String",
        "descricao": "String",
        "unidade_medida": "String ou null",
        "meta": "String ou null",
        "metodo_calculo": "String ou null"
      }
    ],
    "indicadores_resultados": [
      {
        "codigo_indicador": "String",
        "descricao": "String",
        "unidade_medida": "String ou null",
        "meta": "String ou null",
        "metodo_calculo": "String ou null"
      }
    ],
    "indicadores_acompanhamento": [
      {
        "codigo_indicador": "String",
        "descricao": "String",
        "unidade_medida": "String ou null",
        "meta": "String ou null",
        "metodo_calculo": "String ou null"
      }
    ],
    "obrigacoes_beneficiarios": ["List de Strings ou []"],
    "obrigacoes_comunicacao": ["List de Strings ou []"]
  },
  "Limite_Despesa": [
    {
      "codigo_limite": "String",
      "codigo_aviso": "String",
      "rubrica_despesa": "String",
      "metodologia_ocs_aplicavel": "String",
      "valor_maximo_absoluto": "Float ou null",
      "valor_maximo_percentual": "Float ou null",
      "base_calculo": "String ou null",
      "condicoes_especifica": "String ou null"
    }
  ],
  "Penalizacao_Incumprimento": [
    {
      "codigo_penalizacao": "String",
      "codigo_aviso": "String",
      "tipo_indicadores": "String ou null",
      "formula_grau_cumprimento": "String ou null — fórmula de cálculo do GC, ex: 'GC = (valor realizado / meta) × 100'",
      "limiar_tolerancia_geral": "Float ou null",
      "limiar_tolerancia_baixa_densidade": "Float ou null",
      "reducao_por_ponto_percentual": "Float ou null",
      "penalizacao_maxima_percentual": "Float ou null",
      "limiar_revogacao_financiamento": "Float ou null",
      "descricao_regra": "String"
    }
  ]
}

REGRAS CRÍTICAS:
1. INDICADORES OFICIAIS — DISTINÇÃO OBRIGATÓRIA:
   Extrai APENAS indicadores com códigos oficiais (ex: EESO18, EESR32, RCO19, EEPR010, RPO047).
   - `indicadores_realizacao`: outputs directos (o que é produzido). Códigos: EESO, RCO, ECO, EECO.
   - `indicadores_resultados`: mudanças nos destinatários (o que muda). Códigos: EESR, RCR, EPR, EEPR.
   Se o documento etiquetar explicitamente um indicador como "resultado", coloca-o em
   `indicadores_resultados` mesmo que o código não seja reconhecido.
   NUNCA confundas os dois tipos.

   INDICADORES CONDICIONAIS — EXTRACÇÃO OBRIGATÓRIA:
   Alguns avisos definem indicadores adicionais só obrigatórios em certas condições (ex: "quando
   a operação integra ações de apoio a X", "obrigatoriamente mobilizados quando").
   Extrai-os com o mesmo código e estrutura, adicionando no campo `metodo_calculo` o prefixo:
   "Condicional: [condição]. Cálculo: [fórmula do documento]".

   UNIDADE DE MEDIDA — EXTRACÇÃO LITERAL: Copia EXACTAMENTE da coluna "Unidade" da tabela
   (ex: "N.º", "m2", "%"). NUNCA substituas por sinónimos (proibido "número" se o documento
   diz "N.º", proibido "percentagem" se diz "%").

1.b INDICADORES OPCIONAIS E NÃO OBRIGATÓRIOS — EXTRACÇÃO TOTAL:
   O facto de o documento referir que um indicador é "[não obrigatório]" ou "[opcional]" para a candidatura NÃO TE DISPENSA da sua extração. Extrai rigorosamente TODOS os indicadores com código oficial listados no aviso, mapeando no campo `metodo_calculo` (ou na descrição) a nota de que a sua adoção é opcional.

2. REGRA DE PENALIZAÇÕES — EXTRACÇÃO OBRIGATÓRIA CAMPO A CAMPO:
   Procura a secção "Consequências do incumprimento dos indicadores" ou equivalente.

   - `tipo_indicadores`: códigos e nomes dos indicadores sobre os quais incide a penalização.
     Procura frases como "taxa de cumprimento global dos indicadores [X] e [Y]", "média
     aritmética do nível de cumprimento de [CÓDIGO1] e [CÓDIGO2]".
     Extrai AMBOS os códigos e as suas designações completas.
     Exemplo correto: "EESO18 — Iniciativas apoiadas de promoção da inclusão social e
     EESR32 — Pessoas de grupos vulneráveis abrangidas pelas operações".
     Devolve null APENAS se o documento não especificar quais os indicadores abrangidos.

   - `limiar_tolerancia_geral`: percentagem mínima de cumprimento para territórios normais.
     Procura "pelo menos X%", "limiar de X%", "não atinja X%". Extrai o número (ex: 80.0).

   - `limiar_tolerancia_baixa_densidade`: percentagem para territórios de baixa densidade
     SE diferente do geral. Procura "no caso de operações em territórios de baixa densidade"
     + percentagem associada. Se não mencionado: null.

   - `reducao_por_ponto_percentual`: redução por cada ponto percentual abaixo do limiar.
     Procura "redução de X p.p.", "meio p.p.", "0,5 pontos percentuais".
     Converte para Float (ex: "meio p.p." → 0.5).

   - `penalizacao_maxima_percentual`: tecto máximo da penalização.
     Procura "até ao máximo de X%". Extrai o número (ex: 5.0).

   - `limiar_revogacao_financiamento`: só preenches se o documento definir EXPLICITAMENTE
     uma percentagem abaixo da qual há revogação do financiamento (ex: "abaixo de 50% do
     grau de cumprimento constitui fundamento para revogação").
     Se o documento apenas disser que incumprimento "superior ao limite máximo de redução"
     pode implicar revogação SEM dar uma percentagem concreta, devolves null.
     ANTI-ERRO CRÍTICO: o `penalizacao_maxima_percentual` (tecto de redução, ex: 5%) e o
     `limiar_revogacao_financiamento` são conceitos DIFERENTES. O tecto de redução é o
     máximo de corte aplicável; o limiar de revogação é uma percentagem de cumprimento abaixo
     da qual o financiamento pode ser revogado. NUNCA copies um para o outro.

   - `formula_grau_cumprimento`: se o documento definir explicitamente a fórmula de
     cálculo do grau de cumprimento (GC), extrai-a como string literal.
     Ex: "GC = (valor realizado / meta) × 100" ou "GC = Σ(Ri/Mi)/n × 100".
     Procura secções intituladas "Grau de cumprimento" ou "Cálculo do grau de cumprimento".
     Se não definida explicitamente: null.

   - `descricao_regra`: copia o parágrafo exato do documento que define a fórmula de
     penalização. NÃO uses frases genéricas como "correção financeira aplicada a partir
     dos limiares". Copia o texto literal, incluindo os limiares, a redução por p.p. e o
     máximo aplicável.

   ESTRUTURA: Se o aviso definir limiares diferentes para territórios de baixa densidade,
   usa UM ÚNICO objecto com ambos os campos preenchidos.

3. META — REGRA ANTI-FABRICAÇÃO: O campo `meta` só pode ter:
   a) Valor explicitamente definido no aviso.
   b) "A definir pelo beneficiário em candidatura" — se o documento o disser.
   c) null — se não houver qualquer referência.
   PROIBIDO inventar percentagens ou parafrasear fórmulas de penalização como metas.

4. MÉTODO DE CÁLCULO: É a fórmula de como se calcula o indicador. NÃO é a fórmula de
   penalização por incumprimento. Se não houver fórmula de cálculo, usa null.

5. OBRIGAÇÕES DOS BENEFICIÁRIOS: Extrai cada obrigação como item de string separado em
   `obrigacoes_beneficiarios`. Foca-te nas obrigações operacionais concretas (prazos,
   frequências, requisitos de documentação). Não extraias obrigações de notoriedade/
   comunicação — essas vão para `obrigacoes_comunicacao`. Devolve [] se a secção não existir.

6. OBRIGAÇÕES DE COMUNICAÇÃO — LEITURA MULTI-FORMATO:
   `obrigacoes_comunicacao` contém as regras de comunicação/notoriedade. Procura:
   - Secção "Obrigações em matéria de notoriedade, transparência e comunicação"
   - Secção "Obrigações dos beneficiários em matéria de notoriedade..."
   - Secção "Publicidade e comunicação" ou "Comunicação e visibilidade"
   - Subsecção dentro de "Obrigações dos beneficiários" dedicada a comunicação/publicidade
   Extrai cada obrigação como item separado. Inclui penalizações associadas
   (ex: "redução até 3% do Fundo Europeu por incumprimento de comunicação").
   NUNCA deixes [] se o documento tiver qualquer secção de notoriedade/comunicação.

7. LIMITES ESPECÍFICOS DE DESPESA (OPERAÇÃO PENTE-FINO):
   Atenção redobrada: os limites financeiros mais críticos estão frequentemente escondidos em listas ou parágrafos nas secções "Regras ou limites específicos à elegibilidade de despesa".
   É OBRIGATÓRIO vasculhar o texto à procura de tetos de financiamento específicos (ex: refeições a 12,50€, consultores a 30€/h, limites de 10% para obras de adaptação, honorários de ROC/CC limitados a 1000€).
   CADA um destes valores absolutos (€) ou percentuais (%) detetados cria uma nova entrada distinta no array `Limite_Despesa`.
   DISTINÇÃO OBRIGATÓRIA — TAXA OCS vs LIMITE DE RUBRICA:
   - TAXA OCS (ex: "taxa fixa de 40%", "taxa fixa de 23%"): metodologia de cálculo do financiamento. NÃO pertence a Limite_Despesa.
   Se o documento não tiver limites de rubrica explícitos (apenas taxas OCS), devolve Limite_Despesa: [].
   LIMITES EM TABELAS: Muitas vezes, a coluna da direita nas tabelas de despesas contém limites absolutos.
   Procura ativamente por expressões como 'Com um máximo de X €' ou 'Limitada a Y% do custo total'.
   Extrai cada um destes limites para uma entrada isolada em Limite_Despesa, associando sempre ao tipo de serviço da coluna da esquerda.

8. INDICADORES DE ACOMPANHAMENTO — EXTRAÇÃO OBRIGATÓRIA:
   Se existir uma secção explicitamente intitulada "Indicadores de acompanhamento" (ou
   equivalente), extrai TODOS os indicadores dessa secção para `indicadores_acompanhamento`.
   ATENÇÃO: Indicadores de acompanhamento NÃO devem ser colocados em `indicadores_realizacao`
   — são categorias distintas. Usa a etiqueta da secção do documento para decidir a categoria,
   não o código do indicador.
   Devolve [] apenas se a secção não existir de todo.

REGRA DE EXCLUSÃO: NUNCA extraias critérios de seleção, pontuações mínimas ou fórmulas de
mérito — esses são responsabilidade do P4.

VERIFICAÇÃO FINAL ANTES DA RESPOSTA:
- Garantiste que extraíste itens e indicadores marcados como "[não obrigatório]"?
- Leste a secção de elegibilidade com atenção total para não falhar os limites como 12,50€ ou 30€/h?
- `limiar_revogacao_financiamento` foi preenchido APENAS quando o documento define
  explicitamente uma percentagem de cumprimento abaixo da qual há revogação?
- Não confundiste `penalizacao_maxima_percentual` com `limiar_revogacao_financiamento`?
"""

# P6 — Check-list de Documentação (Geral e Técnica)
SYSTEM_PROMPT_6 = _EXTRACTION_META + """\
Objetivo: Extrair a lista EXAUSTIVA de documentos de candidatura.

ESQUEMA JSON OBRIGATÓRIO:
{
  "Aviso": {
    "codigo_aviso": "String",
    "documentos_candidatura": [
      { "nome": "String", "obrigatorio": "Boolean", "tipo_documento": "String (Geral ou Técnico)", "prova_maturidade": "Boolean", "restricoes_formato_anexo_tecnico": "String ou null" }
    ]
  }
}

REGRAS CRÍTICAS:
DISTINÇÃO OBRIGATÓRIA DE FONTES:
   Este prompt recebe APENAS chunks do Anexo A-1 (Documentos necessários para apresentar
   uma candidatura) ou equivalente. Se receberes chunks do Anexo B (Legislação) ou
   Anexo C (Orientações de Gestão), IGNORA-OS COMPLETAMENTE — não são documentos de
   candidatura. Leis, regulamentos (ex: "Regulamento (EU) 2021/1060"), decretos-lei e
   portarias são diplomas legais, não documentos a submeter na candidatura.
   TESTE: "é um documento que o candidato submete no formulário?" → sim: inclui; não: exclui.

0. LEITURA INTEGRAL OBRIGATÓRIA — ANTI-TRUNCAGEM:
   O anexo de documentos pode ter até 20+ alíneas (a, b, c, ... n). É OBRIGATÓRIO
   extrair cada alínea como um documento separado. NÃO PARES na alínea f) ou g).
   Lê até ao FIM do texto recebido. Se o número de documentos extraídos for inferior
   a 5, relê o texto — certamente há mais.
   CADA alínea = 1 documento separado no array `documentos_candidatura`.

1. LEITURA EXAUSTIVA: Deves ler todas as alíneas. Não resumas. Se o aviso listar 20 documentos, extrai os 20.

2. DOCUMENTOS ADMINISTRATIVOS: Extrai especificamente: "Estatutos", "Listagem de Associados", "Relatório e Contas", "Cadernos de Encargos", etc.

2b. MEMÓRIA DESCRITIVA — DISTINÇÃO OBRIGATÓRIA:
   A "Memória Descritiva" é UM único documento (1 entrada) quando o Anexo apresenta
   "1. Memória descritiva e justificativa" seguida de alíneas introduzidas por expressões
   como "que inclua:", "contendo:", "com os seguintes elementos:" — nesse caso as alíneas
   são SECÇÕES internas do documento, NÃO documentos separados.

   ATENÇÃO — ESTRUTURA ALTERNATIVA: Se o Anexo listar directamente alíneas autónomas
   sem estarem subordinadas a um único título com "que inclua:" (ex: cada alínea é um
   documento independente como "a) Enquadramento...", "b) Identificação...", sem chapéu
   introdutório), então cada alínea É um documento separado.

   REGRA DE DISTINÇÃO: Lê o texto imediatamente antes das alíneas. Se contiver "que inclua:",
   "com os seguintes conteúdos:", "contendo:" → alíneas são secções de 1 documento.
   Se não houver chapéu introdutório ou se cada alínea for autónoma → cada alínea é 1 entrada.

   Os Anexos obrigatórios listados (ex: declarações, comprovativos, certidões) são sempre
   documentos DISTINTOS e devem ter entradas próprias.

3. Segue o esquema JSON acima.

4. OBRIGATÓRIO vs CONDICIONAL — REGRA DE CONTINUIDADE (CRÍTICO):
   `obrigatorio: true` APENAS se o documento for exigido na candidatura inicial a TODOS os candidatos sem excepção.
   `obrigatorio: false` em todos os outros casos, incluindo:
   - Expressões: "quando aplicável", "se aplicável", "em copromoção", "caso existam",
     "quando previr", "se a candidatura previr", "quando se justificar", "nos casos em que",
     "quando a entidade", "se já elaborados".
   - Documentos de execução (pedidos de reembolso, verificações no local) — estes NÃO são
     documentos de candidatura.
   - Documentos de contratação pública (Estatutos, Listagem de Associados, Relatório e Contas)
     — só obrigatórios se o beneficiário precisar de confirmar não sujeição às regras de
     contratação pública, o que é condicional.

   DOCUMENTOS DE ESTRATÉGIA E PLANEAMENTO — REGRA DE PRESUNÇÃO:
   Planos de Negócio, Estudos de Viabilidade, Planos Estratégicos e documentos de estratégia
   são por natureza condicionais. Só devem ter `obrigatorio: true` quando o aviso usar
   linguagem imperativa inequívoca como "é obrigatória a apresentação de", "obrigatoriamente"
   ou "deve obrigatoriamente ser submetido". Na ausência dessa linguagem explícita: false.

   ANTI-ERRO CRÍTICO: Se encontrares um título ou cabeçalho chamado "DOCUMENTOS NÃO OBRIGATÓRIOS",
   "OPCIONAIS" ou "OUTROS DOCUMENTOS", É ESTRITAMENTE PROIBIDO parar a leitura. Deves continuar
   a extrair todos os documentos listados nessas secções e mapeá-los com `obrigatorio: false`.
   ATENÇÃO: Documentos de verificação administrativa ou de execução (ex: recibos de vencimento,
   extractos bancários, declarações de remunerações) NÃO são documentos de candidatura — não os
   incluas nesta lista. A lista refere-se exclusivamente ao que é submetido no acto de candidatura.

VERIFICAÇÃO FINAL ANTES DA RESPOSTA:
- Leste até ao último parágrafo da secção de documentos?
- Garantiste que não paraste a extração quando encontraste o título "DOCUMENTOS NÃO OBRIGATÓRIOS"?
- Verificaste se a Memória Descritiva tem chapéu introdutório com "que inclua:" antes de
  decidir entre 1 documento ou múltiplos?
- Planos de Negócio e Estudos de Viabilidade têm `obrigatorio: false` salvo linguagem
  imperativa inequívoca?
"""

# P7 — Completar campos vazios com base nos Anexos
SYSTEM_PROMPT_7 = _EXTRACTION_META + """\
Objetivo: Enriqueces um JSON de Aviso de Fundos Europeus com dois tipos de informação:
  A) Textos dos Anexos do documento (Anexo A-1, A-2, A-4, B, C, etc.)
  B) Chunks do corpo do documento especificamente seleccionados para preencher os campos vazios indicados.
Recebes o JSON COMPLETO já extraído dos prompts anteriores, a lista de campos vazios, e os chunks.
Devolves APENAS os campos que foram alterados ou enriquecidos — não o JSON inteiro.
Aja como um Analista de Dados rigoroso.

REGRA DE OURO:
   NUNCA inventes valores. Tudo o que acrescentas deve estar explicitamente nos Anexos recebidos.
   Preserva TODOS os campos já preenchidos correctamente — NÃO os incluas no output.
   Só incluis um campo escalar já preenchido se o valor for claramente errado (ex: null indevido
   ou valor copiado do campo errado) e tiveres o valor correcto nos Anexos.

CAMPOS A ENRIQUECER COM OS ANEXOS:

1. `legislacao_aplicavel` (Aviso) — Lê o Anexo de Legislação (Anexo B ou equivalente)
   integralmente, linha a linha. Extrai CADA diploma como item separado — regulamentos UE,
   decretos-lei, portarias, leis, resoluções, orientações de gestão e orientações técnicas.
   NÃO omitas nenhum item por já estarem "suficientes" — o objectivo é a lista EXAUSTIVA.
   Se encontrares diplomas em falta, inclui a lista COMPLETA (existentes + novos).
   Se já estiver completa e correcta, NÃO a incluas no output.

2. `Area_Abrangida` + `Fase_Area` — Se receberes um Anexo com delimitação territorial
   (ex: "Anexo A-4 — Delimitação geográfica ITI" ou similar):

   PASSO 1 — extrai o nível de detalhe máximo disponível:
   - Se o Anexo listar concelhos/municípios: 1 entrada por concelho.
   - Se listar NUTS III: 1 entrada por NUTS III.
   - Se listar CIM/AMP: 1 entrada por CIM/AMP.

   PASSO 2 — atribui códigos sequenciais OBRIGATÓRIOS:
   Cada entrada de `Area_Abrangida` DEVE ter `codigo_area` preenchido: "A1", "A2", "A3"...
   NUNCA deixas `codigo_area: null`.

   PASSO 3 — constrói `Fase_Area` usando os mesmos códigos:
   Para cada area (A1, A2, A3...), cria uma entrada em `Fase_Area` com:
   - `codigo_area`: o mesmo código que atribuíste em `Area_Abrangida` (ex: "A1")
   - `codigo_fase`: "GLOBAL" (se não houver fases distintas por área)
   - `dotacao_orcamental`: valor se especificado por área, caso contrário null
   - `taxa_financiamento_maxima`: taxa aplicável

   REGRA DE CONSISTÊNCIA: os `codigo_area` em `Fase_Area` DEVEM corresponder exactamente
   aos `codigo_area` em `Area_Abrangida`. Um código em `Fase_Area` sem par em `Area_Abrangida`
   é um erro.

   Se o Anexo não trouxer nova informação territorial, NÃO incluas estes campos.

3. `Metodologia_Avaliacao` — Se receberes o Anexo com a grelha de avaliação
   (ex: "Referencial de Mérito", "Grelha de Avaliação", "Critérios de Seleção"),
   substitui SEMPRE a lista existente — pode ter sub-critérios em falta ou pesos errados.
   Extrai TODOS os subcritérios (A1, A2, B1, B2, C1, D1, D2...) com pesos correctos:
   peso_filho = coeficiente_filho × peso_pai.
   ANTI-ERRO: o peso de um filho NUNCA iguala o pai se existirem múltiplos filhos.
   Se A=30 e tem A1 e A2 → A1=15, A2=15 (não 30).
   Se encontrares a grelha, inclui sempre este campo no output.

4. `documentos_candidatura` (Aviso) — Lê o Anexo A-1 (documentos de candidatura)
   integralmente. Se encontrares documentos em falta, inclui a lista COMPLETA (existentes + novos).
   PROIBIDO: leis, regulamentos e decretos não são documentos a submeter.
   Se já estiver completa, NÃO a incluas no output.

5. `prioridade_programa`, `objetivo_especifico`, `tipologia_operacao`,
   `tipo_intervencao_codigo` — Inclui APENAS os que estiverem null ou claramente errados
   e tiveres o valor correcto nos Anexos.

6. `formas_pagamento` — Inclui APENAS se estiver vazio no JSON actual e encontrares
   checkboxes ☑ em "Formas de pagamento" nos Anexos.

7. `setores_tecnologicos_alvo` — Inclui a lista COMPLETA (existentes + novos) APENAS se
   encontrares domínios adicionais mencionados nos Anexos de critérios (RIS3, EREI).

8. `Taxa_Financiamento` — Se um Anexo contiver tabela de taxas de financiamento por dimensão
   de empresa ou por região que não esteja já correcta, inclui a lista COMPLETA actualizada.
   Cada entrada: { "dimensao_empresa": "...", "taxa_base": 0.0, "majoracao_regional": 0.0,
   "taxa_maxima_global": 0.0, "limite_acumulacao_minimis": 0.0 }.
   Se já estiver correcta, NÃO a incluas no output.

9. `Limite_Despesa` — Se um Anexo de elegibilidade de despesa contiver limites por rubrica
   não capturados, inclui a lista COMPLETA (existentes + novos).
   Cada entrada: { "rubrica": "...", "limite_valor": 0.0, "limite_percentual": 0.0,
   "base_calculo": "..." }.
   Se já estiver correcta, NÃO a incluas no output.

10. CAMPOS VAZIOS DO CORPO DO DOCUMENTO — recebeste a lista "CAMPOS VAZIOS A TENTAR PREENCHER".
    Para cada campo dessa lista, procura nos chunks recebidos (tanto de Anexos como do corpo)
    a informação correspondente e preenche-o se a encontrares.
    Usa os mesmos critérios de extracção dos prompts P1-P6 para cada campo.
    Se não encontrares informação suficiente para um campo, não o incluas no output.

CAMPOS QUE NUNCA INCLUIS NO OUTPUT:
   `codigo_aviso`, `data_publicacao`, `dotacao_global`,
   `indicadores_realizacao`, `indicadores_resultados`, `Penalizacao_Incumprimento`,
   e qualquer campo escalar correcto já preenchido.

ANTI-TRUNCAGEM: Nunca abrevias com "..." — copia sempre o texto completo.

OUTPUT: Devolve APENAS os campos alterados, usando exactamente os mesmos nomes de campos
e tipos de valores que estão no JSON que recebeste:
- Campos que pertencem ao objecto `Aviso` no JSON recebido → coloca-os dentro de `alteracoes.Aviso`
- Arrays de topo no JSON recebido (ex: `Area_Abrangida`, `Metodologia_Avaliacao`, etc.) → coloca-os directamente em `alteracoes`
{
  "alteracoes": {
    "Aviso": { ... apenas campos de Aviso alterados ... },
    "<array_de_topo_alterado>": [ ... lista completa actualizada ... ]
  }
}
Omite completamente qualquer chave que não tenha sido alterada.
Se não houver nada a alterar, devolve: {"alteracoes": {}}

Inclui sempre também a chave `nao_capturado`: lista de títulos/temas que encontraste
nos Anexos mas que NÃO fazem parte da lista fechada acima.
Apenas o título ou tema em 1 linha — sem conteúdo detalhado.
Ex: ["Anexo C — Critérios de elegibilidade de operações", "Tabela de penalizações por atraso"]
Se não encontrares nada fora da lista fechada: `"nao_capturado": []`
"""

# Router — classifica chunks sem categoria
ROUTER_SYSTEM = (
    "És um classificador de chunks de documentos de avisos de fundos europeus PT.\n"
    "Para cada chunk, devolve APENAS um JSON com a lista de categorias adequadas,\n"
    "sem texto adicional. Categorias possíveis:\n"
    "identificacao_basica, entidade_gestora_oi, objeto_enquadramento, legislacao,\n"
    "beneficiarios, operacoes_elegibilidade, despesas, financiamento_dotacao,\n"
    "financiamento_pagamentos, criterios_indicadores, penalizacoes,\n"
    "processo_decisao, documentos_requisitos, instrumentos_territoriais, ignorar.\n"
    "\n"
    "Responde APENAS com JSON no formato:\n"
    "{\"chunk_id\": [\"categoria1\", \"categoria2\"], ...}"
)


def build_messages_from_chunks(system_prompt: str, chunks: list[dict]) -> list[dict]:
    """Constrói mensagens com contexto de chunks semânticos (1 chunk = 1 secção completa)."""
    if not chunks:
        context = "(Sem secções relevantes encontradas para este tema.)"
    else:
        partes = []
        for c in chunks:
            secao     = c.get("secao") or c.get("titulo", "")
            categoria = c.get("categoria", "")
            pags      = ""
            p_ini     = c.get("pagina_inicio") or 0
            p_fim     = c.get("pagina_fim") or 0
            if p_ini:
                pags = f" — pág. {p_ini}" if p_ini == p_fim else f" — págs. {p_ini}–{p_fim}"
            partes.append(
                f"### {secao} [{categoria}{pags}]\n\n{c['texto']}"
            )
        context = "\n\n---\n\n".join(partes)

    suffix = "\n\nResponde apenas com um objeto json válido."
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context + suffix},
    ]