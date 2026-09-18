"""Calls do portal Funding & Tenders da Comissão Europeia (SEDIA).

Três tabelas:
  * `ProgrammeType` — catálogo dos programas-quadro (Horizon Europe, LIFE, ...), alimentado
    pela faceta `frameworkProgramme` da API. Serve de FILTRO das calls por programa.
  * `CallType`      — catálogo dos tipos de call (Grant, Calls for proposals, Cascade funding),
    alimentado pela faceta `type`. Serve de FILTRO das calls por tipo.
  * `Call`          — a call em si.

Os dois catálogos existem porque a API devolve apenas o CÓDIGO em cada call
(`frameworkProgramme: ["43108390"]`, `type: ["1"]`); o nome legível ("Horizon Europe
(HORIZON)", "Grant") só vem no endpoint /facet. Sem estas tabelas a listagem mostrava
números ao utilizador e não havia por onde filtrar.
"""

from django.conf import settings
from django.db import models


class ProgrammeType(models.Model):
    """Programa-quadro (faceta `frameworkProgramme`): código → nome legível.

    `code` é o `rawValue` da faceta e é o valor que aparece no campo `frameworkProgramme`
    de cada call — daí ser a chave do upsert e o alvo da FK.
    """

    code = models.CharField(max_length=50, unique=True)   # rawValue, ex: "43108390"
    name = models.TextField()                             # value, ex: "Horizon Europe (HORIZON)"
    # Nº de documentos que a API reporta para este programa. É o total do portal (todos os
    # tipos e estados), NÃO o número de calls guardadas nesta BD — serve só como ordenação
    # natural do catálogo (os programas maiores primeiro).
    total_count = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-total_count", "name"]
        verbose_name = "Programa-quadro"
        verbose_name_plural = "Programas-quadro"

    def __str__(self):
        return self.name


class CallType(models.Model):
    """Tipo de call (faceta `type`): código → nome legível.

    Só os tipos importados interessam — 1 (Grant), 2 (Calls for proposals) e 8 (Cascade
    funding calls). O tipo 0 (Tender) está deliberadamente de fora do import: tem uma
    metadata completamente diferente (lots, CPV, entidade adjudicante) que não cabe neste
    modelo.
    """

    code = models.CharField(max_length=20, unique=True)   # rawValue, ex: "1"
    name = models.TextField()                             # value, ex: "Grant"
    total_count = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "Tipo de call"
        verbose_name_plural = "Tipos de call"

    def __str__(self):
        return self.name


class CodeLabel(models.Model):
    """Código → nome legível para as restantes dimensões da API (destino, missão, zona
    geográfica, tipo de contrato, divisão do programa...).

    A API devolve estas dimensões só como números (`destination: ["48080639"]`,
    `mission: ["44798839"]`) e os nomes vivem no endpoint /facet. Em vez de uma tabela por
    dimensão — seriam oito iguais — há uma só, com `facet` a dizer de qual se trata.
    `ProgrammeType` e `CallType` continuam à parte: são os filtros principais e têm FK própria.
    """

    facet = models.CharField(max_length=60, db_index=True)   # ex: "destination", "mission"
    # 255 e não 60: em `programmeDivision` o rawValue nem sempre é um número — há entradas
    # que trazem o próprio nome ("Food, Bioeconomy Natural Resources, Agriculture and
    # Environment", 63 caracteres), e 60 rebentava o import com DataError.
    code = models.CharField(max_length=255)                  # rawValue
    label = models.TextField()                               # value (nome legível)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # O mesmo código pode existir em facetas diferentes, por isso a chave é o par.
        constraints = [
            models.UniqueConstraint(fields=["facet", "code"], name="uniq_codelabel_facet_code"),
        ]
        ordering = ["facet", "label"]
        verbose_name = "Código traduzido"
        verbose_name_plural = "Códigos traduzidos"

    def __str__(self):
        return f"{self.facet}:{self.code} = {self.label}"


