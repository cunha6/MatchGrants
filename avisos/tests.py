"""Testes da app avisos — a lógica pura (classificação de documentos, heurísticas de
consolidação, deteção de convites, parsing de datas/dotações/montantes), os parsers dos
scrapers com HTML de fixture (SEM rede), a desativação por data de fecho e a view de
edição (permissões + validação + auditoria).

Os scrapers são testados só na camada de parse: o HTML é sintético e o `_pdf_info`
(que faz pedidos HTTP) é substituído por mock.
"""

import json
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from common.dates import parse_date, parse_datetime
from common.text import normalize
from users.models import UserProfile

from .db_service import _parse_allocation
from .Docling.converter import text_is_invitation
from .documents import (
    AMENDMENT, ANNEX, BASE, OTHER, PRORROGATION, RECTIFICATION, REPUBLICATION,
    amendment_ordinal, classify_document, is_consolidated_markdown,
    needs_consolidation, order_documents,
)
from .IA.pydantic_models import _float
from .models import Grant
from .service import deactivate_expired_grants
from . import scrape_compete, scrape_portugal, scrape_prr

# Password dos utilizadores de teste — lida do ambiente (.env), nunca hardcoded.
TEST_PASSWORD = os.environ.get("TEST_USER_PASSWORD", "test-only-password")


class NormalizeTests(SimpleTestCase):
    """common.text.normalize — fonte única de normalização de texto."""

    def test_accents_case_and_whitespace(self):
        self.assertEqual(normalize("  Águas   Vivas\n\tLda "), "aguas vivas lda")

    def test_none_and_empty(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(""), "")

    def test_non_string_input(self):
        self.assertEqual(normalize(123), "123")


class ParseDateTests(SimpleTestCase):
    """common.dates.parse_date — todos os formatos que os avisos/anúncios usam."""

    def test_iso(self):
        self.assertEqual(parse_date("2026-09-30"), date(2026, 9, 30))
        self.assertEqual(parse_date("2026-09-30T18:00:00Z"), date(2026, 9, 30))

    def test_pt_format(self):
        self.assertEqual(parse_date("30/09/2026"), date(2026, 9, 30))
        self.assertEqual(parse_date("30-09-2026 18:00:00"), date(2026, 9, 30))

    def test_embedded_in_sentence(self):
        self.assertEqual(parse_date("até 30/09/2026 às 18h"), date(2026, 9, 30))

    def test_ymd_with_slash(self):
        self.assertEqual(parse_date("2026/09/30"), date(2026, 9, 30))

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date("brevemente"))
        self.assertIsNone(parse_date("32/13/2026"))  # data impossível

    def test_parse_datetime_keeps_time(self):
        dt = parse_datetime("2026-04-30T15:00:00")
        self.assertEqual((dt.hour, dt.minute), (15, 0))
        self.assertIsNone(parse_datetime("sem data"))


class ClassifyDocumentTests(SimpleTestCase):
    """Classificação por nome: anexos primeiro, tipos específicos, base por palavra inteira."""

    def test_annex_variants(self):
        self.assertEqual(classify_document("Anexo A-2 Lista de Atividades"), ANNEX)
        self.assertEqual(classify_document("Guia de apoio ao preenchimento"), ANNEX)
        # "Anexo ... alterações climáticas" NÃO pode virar alteração ao aviso.
        self.assertEqual(classify_document("Anexo — adaptações às alterações climáticas"), ANNEX)

    def test_specific_types(self):
        self.assertEqual(classify_document("Prorrogação do prazo de candidaturas"), PRORROGATION)
        self.assertEqual(classify_document("Declaração de retificação do aviso"), RECTIFICATION)
        self.assertEqual(classify_document("1ª Republicação do Aviso"), REPUBLICATION)
        self.assertEqual(classify_document("Aviso consolidado"), REPUBLICATION)
        self.assertEqual(classify_document("3ª Alteração ao Aviso X"), AMENDMENT)
        self.assertEqual(classify_document("Alteração ao aviso n.º 1"), AMENDMENT)

    def test_base_requires_whole_word(self):
        self.assertEqual(classify_document("Aviso ALG-2026-10"), BASE)
        self.assertEqual(classify_document("AAC 01/2026"), BASE)
        # "aviso" colado dentro de outra palavra não conta.
        self.assertEqual(classify_document("CustosElegiveisAviso"), OTHER)

    def test_convite_is_other(self):
        self.assertEqual(classify_document("Convite à apresentação de candidatura"), OTHER)
        self.assertEqual(classify_document("Aviso X", nature="convite"), OTHER)

    def test_amendment_ordinal(self):
        self.assertEqual(amendment_ordinal("3ª Alteração"), 3)
        self.assertEqual(amendment_ordinal("2024-14_7.ª Alt"), 7)
        self.assertEqual(amendment_ordinal("1a Republicação"), 1)
        self.assertEqual(amendment_ordinal("Aviso base"), 0)

    def test_order_documents(self):
        docs = [
            {"name": "Aviso base", "type": BASE},
            {"name": "2ª Alteração", "type": AMENDMENT},
            {"name": "1ª Republicação", "type": REPUBLICATION},
            {"name": "5ª Alteração", "type": AMENDMENT},
        ]
        ordered = [d["name"] for d in order_documents(docs)]
        self.assertEqual(ordered, ["1ª Republicação", "5ª Alteração", "2ª Alteração", "Aviso base"])


class ConsolidationHeuristicsTests(SimpleTestCase):
    """Um diff puro precisa de consolidação; um documento completo (template) não."""

    DIFF_MD = "O ponto 3 do Aviso passa a ter a seguinte redação: «novo texto»."
    FULL_MD = (
        "# Designação do aviso\n...\n# Apoio para\n...\n# Dotação\n...\n"
        "# Custos elegíveis\n...\n# Critérios de seleção\n...\n"
    )

    def test_pure_diff_needs_consolidation(self):
        self.assertTrue(needs_consolidation(self.DIFF_MD))

    def test_full_template_does_not(self):
        self.assertTrue(is_consolidated_markdown(self.FULL_MD))
        # Mesmo com linguagem de diff, um documento completo já está consolidado.
        self.assertFalse(needs_consolidation(self.FULL_MD + "\n" + self.DIFF_MD))

    def test_plain_text_does_not(self):
        self.assertFalse(needs_consolidation("Texto normal sem alterações."))