class Call(models.Model):
    """Uma call do portal Funding & Tenders.

    Chave do upsert: `reference` (o campo `reference` da API, ex: "50132976TOPICSen").
    NÃO é o `identifier`: nas cascade funding calls (tipo 8) o mesmo `identifier` aparece em
    várias linhas distintas — 20 linhas da amostra traziam só 10 identifiers —, por isso um
    unique sobre `identifier` perdia calls reais. A API chega a repetir a mesma `reference`
    em duas linhas (33 grupos em 1341 na verificação inicial, praticamente todos idênticos);
    como é a MESMA call repetida, o upsert por `reference` limita-se a reescrevê-la em vez
    de criar um duplicado.

    A metadata completa fica em `raw`: os três tipos partilham o essencial, mas cada um traz
    campos próprios (topicConditions, destinationDetails, budgetOverview...) que não vale a
    pena promover a coluna. As colunas dedicadas são as que servem de filtro/ordenação.
    """

    STATUS_FORTHCOMING = "31094501"
    STATUS_OPEN = "31094502"
    STATUS_CLOSED = "31094503"
    STATUS_CHOICES = [
        (STATUS_FORTHCOMING, "Forthcoming"),
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    ]

    reference = models.CharField(max_length=255, unique=True)

    # `identifier` é o código público da call (ex: "HORIZON-MSCA-2027-COFUND-01-01"). É o que
    # o utilizador reconhece e procura, mas NÃO é único (ver docstring) — daí indexado e não unique.
    identifier = models.CharField(max_length=255, db_index=True)
    title = models.TextField(blank=True, default="")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True)
    call_type = models.ForeignKey(
        CallType, on_delete=models.PROTECT, related_name="calls", null=True, blank=True,
    )
    programme = models.ForeignKey(
        ProgrammeType, on_delete=models.PROTECT, related_name="calls", null=True, blank=True,
    )

    # Identificação da call-mãe (várias calls/topics partilham a mesma). Ausente em parte dos
    # tipos 2 e 8, daí o blank/default em vez de null (evita ter de tratar None na listagem).
    call_identifier = models.CharField(max_length=255, blank=True, default="", db_index=True)
    call_title = models.TextField(blank=True, default="")

    start_date = models.DateTimeField(null=True, blank=True, db_index=True)
    # PRÓXIMO prazo. A API devolve `deadlineDate` como lista e 136 das 1341 calls verificadas
    # traziam vários prazos (submissão em cascata); esta coluna guarda o mais próximo no futuro
    # (ou o último, se já passaram todos) para ordenar/filtrar, e a lista inteira fica em
    # `deadlines` — sem isso, ordenar por prazo numa call multi-prazo dava o resultado errado.
    deadline_date = models.DateTimeField(null=True, blank=True, db_index=True)
    deadlines = models.JSONField(default=list, blank=True)
    deadline_model = models.CharField(max_length=100, blank=True, default="")

    programme_period = models.CharField(max_length=100, blank=True, default="")
    url = models.TextField(blank=True, default="")
    description = models.TextField(blank=True, default="")
    keywords = models.JSONField(default=list, blank=True)

    # --- Orçamento (só nas calls que o publicam: 553 das 1339) -------------------------
    # A API manda isto dentro de `budgetOverview`, que é uma STRING com JSON lá dentro e
    # cobre a call-mãe INTEIRA (todos os topics irmãos). Somar tudo dava valores absurdos
    # — 4,6 mil milhões numa call de 17 M€ —, por isso só se guarda a entrada cujo `action`
    # começa pelo `identifier` desta call. Verificado: as 553 têm entrada própria.
    # Nos tipos 2 e 8 vem do campo `budget` da API, já pronto. ATENÇÃO: aí a Comissão não
    # normaliza a escala — 5 calls (0,4%) publicam valores como "1.2" ou "6.8" que serão
    # milhões, e "560" que tanto pode ser euros como milhares. Guarda-se o valor TAL COMO
    # vem: inventar um multiplicador daria números errados com ar de certos. Quem mostra
    # isto deve assinalar montantes implausivelmente baixos em vez de os apresentar a seco.
    budget_total = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True, db_index=True,
    )
    min_contribution = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    max_contribution = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    expected_grants = models.IntegerField(null=True, blank=True)

    # Tipo de ação ("HORIZON Innovation Actions", "HORIZON Research and Innovation Actions"...).
    # É o critério por que os comerciais distinguem calls dentro do mesmo programa.
    types_of_action = models.TextField(blank=True, default="", db_index=True)
    # Prioridades transversais ("AI", "DigitalAgenda", "SSH"...) — lista, serve de filtro.
    cross_cutting_priorities = models.JSONField(default=list, blank=True)

    # --- Dimensões traduzidas (código da API → nome, via CodeLabel) --------------------
    # Guarda-se o NOME já resolvido, não o código: a listagem mostra-o sem mais queries, e
    # um código que a faceta deixe de trazer continua legível no que já foi importado.
    destination = models.TextField(blank=True, default="")          # 659 calls
    destination_group = models.TextField(blank=True, default="")    # 637
    mission = models.TextField(blank=True, default="", db_index=True)  # 25
    geographical_zones = models.JSONField(default=list, blank=True)  # 23 (tipo 2)
    contract_type = models.TextField(blank=True, default="")         # 23 (tipo 2)
    specific_objective = models.TextField(blank=True, default="")    # 23
    programme_divisions = models.JSONField(default=list, blank=True)  # 1140 — área/cluster

    # Tags temáticas livres (2276 distintas em 317 calls): "Artificial intelligence",
    # "security", "STEP-Defence"... É o vocabulário mais fino que a API publica.
    tags = models.JSONField(default=list, blank=True)

    # Documentos oficiais da call (guias de candidatura, anexos). 255 ficheiros em 23 calls,
    # cada um com {name, type, language, date, url} — links diretos de download.
    documents = models.JSONField(default=list, blank=True)

    # Projeto-mãe das cascade funding calls (quem redistribui o financiamento).
    project_name = models.TextField(blank=True, default="")
    project_acronym = models.CharField(max_length=255, blank=True, default="")
    project_id = models.CharField(max_length=50, blank=True, default="")
    duration = models.TextField(blank=True, default="")
    # Nome da call-mãe / entidade (187 calls dos tipos 2 e 8).
    ca_name = models.TextField(blank=True, default="")

    # Aceita pedidos de parceria no portal (553 calls) — útil para o comercial.
    allow_partner_search = models.BooleanField(null=True, blank=True)
    # Última atualização DO LADO DA COMISSÃO (não confundir com `updated_at`, que é nosso).
    source_updated_at = models.DateTimeField(null=True, blank=True)

    # =====================================================================================
    # CAMPOS CRUS DA API — uma coluna por campo tal como a API o envia, sem transformação
    # (nomes traduzidos de camelCase para snake_case, valores copiados tal como vêm).
    #
    # A API devolve cada resultado como {..., "metadata": {...}} — MAS 5 calls (tipo 8,
    # "COMPETITIVE_CALL") não têm a chave "metadata" de todo: os mesmos campos aparecem
    # soltos no nível de topo do resultado. O sync lê cada campo com fallback (topo ou
    # metadata, o que existir) para estas colunas ficarem sempre preenchidas da mesma forma,
    # independentemente de onde a API o pôs.
    #
    # Os campos que JÁ tinham coluna semântica acima (status, title, url, deadlineDate...)
    # não se repetem aqui — seria a mesma informação duas vezes. As colunas abaixo são as
    # que só existiam dentro do `raw` até agora: nomes de campo do motor de busca
    # (es_*, checksum, apiVersion...), estruturas ainda por descodificar (actions,
    # budgetOverview) e as tags internas do catálogo (ccmTags, cenTagsA...).
    # =====================================================================================

    # --- Envelope do motor de busca (metadados técnicos da SEARCH API, não da call) -------
    api_version = models.CharField(max_length=20, blank=True, default="")       # "2.155"
    checksum = models.CharField(max_length=128, blank=True, default="")
    content = models.TextField(blank=True, default="")            # = summary, na prática
    content_type = models.CharField(max_length=50, blank=True, default="")      # "text/plain"
    database = models.CharField(max_length=50, blank=True, default="")          # "SEDIA"
    database_label = models.CharField(max_length=50, blank=True, default="")    # "SEDIA"
    group_by_id = models.CharField(max_length=20, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    citation = models.TextField(blank=True, default="")
    weight = models.FloatField(null=True, blank=True)
    access_restriction = models.BooleanField(null=True, blank=True)
    # Sempre [] / {} nas 1339 calls verificadas, mas fazem parte do envelope da API.
    children = models.JSONField(default=list, blank=True)
    highlighted_fragments = models.JSONField(default=list, blank=True)
    enriched_metadata = models.JSONField(default=dict, blank=True)

    # --- Rastreio interno do motor de busca (es_*, esDA_*, esST_*, DATASOURCE) ------------
    es_datasource = models.CharField(  # "DATASOURCE" (maiúsculas) OU "datasource" — nunca as duas
        max_length=50, blank=True, default="",
    )
    es_combine = models.CharField(max_length=10, blank=True, default="")        # "es_Combine"
    es_content_type = models.CharField(max_length=50, blank=True, default="")   # "es_ContentType"
    es_sort_date = models.DateTimeField(null=True, blank=True)                  # "es_SortDate"
    es_checksum = models.CharField(max_length=128, blank=True, default="")      # "esST_checksum"
    es_filename = models.CharField(max_length=50, blank=True, default="")       # "esST_FileName"
    es_url = models.TextField(blank=True, default="")                          # "esST_URL"
    es_first_ingest_date = models.DateTimeField(null=True, blank=True)         # "esDA_FirstIngestDate"
    es_ingest_date = models.DateTimeField(null=True, blank=True)               # "esDA_IngestDate"
    es_queue_date = models.DateTimeField(null=True, blank=True)                # "esDA_QueueDate"
    corporate_search_version = models.CharField(max_length=20, blank=True, default="")
    sort_status = models.CharField(max_length=10, blank=True, default="")  # "sortStatus"

    # --- Estruturas ainda em JSON string na API (descodificadas no detalhe, guardadas cruas
    #     aqui tal como vieram — ver services.serialize_call_detail para a versão legível) --
    actions = models.TextField(blank=True, default="")            # string com JSON
    budget_overview = models.TextField(blank=True, default="")    # string com JSON
    links_raw = models.TextField(blank=True, default="")          # "links" — string com JSON
    latest_infos_raw = models.TextField(blank=True, default="")   # "latestInfos" — idem

    # --- Campos de texto crus adicionais (têm coluna tratada acima; estes são a fonte tal
    #     como veio, sem HTML sanitizado nem fallback aplicado) -----------------------------
    description_byte = models.TextField(blank=True, default="")   # "descriptionByte"
    topic_conditions_raw = models.TextField(blank=True, default="")  # "topicConditions" — HTML
    destination_details_raw = models.TextField(blank=True, default="")  # HTML cru
    destination_description_raw = models.TextField(blank=True, default="")
    beneficiary_administration_raw = models.TextField(blank=True, default="")
    further_information_raw = models.TextField(blank=True, default="")
    sep_template = models.TextField(blank=True, default="")       # instruções de submissão
    support_info_raw = models.TextField(blank=True, default="")
    language = models.CharField(max_length=10, blank=True, default="")
    focus_area = models.JSONField(default=list, blank=True)       # sempre [] nas 1339 verificadas
    currency = models.CharField(max_length=10, blank=True, default="")  # só no tipo 2, "EUR"
    closing_date_raw = models.CharField(max_length=40, blank=True, default="")  # "closingDate"
    update_date_raw = models.CharField(max_length=40, blank=True, default="")   # "updateDate"

    # --- Códigos crus por trás das colunas já traduzidas (destination, mission, etc. acima
    #     guardam o NOME; estes guardam o CÓDIGO exatamente como a API o deu) --------------
    destination_code = models.CharField(max_length=50, blank=True, default="")
    destination_group_code = models.CharField(max_length=50, blank=True, default="")
    mission_code = models.CharField(max_length=50, blank=True, default="")
    mission_group_code = models.CharField(max_length=50, blank=True, default="")
    geographical_zone_code = models.CharField(max_length=50, blank=True, default="")  # sing.
    contract_type_code = models.CharField(max_length=50, blank=True, default="")
    specific_objective_code = models.CharField(max_length=50, blank=True, default="")
    programme_division_codes = models.JSONField(default=list, blank=True)
    programme_division_prospect_code = models.CharField(max_length=50, blank=True, default="")
    type_of_mgas = models.JSONField(default=list, blank=True)     # "typeOfMGAs" — lista de códigos
    cft_id = models.CharField(max_length=50, blank=True, default="")

    # --- Vocabulário interno do catálogo (CCM), distinto das `tags` legíveis acima --------
    ccm_tags = models.JSONField(default=list, blank=True)         # "ccmTags"
    ccm_tags2 = models.JSONField(default=list, blank=True)        # "ccmTags2"
    es_in_ccm_tags = models.JSONField(default=list, blank=True)   # "esIN_ccmTags"
    es_in_ccm_tags2 = models.JSONField(default=list, blank=True)  # "esIN_ccmTags2"
    cen_tags_a = models.JSONField(default=list, blank=True)       # "cenTagsA"

    # Metadata completa tal como veio da API — o resultado INTEIRO (envelope + metadata),
    # sem qualquer transformação. Fica como rede de segurança para o que não tiver coluna
    # própria acima (ex: campos futuros da API) e para reconstituir o pedido original.
    raw = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Última vez que o sync viu esta call na resposta da API. Uma call que deixa de aparecer
    # (fechou) mantém-se na BD com um `last_seen_at` antigo, em vez de desaparecer sem rasto.
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # Origem da ÚLTIMA escrita nesta call: 'api' (sync_calls, ver services.py) ou 'manual'
    # (PUT/PATCH em /calls/<id>/edit/). Mesmo padrão de avisos.Grant/anuncios.Notice — regista
    # quem tocou por último, sem impedir a escrita seguinte: um sync a seguir a uma edição
    # manual sobrescreve os campos que a API trouxer, tal como o scrape faz aos avisos.
    SOURCE_API = "api"
    SOURCE_MANUAL = "manual"
    LAST_UPDATE_SOURCE_CHOICES = [(SOURCE_API, "API (sync)"), (SOURCE_MANUAL, "Manual")]
    last_update_source = models.CharField(
        max_length=10, choices=LAST_UPDATE_SOURCE_CHOICES, default=SOURCE_API, db_index=True,
    )
    # Utilizador que fez a última edição MANUAL; None quando a última escrita foi do sync.
    last_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        ordering = ["-start_date", "identifier"]
        indexes = [
            models.Index(fields=["status", "-start_date"]),
            models.Index(fields=["programme", "status"]),
        ]

    def __str__(self):
        return f"{self.identifier} · {self.title[:60]}"

    @property
    def is_open(self) -> bool:
        return self.status == self.STATUS_OPEN