class TextIsInvitationTests(SimpleTestCase):
    """Deteção precisa de convites: lê o campo 'Natureza do aviso', não a palavra solta."""

    def test_natureza_convite(self):
        self.assertTrue(text_is_invitation("Natureza do aviso: Convite à apresentação"))

    def test_natureza_concurso(self):
        self.assertFalse(text_is_invitation("Natureza do aviso: Concurso para apresentação"))

    def test_mentioning_convite_elsewhere_is_not_invitation(self):
        text = "Natureza do aviso: Concurso. O convite mencionado no ponto 3 não se aplica."
        self.assertFalse(text_is_invitation(text))

    def test_direct_phrase(self):
        self.assertTrue(text_is_invitation("Convite à apresentação de candidaturas"))

    def test_empty(self):
        self.assertFalse(text_is_invitation(""))


class ParseAllocationTests(SimpleTestCase):
    """Dotação do HTML: pontos = milhares, vírgula = decimal."""

    def test_pt_amounts(self):
        self.assertEqual(_parse_allocation("1.500.000,50 €"), 1500000.5)
        self.assertEqual(_parse_allocation("3.000.000€"), 3000000.0)

    def test_numeric_passthrough(self):
        self.assertEqual(_parse_allocation(2500000), 2500000.0)

    def test_invalid(self):
        self.assertIsNone(_parse_allocation(""))
        self.assertIsNone(_parse_allocation(None))
        self.assertIsNone(_parse_allocation("n/a"))


class FloatCoercionTests(SimpleTestCase):
    """Coercer _float dos modelos Pydantic: formatos PT e montantes por extenso."""

    def test_thousands_dots(self):
        self.assertEqual(_float("300.000"), 300000.0)
        self.assertEqual(_float("3.000.000"), 3000000.0)

    def test_decimal_formats(self):
        self.assertEqual(_float("85,5%"), 85.5)
        self.assertEqual(_float("4705882.34"), 4705882.34)
        self.assertEqual(_float("1.000,50"), 1000.5)

    def test_textual_amounts(self):
        self.assertEqual(_float("25 milhões euros"), 25_000_000.0)
        self.assertEqual(_float("300 mil"), 300_000.0)
        self.assertEqual(_float("2 mil milhões"), 2_000_000_000.0)
        self.assertEqual(_float("1,5 milhões"), 1_500_000.0)

    def test_invalid(self):
        self.assertIsNone(_float(None))
        self.assertIsNone(_float("null"))
        self.assertIsNone(_float(True))
        self.assertIsNone(_float("sem valor"))


class ScrapeCompeteParsingTests(SimpleTestCase):
    """Parse do HTML do Compete2030 (fixture sintética, sem rede)."""

    LISTING_HTML = """
    <html><body>
      <article class="row g-0 border-bottom">
        <a href="https://compete2030.gov.pt/avisos/aviso-teste/">
          <h3 class="card-title">Aviso Teste</h3>
        </a>
        <div class="badge">Aberto</div>
        <h4 class="fw-normal">COMPETE-2026-1</h4>
        <div class="col-md-3"><p class="fw-bold">30/09/2026</p></div>
      </article>
    </body></html>
    """

    DETAIL_HTML = """
    <html><body><main>
      <div class="date-badges"><span class="text-muted">10 de junho de 2026</span></div>
      <p>Período de candidatura: 01/07/2026</p>
      <h2>Documentos</h2>
      <ul>
        <li><a href="/docs/aviso.pdf">Aviso COMPETE-2026-1</a></li>
      </ul>
    </main></body></html>
    """

    def test_parse_listing(self):
        grants = scrape_compete._parse_listing(self.LISTING_HTML)
        self.assertEqual(len(grants), 1)
        g = grants[0]
        self.assertEqual(g["url"], "https://compete2030.gov.pt/avisos/aviso-teste/")
        self.assertEqual(g["title"], "Aviso Teste")
        self.assertEqual(g["estados"], ["Aberto"])
        self.assertEqual(g["grant_code"], "COMPETE-2026-1")
        self.assertEqual(g["closing_date"], "30/09/2026")

    def test_parse_detail(self):
        with patch.object(scrape_compete, "_pdf_info",
                          return_value={"paginas": 10, "natureza": None}):
            data = scrape_compete._parse_detail(self.DETAIL_HTML, "https://x/")
        self.assertEqual(data["data_publicacao"], "10/06/2026")
        self.assertEqual(data["data_inicio"], "01/07/2026")
        self.assertEqual(len(data["documentos"]), 1)
        # href relativo vira URL absoluto do site
        self.assertEqual(data["documentos"][0]["url"], "https://compete2030.gov.pt/docs/aviso.pdf")
        self.assertIsNotNone(data["latest_notice"])

    def test_parse_detail_without_main_does_not_crash(self):
        # HTML inesperado (site mudou) → degrada para campos vazios, não AttributeError.
        data = scrape_compete._parse_detail("<html><body><p>vazio</p></body></html>", "https://x/")
        self.assertEqual(data["phases"], [])
        self.assertEqual(data["documentos"], [])
        self.assertIsNone(data["latest_notice"])


class ScrapePortugalParsingTests(SimpleTestCase):
    """Parse do HTML do Portugal2030 (fixture sintética, _pdf_info mockado)."""

    HTML = """
    <html><body><div class="et_pb_column_2_tb_body">
      <li>
        <strong>Aviso Teste Portugal</strong>
        <div><dl><dt>Código do Aviso</dt><dd>PT-2026-01</dd></dl></div>
        <div class="avisos-docs">
          <a href="/wp-content/anexo_um.pdf">Anexo I.pdf</a>
          <a href="https://cdn.example.com/aviso_pt202601.pdf">Aviso PT-2026-01.pdf</a>
        </div>
      </li>
    </div></body></html>
    """

    def test_parse_main(self):
        with patch.object(scrape_portugal, "_pdf_info",
                          return_value={"paginas": 10, "natureza": None}):
            parsed = scrape_portugal._parse_main(self.HTML)
        self.assertEqual(len(parsed), 1)
        aviso = parsed[0]
        self.assertEqual(aviso["title"], "Aviso Teste Portugal")
        self.assertEqual(aviso["grant_code"], "PT-2026-01")
        urls = {d["nome"]: d["url"] for d in aviso["documentos"]}
        # href relativo leva o prefixo do site; URL absoluto fica intacto.
        self.assertEqual(urls["Anexo I.pdf"], "https://portugal2030.pt/wp-content/anexo_um.pdf")
        self.assertEqual(urls["Aviso PT-2026-01.pdf"], "https://cdn.example.com/aviso_pt202601.pdf")
        # O anexo é rejeitado como candidato; o aviso é o latest_notice.
        self.assertEqual(aviso["latest_notice"]["nome"], "Aviso PT-2026-01.pdf")

    def test_missing_container_returns_empty(self):
        self.assertEqual(scrape_portugal._parse_main("<html><body></body></html>"), [])


class _FakeDriver:
    """Substitui o WebDriver no teste: _parse_grants só usa .page_source."""

    def __init__(self, html: str):
        self.page_source = html


class ScrapePrrParsingTests(SimpleTestCase):
    """Parse do HTML do PRR (fixture sintética, sem Selenium nem rede)."""

    HTML = """
    <html><body>
    <div class="vc_tta-panel" data-vc-content=".vc_tta-panel-body">
      <span class="vc_tta-title-text">Empresas</span>
      <div class="accordion">
        <div id="prr-arrow-chev" class="arrow-chev">Apoios às empresas</div>
        <div class="panel">
          <div class="search-card-top">
            <a class="title-link" href="https://recuperarportugal.gov.pt/aviso-x/">
              Aviso N.º 01/C05-i01/2026 - Apoio Teste
            </a>
            <p>Data de abertura: 01/06/2026</p>
            <p>Data de encerramento: até 30/09/2026 às 18h00</p>
            <p>Ver documentação <a href="https://recuperarportugal.gov.pt/docs/aviso.pdf">aqui</a></p>
            <p><a href="https://recuperarportugal.gov.pt/uploads/anexo1.pdf">Anexo 1</a></p>
          </div>
        </div>
      </div>
    </div>
    </body></html>
    """

    def test_parse_grants(self):
        all_data: list[dict] = []
        scrape_prr._parse_grants(_FakeDriver(self.HTML), all_data)

        self.assertEqual(len(all_data), 1)
        aviso = all_data[0]
        self.assertEqual(aviso["tipo"], "Empresas")
        self.assertEqual(aviso["subtitulo"], "Apoios às empresas")
        self.assertEqual(aviso["grant_code"], "01/C05-i01/2026")
        self.assertEqual(aviso["opening_date"], "01/06/2026")
        # data de fim apanha a data E a hora do texto "até 30/09/2026 às 18h00"
        self.assertEqual(aviso["closing_date"], "30/09/2026 18:00h")
        # "Ver documentação" define o latest_notice
        self.assertEqual(aviso["latest_notice"],
                         "https://recuperarportugal.gov.pt/docs/aviso.pdf")
        nomes = {d["nome"] for d in aviso["documentos"]}
        self.assertIn("anexo1.pdf", nomes)

    def test_missing_title_span_does_not_crash(self):
        # Painel sem o span do título (HTML mudou) → tipo vazio, sem AttributeError.
        html = self.HTML.replace('<span class="vc_tta-title-text">Empresas</span>', "")
        all_data: list[dict] = []
        scrape_prr._parse_grants(_FakeDriver(html), all_data)
        self.assertEqual(all_data[0]["tipo"], "")

    def test_normalize_date_handles_por_extenso(self):
        self.assertEqual(scrape_prr._normalize_date("10 de junho de 2026"), "10/06/2026")
        self.assertEqual(scrape_prr._normalize_date("até 15 de janeiro de 2027, 17h30"),
                         "15/01/2027 17:30h")
        self.assertEqual(scrape_prr._normalize_date("sem data nenhuma"), "")


class GrantCaeSyncTests(TestCase):
    """A tabela derivada GrantCae mantém-se em sincronia com included_caes/excluded_caes
    (fonte de verdade), via signal — na criação e na edição."""

    def _grant(self, **kw):
        from .models import Grant
        defaults = dict(source="portugal", scraping_url="https://x/cae/", grant_code="CAE-1",
                        ai_processed=True)
        defaults.update(kw)
        return Grant.objects.create(**defaults)

    def _prefixes(self, grant, kind):
        return sorted(grant.cae_entries.filter(kind=kind).values_list("prefix", flat=True))

    def test_populated_on_create(self):
        g = self._grant(included_caes=["62***", "651**"], excluded_caes=["6512*"])
        self.assertEqual(self._prefixes(g, "included"), ["62", "651"])
        self.assertEqual(self._prefixes(g, "excluded"), ["6512"])

    def test_exact_code_prefix_is_full_code(self):
        g = self._grant(included_caes=["65124"])
        self.assertEqual(self._prefixes(g, "included"), ["65124"])

    def test_malformed_pattern_ignored(self):
        g = self._grant(included_caes=["62***", "abc**", ""])
        self.assertEqual(self._prefixes(g, "included"), ["62"])  # 'abc'/'' descartados

    def test_resync_on_edit(self):
        g = self._grant(included_caes=["62***"])
        self.assertEqual(self._prefixes(g, "included"), ["62"])
        g.included_caes = ["47***", "471**"]
        g.save()
        self.assertEqual(self._prefixes(g, "included"), ["47", "471"])  # antigo '62' desapareceu

    def test_cleared_when_emptied(self):
        g = self._grant(included_caes=["62***"])
        g.included_caes = []
        g.save()
        self.assertEqual(g.cae_entries.count(), 0)

    def test_embedding_save_does_not_touch_cae(self):
        # Um save com update_fields que não inclui os CAE não reconstrói a tabela.
        g = self._grant(included_caes=["62***"])
        g.activity_embedding_hash = "abc"
        g.save(update_fields=["activity_embedding_hash"])
        self.assertEqual(self._prefixes(g, "included"), ["62"])  # intacto


class DeactivateExpiredGrantsTests(TestCase):
    """Sincronização de Grant.active com a closing_date (desativa terminados, reativa prorrogados)."""

    def _grant(self, code, closing, active=True):
        return Grant.objects.create(
            source="portugal", scraping_url=f"https://x/{code}/", grant_code=code,
            closing_date=closing, active=active,
        )

    def test_sync(self):
        today = date(2026, 7, 1)
        expired = self._grant("EXP", "2026-06-30", active=True)
        still_open = self._grant("OPEN", "31/12/2026", active=True)
        unreadable = self._grant("RAW", "brevemente", active=True)
        prorrogado = self._grant("PROR", "2026-12-31", active=False)  # reativa

        changed = deactivate_expired_grants(today)

        for g in (expired, still_open, unreadable, prorrogado):
            g.refresh_from_db()
        self.assertFalse(expired.active)
        self.assertTrue(still_open.active)
        self.assertTrue(unreadable.active)   # sem data legível → mantém ativo
        self.assertTrue(prorrogado.active)   # data futura → reativado
        self.assertEqual(changed, 2)         # EXP desativado + PROR reativado


class GrantCollectionEditTests(TestCase):
    """Edição das coleções-filhas (taxas / dotações por fase×área) pela MESMA rota /edit/,
    e recomputo do financing_rate. Tudo num só pedido."""

    def setUp(self):
        self.grant = Grant.objects.create(
            source="portugal", scraping_url="https://x/coll/", grant_code="COLL-1",
            ai_processed=True,
        )
        self.commercial = User.objects.create_user(
            "com_coll", email="c@x.pt", password="Xk93!vTq21mZ")
        self.commercial.profile.role = UserProfile.COMMERCIAL_GRANTS
        self.commercial.profile.save()
        self.client_user = User.objects.create_user("cli_coll", password="Xk93!vTq21mZ")

    def _edit(self, payload):
        return self.client.put(f"/avisos/{self.grant.pk}/edit/",
                               data=json.dumps(payload), content_type="application/json")

    # --- FinancingRate ---
    def test_replace_financing_rates_and_recompute_rate(self):
        self.client.force_login(self.commercial)
        resp = self._edit({
            "financing_rates": [
                {"company_size": "PME", "base_rate": "60.0", "max_global_rate": "75.0"},
                {"company_size": "Grande", "base_rate": "40.0", "max_global_rate": "50.0"},
            ]
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["collections_updated"], ["financing_rates"])
        self.assertEqual(body["financing_rate"], 75.0)   # maior das taxas, recomputado
        self.assertEqual(self.grant.financing_rates.count(), 2)

    def test_financing_rates_full_replace_removes_old(self):
        from .models import FinancingRate
        FinancingRate.objects.create(grant=self.grant, max_global_rate="99.0")
        self.client.force_login(self.commercial)
        self._edit({"financing_rates": [{"max_global_rate": "10.0"}]})
        rates = list(self.grant.financing_rates.values_list("max_global_rate", flat=True))
        self.assertEqual(rates, ["10.0"])   # a antiga (99) foi substituída

    def test_empty_list_clears_collection(self):
        from .models import FinancingRate
        FinancingRate.objects.create(grant=self.grant, max_global_rate="50.0")
        self.client.force_login(self.commercial)
        resp = self._edit({"financing_rates": []})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.grant.financing_rates.count(), 0)
        self.assertIsNone(resp.json()["financing_rate"])

    def test_invalid_float_returns_400(self):
        self.client.force_login(self.commercial)
        resp = self._edit({"financing_rates": [{"minimis_accumulation_limit": "não é número"}]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.grant.financing_rates.count(), 0)  # nada gravado (atómico)

    # --- PhaseArea ---
    def test_replace_phase_areas_recompute_rate_ignores_global(self):
        self.client.force_login(self.commercial)
        resp = self._edit({
            "phase_areas": [
                {"fund_name": "FEDER", "budget_allocation": 4000000.0, "max_financing_rate": 85.0},
                # "Dotação Global" (100%) é ignorada no cálculo da taxa mostrada.
                {"fund_name": "Dotação Global", "budget_allocation": 5000000.0,
                 "max_financing_rate": 100.0},
            ]
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.grant.phase_areas.count(), 2)
        self.assertEqual(resp.json()["financing_rate"], 85.0)   # 100 da global é ignorado

    def test_phase_areas_take_priority_over_financing_rates(self):
        from .models import FinancingRate
        FinancingRate.objects.create(grant=self.grant, max_global_rate="60.0")
        self.client.force_login(self.commercial)
        self._edit({"phase_areas": [{"fund_name": "FEDER", "max_financing_rate": 85.0}]})
        self.grant.refresh_from_db()
        from avisos.views import _financing_rate
        self.assertEqual(_financing_rate(self.grant), 85.0)  # PhaseArea manda sobre FinancingRate

    def test_phase_id_not_belonging_returns_400(self):
        from .models import Grant as G, Phase
        other = G.objects.create(source="prr", scraping_url="https://x/other/", grant_code="OTHER")
        alien_phase = Phase.objects.create(grant=other, name="Fase alheia")
        self.client.force_login(self.commercial)
        resp = self._edit({"phase_areas": [{"max_financing_rate": 50.0, "phase_id": alien_phase.pk}]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.grant.phase_areas.count(), 0)

    def test_phase_area_keeps_valid_fk(self):
        from .models import Phase
        phase = Phase.objects.create(grant=self.grant, name="Fase 1")
        self.client.force_login(self.commercial)
        resp = self._edit({"phase_areas": [{"max_financing_rate": 70.0, "phase_id": phase.pk}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.grant.phase_areas.first().phase_id, phase.pk)

    # --- Override manual do financing_rate ---
    def test_edit_financing_rate_directly(self):
        # Editar o financing_rate diretamente fixa o override manual.
        from .models import FinancingRate
        FinancingRate.objects.create(grant=self.grant, max_global_rate="60.0")  # calculado = 60
        self.client.force_login(self.commercial)
        resp = self._edit({"financing_rate": 92.5})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("financing_rate", resp.json()["updated"])
        self.assertEqual(resp.json()["financing_rate"], 92.5)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.financing_rate, 92.5)  # override gravado

    def test_manual_override_wins_over_computed(self):
        from .models import FinancingRate
        FinancingRate.objects.create(grant=self.grant, max_global_rate="60.0")
        self.client.force_login(self.commercial)
        self._edit({"financing_rate": 92.5})
        self.grant.refresh_from_db()
        # Detalhe mostra o override (92.5), não o calculado (60), e marca-o como manual.
        detail = self.client.get(f"/avisos/{self.grant.pk}/").json()
        self.assertEqual(detail["financing_rate"], 92.5)
        self.assertTrue(detail["financing_rate_manual"])

    def test_null_override_reverts_to_computed(self):
        from .models import FinancingRate
        FinancingRate.objects.create(grant=self.grant, max_global_rate="60.0")
        self.grant.financing_rate = 92.5
        self.grant.save(update_fields=["financing_rate"])
        self.client.force_login(self.commercial)
        self._edit({"financing_rate": None})   # repõe o cálculo automático
        detail = self.client.get(f"/avisos/{self.grant.pk}/").json()
        self.assertEqual(detail["financing_rate"], 60.0)   # volta ao calculado
        self.assertFalse(detail["financing_rate_manual"])

    def test_editing_tables_follows_when_no_override(self):
        # Sem override, editar as tabelas muda a taxa mostrada (comportamento por defeito).
        self.client.force_login(self.commercial)
        self._edit({"phase_areas": [{"fund_name": "FEDER", "max_financing_rate": 85.0}]})
        detail = self.client.get(f"/avisos/{self.grant.pk}/").json()
        self.assertEqual(detail["financing_rate"], 85.0)
        self.assertFalse(detail["financing_rate_manual"])

    def test_override_persists_even_after_editing_tables(self):
        # Com override manual, editar as tabelas NÃO muda a taxa mostrada (o manual prevalece).
        self.client.force_login(self.commercial)
        self._edit({"financing_rate": 50.0})
        self._edit({"phase_areas": [{"fund_name": "FEDER", "max_financing_rate": 85.0}]})
        detail = self.client.get(f"/avisos/{self.grant.pk}/").json()
        self.assertEqual(detail["financing_rate"], 50.0)   # override mantém-se

    # --- Campos + coleções no MESMO pedido (atómico) ---
    def test_scalar_and_collection_in_one_request(self):
        self.client.force_login(self.commercial)
        resp = self._edit({
            "title": "Título novo",
            "financing_rates": [{"max_global_rate": "80.0"}],
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["updated"], ["title"])
        self.assertEqual(body["collections_updated"], ["financing_rates"])
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.title, "Título novo")
        self.assertEqual(self.grant.financing_rates.count(), 1)

    def test_collection_failure_rolls_back_scalar(self):
        # Se a coleção for inválida, o campo escalar também NÃO é gravado (atómico).
        self.client.force_login(self.commercial)
        resp = self._edit({
            "title": "Não deve gravar",
            "financing_rates": [{"minimis_accumulation_limit": "xpto"}],
        })
        self.assertEqual(resp.status_code, 400)
        self.grant.refresh_from_db()
        self.assertNotEqual(self.grant.title, "Não deve gravar")

    # --- Permissões / método ---
    def test_anonymous_401(self):
        self.assertEqual(self._edit({"financing_rates": []}).status_code, 401)

    def test_client_role_403(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self._edit({"phase_areas": []}).status_code, 403)

    def test_non_list_body_returns_400(self):
        self.client.force_login(self.commercial)
        resp = self._edit({"financing_rates": {"não": "é lista"}})
        self.assertEqual(resp.status_code, 400)


class SaveGrantMarksSourceAsScrapeTests(TestCase):
    """save_scraped_grant/save_ai_grant (o pipeline) marcam a origem da última escrita como
    'scrape' e limpam `last_updated_by` — mesmo sobre um aviso que já tinha sido editado à mão,
    porque a origem reflete SEMPRE a escrita mais recente."""

    def _manually_edited_grant(self, code, **kw):
        editor = User.objects.create_user(f"editor_{code}", password="Xk93!vTq21mZ")
        defaults = dict(
            source="portugal", scraping_url=f"https://x/{code}/", grant_code=code,
            last_update_source=Grant.SOURCE_MANUAL, last_updated_by=editor,
        )
        defaults.update(kw)
        return Grant.objects.create(**defaults), editor

    def test_save_scraped_grant_marks_scrape(self):
        from .db_service import save_scraped_grant
        grant, editor = self._manually_edited_grant("SCR-SRC")
        save_scraped_grant(
            {"url": grant.scraping_url, "grant_code": "SCR-SRC", "closing_date": "31/12/2026"},
            source="portugal",
        )
        grant.refresh_from_db()
        self.assertEqual(grant.last_update_source, Grant.SOURCE_SCRAPE)
        self.assertIsNone(grant.last_updated_by)

    def test_save_ai_grant_marks_scrape(self):
        from .db_service import save_ai_grant
        grant, editor = self._manually_edited_grant("AI-SRC")
        save_ai_grant({"Grant": {"grant_code": "AI-SRC"}}, scraping_url=grant.scraping_url)
        grant.refresh_from_db()
        self.assertEqual(grant.last_update_source, Grant.SOURCE_SCRAPE)
        self.assertIsNone(grant.last_updated_by)


class GrantsEditViewTests(TestCase):
    """Permissões, validação e auditoria da edição de avisos (/avisos/<pk>/edit/)."""

    def setUp(self):
        self.grant = Grant.objects.create(
            source="portugal", scraping_url="https://x/edit-test/",
            grant_code="EDIT-1", title="Original",
        )
        self.commercial = User.objects.create_user(
            "comercial1", email="comercial1@x.pt", password="Xk93!vTq21mZ")
        self.commercial.profile.role = UserProfile.COMMERCIAL_GRANTS
        self.commercial.profile.save()
        self.client_user = User.objects.create_user("cliente1", password="Xk93!vTq21mZ")
        # o signal já cria o perfil com role=client

    def _edit(self, payload):
        # Edição é por PUT (por id, inalterável) — POST já não é aceite.
        return self.client.put(
            f"/avisos/{self.grant.pk}/edit/",
            data=json.dumps(payload), content_type="application/json",
        )

    def test_anonymous_gets_401(self):
        self.assertEqual(self._edit({"title": "X"}).status_code, 401)

    def test_client_role_gets_403(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self._edit({"title": "X"}).status_code, 403)

    def test_commercial_edits_and_audit_logs_who_and_what(self):
        self.client.force_login(self.commercial)
        with self.assertLogs("avisos.audit", level="INFO") as logs:
            resp = self._edit({"title": "Novo título", "id": 999})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["updated"], ["title"])
        self.assertEqual(body["ignored"], ["id"])   # id nunca é editável
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.title, "Novo título")
        # O log de auditoria diz QUEM alterou e O QUÊ (antigo -> novo).
        self.assertIn("comercial1", logs.output[0])
        self.assertIn("'Original'", logs.output[0])
        self.assertIn("'Novo título'", logs.output[0])

    def test_commercial_public_also_edits_avisos(self):
        # commercial_public acumula avisos+anúncios (commercial_grants é o especialista, mas
        # não o único com acesso).
        commercial_public = User.objects.create_user(
            "comercial_pub1", email="cp1@x.pt", password="Xk93!vTq21mZ")
        commercial_public.profile.role = UserProfile.COMMERCIAL_PUBLIC
        commercial_public.profile.save()
        self.client.force_login(commercial_public)
        self.assertEqual(self._edit({"title": "X"}).status_code, 200)

    def test_invalid_value_returns_400_not_500(self):
        self.client.force_login(self.commercial)
        resp = self._edit({"max_duration_months": "não é um número"})
        self.assertEqual(resp.status_code, 400)
        self.grant.refresh_from_db()
        self.assertIsNone(self.grant.max_duration_months)  # nada foi gravado

    def test_unknown_grant_returns_404(self):
        self.client.force_login(self.commercial)
        resp = self.client.put(
            "/avisos/999999/edit/", data="{}", content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_regenerated_fields_are_not_editable(self):
        # annex_documents (vem do scrape) e applicable_legislation (vem da extração IA) são
        # reescritos a cada processamento — editá-los seria uma alteração fantasma.
        self.client.force_login(self.commercial)
        resp = self._edit({
            "annex_documents": [{"name": "falso.pdf", "url": "https://x/f.pdf"}],
            "applicable_legislation": [{"regulation_name": "Lei inventada"}],
            "title": "Este passa",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["updated"], ["title"])
        self.assertEqual(set(body["ignored"]), {"annex_documents", "applicable_legislation"})
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.annex_documents, [])        # não foi tocado
        self.assertEqual(self.grant.applicable_legislation, [])  # não foi tocado

    def test_manual_edit_marks_source_and_user(self):
        self.client.force_login(self.commercial)
        self._edit({"title": "Editado à mão"})
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.last_update_source, Grant.SOURCE_MANUAL)
        self.assertEqual(self.grant.last_updated_by, self.commercial)

    def test_collection_only_edit_also_marks_manual(self):
        # Só coleções (sem campos escalares) — o grant.save() só acontecia antes quando havia
        # campos escalares; tem de gravar a origem/utilizador mesmo assim.
        self.client.force_login(self.commercial)
        self._edit({"financing_rates": [{"max_global_rate": "50.0"}]})
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.last_update_source, Grant.SOURCE_MANUAL)
        self.assertEqual(self.grant.last_updated_by, self.commercial)

    def test_last_update_source_and_user_are_not_client_editable(self):
        self.client.force_login(self.commercial)
        other = User.objects.create_user("outro_admin", password="Xk93!vTq21mZ")
        resp = self._edit({
            "title": "X", "last_update_source": Grant.SOURCE_SCRAPE, "last_updated_by": other.pk,
        })
        body = resp.json()
        self.assertEqual(set(body["ignored"]), {"last_update_source", "last_updated_by"})
        self.grant.refresh_from_db()
        # A origem/utilizador refletem QUEM FEZ ESTA EDIÇÃO (o commercial autenticado), não o
        # que veio no payload — o cliente não consegue forjar a origem nem atribuí-la a outrem.
        self.assertEqual(self.grant.last_update_source, Grant.SOURCE_MANUAL)
        self.assertEqual(self.grant.last_updated_by, self.commercial)

    def test_detail_exposes_last_updated_by_as_username(self):
        self.client.force_login(self.commercial)
        self._edit({"title": "X"})
        resp = self.client.get(f"/avisos/{self.grant.pk}/")
        self.assertEqual(resp.json()["last_updated_by"], "comercial1")
        self.assertEqual(resp.json()["last_update_source"], Grant.SOURCE_MANUAL)

    def test_post_method_not_allowed(self):
        # A edição já não aceita POST — só PUT/PATCH.
        self.client.force_login(self.commercial)
        resp = self.client.post(
            f"/avisos/{self.grant.pk}/edit/",
            data=json.dumps({"title": "X"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 405)

    def test_edit_notifies_commercials_by_email(self):
        from django.core import mail
        self.client.force_login(self.commercial)
        resp = self._edit({"title": "Alterado"})
        self.assertEqual(resp.status_code, 200)
        # Um email-resumo foi enviado ao comercial (backend locmem nos testes).
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("comercial1@x.pt", mail.outbox[0].to)
        self.assertIn("EDIT-1", mail.outbox[0].body)

    def test_no_effective_change_does_not_notify(self):
        from django.core import mail
        self.client.force_login(self.commercial)
        self._edit({"title": "Original"})  # valor igual ao atual
        self.assertEqual(len(mail.outbox), 0)


class GrantsListAccessTests(TestCase):
    """Sem sessão só se acede ao detalhe de UM aviso (via match); a listagem exige login."""

    def setUp(self):
        self.grant = Grant.objects.create(
            source="portugal", scraping_url="https://x/access-test/",
            grant_code="ACCESS-1", ai_processed=True, active=True,
        )
        self.client_user = User.objects.create_user("cliente_acesso", password=TEST_PASSWORD)

    def test_list_requires_authentication(self):
        resp = self.client.get("/avisos/list/")
        self.assertEqual(resp.status_code, 401)

    def test_list_works_when_authenticated(self):
        self.client.force_login(self.client_user)
        resp = self.client.get("/avisos/list/")
        self.assertEqual(resp.status_code, 200)

    def test_detail_is_public_without_login(self):
        # Clicar num aviso a partir de um resultado de match (sem sessão) tem de funcionar —
        # é o único aviso a que o utilizador não autenticado pode aceder.
        resp = self.client.get(f"/avisos/{self.grant.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["grant_code"], "ACCESS-1")


class GrantsListAndDetailTests(TestCase):
    """Listagem enxuta com filtros/ordenação (exige sessão) e detalhe completo (público)."""

    def setUp(self):
        # A listagem exige sessão (qualquer papel) — autentica por omissão nesta classe,
        # que testa sobretudo filtros/ordenação da listagem, não permissões em si (essas
        # estão em GrantsListRequiresAuthTests).
        client_user = User.objects.create_user("cliente_lista", password=TEST_PASSWORD)
        self.client.force_login(client_user)

    def _grant(self, code, **kw):
        defaults = dict(source="portugal", scraping_url=f"https://x/{code}/",
                        grant_code=code, ai_processed=True, active=True)
        defaults.update(kw)
        return Grant.objects.create(**defaults)

    def test_summary_has_only_key_fields(self):
        self._grant("A", title="Aviso A", closing_date="2026-12-31", total_allocation=1000000.0)
        resp = self.client.get("/avisos/list/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body), {"total", "page", "page_size", "num_pages", "grants"})
        g = body["grants"][0]
        self.assertEqual(set(g), {"id", "grant_code", "title", "closing_date",
                                  "total_allocation", "financing_rate", "next_phase_date", "active"})

    def test_pagination(self):
        for i in range(5):
            self._grant(f"P{i}", publication_date=f"0{i+1}/01/2026")
        resp = self.client.get("/avisos/list/?page=2&page_size=2")
        body = resp.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["page_size"], 2)
        self.assertEqual(body["num_pages"], 3)
        self.assertEqual(len(body["grants"]), 2)

    def test_page_size_is_capped(self):
        self._grant("ONE")
        resp = self.client.get("/avisos/list/?page_size=9999")
        self.assertEqual(resp.json()["page_size"], 200)  # MAX_PAGE_SIZE

    def test_ordering_is_global_not_per_page(self):
        # O aviso com maior dotação foi criado PRIMEIRO (id mais baixo) — se a ordenação
        # fosse só dentro da página devolvida (ou pela ordem de inserção), não apareceria
        # em 1º lugar quando pedimos por dotação decrescente. Prova que ordena o conjunto
        # inteiro antes de paginar: a maior dotação vem sempre em 1º, o resto por id.
        self._grant("FIRST_INSERTED_SMALL", total_allocation=1000.0)
        self._grant("BIGGEST", total_allocation=9000000.0)
        self._grant("MEDIUM", total_allocation=500000.0)
        resp = self.client.get("/avisos/list/?order_by=allocation_highest&page_size=2")
        codes = [g["grant_code"] for g in resp.json()["grants"]]
        self.assertEqual(codes, ["BIGGEST", "MEDIUM"])

    def test_ordering_allocation_lowest(self):
        self._grant("BIG", total_allocation=9000000.0)
        self._grant("SMALL", total_allocation=1000.0)
        resp = self.client.get("/avisos/list/?order_by=allocation_lowest")
        codes = [g["grant_code"] for g in resp.json()["grants"]]
        self.assertEqual(codes, ["SMALL", "BIG"])

    def test_ordering_closing_earliest_and_latest(self):
        self._grant("LATE", closing_date="2026-12-31")
        self._grant("EARLY", closing_date="2026-03-01")
        earliest = [g["grant_code"] for g in self.client.get(
            "/avisos/list/?order_by=closing_earliest").json()["grants"]]
        self.assertEqual(earliest, ["EARLY", "LATE"])
        latest = [g["grant_code"] for g in self.client.get(
            "/avisos/list/?order_by=closing_latest").json()["grants"]]
        self.assertEqual(latest, ["LATE", "EARLY"])

    def test_ordering_rate_highest_puts_unknown_rate_last(self):
        from .models import FinancingRate
        with_rate = self._grant("HASRATE")
        FinancingRate.objects.create(grant=with_rate, max_global_rate="75.0")
        self._grant("NORATE")  # sem FinancingRate/PhaseArea -> rate=None
        resp = self.client.get("/avisos/list/?order_by=rate_highest")
        codes = [g["grant_code"] for g in resp.json()["grants"]]
        self.assertEqual(codes, ["HASRATE", "NORATE"])  # sem taxa vai para o fim, não para o topo

    def test_search_matches_grant_code_and_title(self):
        self._grant("MPR-2026-6", title="Modernização e Capacitação")
        self._grant("OUTRO-2026", title="Reindustrialização Verde")
        self.assertEqual(
            [g["grant_code"] for g in self.client.get("/avisos/list/?q=MPR-2026").json()["grants"]],
            ["MPR-2026-6"],
        )
        self.assertEqual(
            [g["grant_code"] for g in self.client.get("/avisos/list/?q=Verde").json()["grants"]],
            ["OUTRO-2026"],
        )
        self.assertEqual(self.client.get("/avisos/list/?q=inexistente").json()["total"], 0)

    def test_search_is_global_not_per_page(self):
        # O aviso que bate na pesquisa é o ÚLTIMO a ser criado (id mais alto) — se a pesquisa só
        # visse a página atual, um page_size pequeno cortava-o fora antes de chegar à pesquisa.
        for i in range(5):
            self._grant(f"N{i}", title="outro assunto")
        self._grant("ALVO", title="requalificação urbana")
        resp = self.client.get("/avisos/list/?q=requalificação&page_size=2")
        body = resp.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual([g["grant_code"] for g in body["grants"]], ["ALVO"])

    def test_unknown_order_by_falls_back_to_publication_recent(self):
        self._grant("OLD", publication_date="01/01/2026")
        self._grant("NEW", publication_date="01/06/2026")
        resp = self.client.get("/avisos/list/?order_by=nonsense")
        codes = [g["grant_code"] for g in resp.json()["grants"]]
        self.assertEqual(codes, ["NEW", "OLD"])

    # --- Filtro CAE (regra do prefixo) -------------------------------------
    def _codes(self, url):
        return {g["grant_code"] for g in self.client.get(url).json()["grants"]}

    def test_filter_cae_prefix_rule(self):
        self._grant("WILDCARD", included_caes=["55***"])   # divisão 55 → casa 55849
        self._grant("EXACT", included_caes=["55847"])      # classe exata → não casa 55848
        self._grant("OPEN")                                # sem restrição → casa qualquer CAE
        self._grant("OTHER", included_caes=["62***"])      # outra divisão → não casa
        self.assertEqual(self._codes("/avisos/list/?cae=55849"), {"WILDCARD", "OPEN"})
        self.assertEqual(self._codes("/avisos/list/?cae=55847"), {"WILDCARD", "EXACT", "OPEN"})

    def test_filter_cae_respects_exclusions(self):
        self._grant("INCL_EXCL", included_caes=["55***"], excluded_caes=["5584*"])
        # 55849 está incluído por '55***' mas excluído por '5584*' (mais específico) → não casa.
        self.assertEqual(self._codes("/avisos/list/?cae=55849"), set())
        # 55100 está incluído e não bate na exclusão → casa.
        self.assertEqual(self._codes("/avisos/list/?cae=55100"), {"INCL_EXCL"})

    # --- Filtro dimensão ---------------------------------------------------
    def test_filter_dimension_matches_admitted(self):
        self._grant("PME", beneficiary_eligibility_criteria=["Destina-se a PME"])
        self._grant("GRANDE", beneficiary_eligibility_criteria=["Apenas grandes empresas"])
        self._grant("OPEN")   # sem restrição de dimensão → admite todas
        self.assertEqual(self._codes("/avisos/list/?dimension=micro"), {"PME", "OPEN"})
        self.assertEqual(self._codes("/avisos/list/?dimension=grande"), {"GRANDE", "OPEN"})
        # Multi (repetido e CSV) — união: micro (PME) ou grande.
        self.assertEqual(
            self._codes("/avisos/list/?dimension=micro&dimension=grande"),
            {"PME", "GRANDE", "OPEN"})
        self.assertEqual(self._codes("/avisos/list/?dimension=micro,grande"),
                         {"PME", "GRANDE", "OPEN"})

    # --- Filtro região / áreas abrangidas ----------------------------------
    def test_filter_region_matches_eligible_regions_and_covered_areas(self):
        from .models import CoveredArea
        self._grant("REGIAO", eligible_regions=["Norte", "Centro"])
        g_area = self._grant("AREA")
        CoveredArea.objects.create(grant=g_area, geographic_area="Área Metropolitana do Porto (AMP)")
        self._grant("ALGARVE", eligible_regions=["Algarve"])
        self._grant("OPEN")   # sem regiões nem áreas → âmbito não restrito, casa qualquer região
        self.assertEqual(self._codes("/avisos/list/?region=Norte"), {"REGIAO", "OPEN"})
        self.assertEqual(self._codes("/avisos/list/?region=Porto"), {"AREA", "OPEN"})
        self.assertEqual(self._codes("/avisos/list/?region=Algarve"), {"ALGARVE", "OPEN"})

    def test_allocation_filter(self):
        self._grant("SMALL", total_allocation=100000.0)
        self._grant("BIG", total_allocation=5000000.0)
        resp = self.client.get("/avisos/list/?allocation_min=1000000")
        codes = [g["grant_code"] for g in resp.json()["grants"]]
        self.assertEqual(codes, ["BIG"])

    def test_closing_date_filter(self):
        self._grant("EARLY", closing_date="2026-03-31")
        self._grant("LATE", closing_date="2026-12-31")
        resp = self.client.get("/avisos/list/?closing_from=2026-06-01")
        codes = [g["grant_code"] for g in resp.json()["grants"]]
        self.assertEqual(codes, ["LATE"])

    def test_ordering_by_publication_date(self):
        # Ordena por data de publicação, mais recente primeiro.
        self._grant("OLD", publication_date="15/01/2026")
        self._grant("NEW", publication_date="20/06/2026")
        resp = self.client.get("/avisos/list/")
        codes = [g["grant_code"] for g in resp.json()["grants"]]
        self.assertLess(codes.index("NEW"), codes.index("OLD"))

    def test_grants_without_publication_date_go_last(self):
        self._grant("DATED", publication_date="20/06/2026")
        self._grant("UNDATED", publication_date="brevemente")
        resp = self.client.get("/avisos/list/")
        codes = [g["grant_code"] for g in resp.json()["grants"]]
        self.assertLess(codes.index("DATED"), codes.index("UNDATED"))

    def test_detail_returns_full_and_relations(self):
        from .models import Phase
        grant = self._grant("DET", title="Detalhado", maximum_self_financing=60.0)
        Phase.objects.create(grant=grant, name="Fase 1", start_date="2026-06-01")
        resp = self.client.get(f"/avisos/{grant.pk}/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["title"], "Detalhado")
        self.assertEqual(body["maximum_self_financing"], 60.0)  # campo renomeado
        self.assertEqual(len(body["phases"]), 1)
        self.assertNotIn("activity_embedding", body)  # vetor não é exposto

    def test_detail_404(self):
        self.assertEqual(self.client.get("/avisos/999999/").status_code, 404)


class GrantDocumentServeTests(TestCase):
    """Servir o PDF local do aviso (pdf_Avisos) e o link no detalhe. BASE_DIR temporário
    (override_settings) para não escrever no repositório."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pdf_Avisos" / "portugal").mkdir(parents=True)
        self.rel = "pdf_Avisos/portugal/20260101_aviso.pdf"
        (self.tmp / self.rel).write_bytes(b"%PDF-1.4\nconteudo\n%%EOF")
        self.grant = Grant.objects.create(
            source="portugal", scraping_url="https://x/doc/", grant_code="DOC-1",
            ai_processed=True, pdf_path=self.rel,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detail_exposes_document_url_and_lists_aviso(self):
        with override_settings(BASE_DIR=self.tmp):
            body = self.client.get(f"/avisos/{self.grant.pk}/").json()
        self.assertEqual(body["document_url"], f"/avisos/{self.grant.pk}/document/")
        first = body["documents"][0]  # o aviso local é o primeiro
        self.assertEqual(first["doc_type"], "aviso")
        self.assertEqual(first["name"], "20260101_aviso.pdf")
        self.assertTrue(first["local"])

    def test_serve_returns_pdf_inline(self):
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/avisos/{self.grant.pk}/document/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("inline", resp["Content-Disposition"])
        self.assertEqual(b"".join(resp.streaming_content), b"%PDF-1.4\nconteudo\n%%EOF")

    def test_serve_404_when_file_missing(self):
        os.remove(self.tmp / self.rel)
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/avisos/{self.grant.pk}/document/")
        self.assertEqual(resp.status_code, 404)

    def test_serve_blocks_path_traversal(self):
        # Um pdf_path manipulado a apontar para fora de pdf_Avisos não é servido.
        (self.tmp / "secret.pdf").write_bytes(b"%PDF-1.4\nsegredo")
        self.grant.pdf_path = "pdf_Avisos/../secret.pdf"
        self.grant.save(update_fields=["pdf_path"])
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/avisos/{self.grant.pk}/document/")
        self.assertEqual(resp.status_code, 404)

    def test_detail_no_document_url_when_no_file(self):
        g = Grant.objects.create(source="prr", scraping_url="https://x/nofile/",
                                 grant_code="NOFILE", ai_processed=True, pdf_path="")
        with override_settings(BASE_DIR=self.tmp):
            body = self.client.get(f"/avisos/{g.pk}/").json()
        self.assertIsNone(body["document_url"])
