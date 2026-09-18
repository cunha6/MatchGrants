"""Testes da app calls.

Nenhum destes testes vai à rede: as respostas da API são simuladas, e os casos que simulam
vieram da resposta REAL do portal (metadata embrulhada em listas, campos em falta nos tipos
2 e 8, `deadlineDate` com vários prazos, a mesma `reference` repetida entre páginas).
"""

import json
import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from users.models import UserProfile
from . import services
from .models import Call, CallType, CodeLabel, ProgrammeType

TEST_PASSWORD = os.environ.get("TEST_USER_PASSWORD", "test-only-password")


def _result(reference, identifier, *, status="31094502", call_type="1",
            programme="43108390", deadlines=None, **extra):
    """Um resultado da API, com a metadata embrulhada em listas como ela vem."""
    metadata = {
        "reference": [reference],
        "identifier": [identifier],
        "title": [f"Title {identifier}"],
        "status": [status],
        "type": [call_type],
        "frameworkProgramme": [programme],
        "startDate": ["2026-12-08T00:00:00.000+0000"],
        "url": [f"https://example.test/{identifier}"],
        **extra,
    }
    if deadlines is not None:
        metadata["deadlineDate"] = deadlines
    return {"reference": reference, "metadata": metadata}


FACETS = {
    "frameworkProgramme": [
        {"rawValue": "43108390", "value": "Horizon Europe (HORIZON)", "count": 108803},
        {"rawValue": "43252405", "value": "LIFE", "count": 5086},
    ],
    "type": [
        {"rawValue": "1", "value": "Grant", "count": 287234},
        {"rawValue": "2", "value": "Calls for proposals", "count": 57773},
        {"rawValue": "8", "value": "Cascade funding calls", "count": 16830},
        # A faceta traz também entradas que NÃO são tipos de call — não devem entrar na BD.
        {"rawValue": "ORGANISATION", "value": "ORGANISATION", "count": 859990},
        {"rawValue": "0", "value": "Tender", "count": 379169},
    ],
}


class ParsingTests(TestCase):
    """Normalização dos formatos que a API usa."""

    def test_first_unwraps_single_element_lists(self):
        self.assertEqual(services._first(["31094502"]), "31094502")
        self.assertEqual(services._first("plain"), "plain")
        self.assertEqual(services._first(None), "")
        self.assertEqual(services._first([]), "")

    def test_parse_datetime_handles_api_formats(self):
        parsed = services._parse_datetime("2026-12-08T00:00:00.000+0000")
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 12, 8))
        self.assertIsNotNone(parsed.tzinfo)
        # Formato com timezone por nome, usado em alguns campos.
        self.assertIsNotNone(services._parse_datetime("2024-09-27+02:00 Europe/Brussels"))
        self.assertIsNone(services._parse_datetime(""))
        self.assertIsNone(services._parse_datetime("não é uma data"))

    def test_next_deadline_prefers_the_next_future_one(self):
        """Numa call com prazos em cascata, vale o próximo prazo — não o primeiro da lista."""
        now = timezone.now()
        past = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        soon = (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        later = (now + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        chosen = services._next_deadline([later, past, soon])
        self.assertEqual(chosen.date(), (now + timedelta(days=10)).date())

    def test_next_deadline_falls_back_to_the_last_past_one(self):
        chosen = services._next_deadline(["2001-01-01T00:00:00.000+0000"])
        self.assertEqual(chosen.year, 2001)
        self.assertIsNone(services._next_deadline([]))


class FieldFallbackTests(TestCase):
    """`_field()`: 5 das 1339 calls reais (tipo 8, "COMPETITIVE_CALL") não têm a chave
    `metadata` — os mesmos campos vêm soltos no nível de topo do resultado. Sem este
    fallback essas calls ficavam com todas as colunas cruas vazias."""

    def test_reads_from_metadata_when_present(self):
        result = {"reference": "R1", "metadata": {"status": ["31094502"]}}
        self.assertEqual(services._field(result, "status"), ["31094502"])

    def test_falls_back_to_top_level_when_metadata_is_absent(self):
        # Exatamente o formato das 5 calls sem "metadata": os campos soltos no topo.
        result = {"reference": "R1", "status": ["31094502"], "identifier": ["X"]}
        self.assertEqual(services._field(result, "status"), ["31094502"])
        self.assertEqual(services._field(result, "identifier"), ["X"])

    def test_top_level_wins_when_both_exist(self):
        result = {"reference": "R1", "status": "top", "metadata": {"status": ["meta"]}}
        self.assertEqual(services._field(result, "status"), "top")

    def test_missing_field_is_none(self):
        self.assertIsNone(services._field({"reference": "R1"}, "nada"))
        self.assertIsNone(services._field({"reference": "R1", "metadata": {}}, "nada"))


@patch.object(services, "_fetch_facets", return_value=FACETS)
class SyncTests(TestCase):
    """Comportamento do upsert."""

    def _run_sync(self, pages):
        """Corre o sync com `pages` (lista de páginas); a última vazia termina a paginação."""
        with patch.object(services, "_fetch_page", side_effect=[*pages, []]):
            return services.sync_calls()

    def test_creates_calls_and_catalogs(self, _facets):
        summary = self._run_sync([[_result("R1", "ID-1"), _result("R2", "ID-2")]])
        self.assertEqual(summary["created"], 2)
        self.assertEqual(Call.objects.count(), 2)
        self.assertEqual(ProgrammeType.objects.count(), 2)
        # Só os tipos de call importados — ORGANISATION e Tender ficam de fora.
        self.assertEqual(
            sorted(CallType.objects.values_list("code", flat=True)), ["1", "2", "8"]
        )
        call = Call.objects.get(reference="R1")
        self.assertEqual(call.status, Call.STATUS_OPEN)
        self.assertEqual(call.programme.name, "Horizon Europe (HORIZON)")
        self.assertEqual(call.call_type.name, "Grant")

    def test_second_sync_changes_nothing(self, _facets):
        """Correr o sync outra vez sobre os mesmos dados não duplica nem marca alterações."""
        page = [_result("R1", "ID-1")]
        self._run_sync([page])
        summary = self._run_sync([page])
        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["unchanged"], 1)
        self.assertEqual(Call.objects.count(), 1)

    def test_status_change_is_applied(self, _facets):
        """Uma call que passa de Forthcoming a Open tem de ser ATUALIZADA, não ignorada."""
        self._run_sync([[_result("R1", "ID-1", status=Call.STATUS_FORTHCOMING)]])
        summary = self._run_sync([[_result("R1", "ID-1", status=Call.STATUS_OPEN)]])
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(Call.objects.get(reference="R1").status, Call.STATUS_OPEN)

    def test_repeated_reference_in_the_same_run_is_counted_once(self, _facets):
        """A API repete linhas entre páginas; a repetição não pode contar como atualização."""
        summary = self._run_sync([[_result("R1", "ID-1")], [_result("R1", "ID-1")]])
        self.assertEqual(summary["fetched"], 2)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(Call.objects.count(), 1)

    def test_same_identifier_on_different_references_are_distinct_calls(self, _facets):
        """Cascade calls (tipo 8) partilham identifier — a chave é a reference."""
        self._run_sync([[
            _result("REF-A", "SAME-ID", call_type="8"),
            _result("REF-B", "SAME-ID", call_type="8"),
        ]])
        self.assertEqual(Call.objects.count(), 2)
        self.assertEqual(Call.objects.filter(identifier="SAME-ID").count(), 2)

    def test_pagination_walks_until_an_empty_page(self, _facets):
        summary = self._run_sync([
            [_result("R1", "ID-1")],
            [_result("R2", "ID-2")],
            [_result("R3", "ID-3")],
        ])
        self.assertEqual(summary["pages"], 3)
        self.assertEqual(Call.objects.count(), 3)

    def test_multiple_deadlines_are_all_stored(self, _facets):
        now = timezone.now()
        soon = (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        later = (now + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        self._run_sync([[_result("R1", "ID-1", deadlines=[later, soon])]])
        call = Call.objects.get(reference="R1")
        self.assertEqual(len(call.deadlines), 2)
        self.assertEqual(call.deadline_date.date(), (now + timedelta(days=10)).date())

    def test_missing_optional_fields_do_not_break_the_import(self, _facets):
        """Os tipos 2 e 8 não trazem callIdentifier/keywords/descrição."""
        self._run_sync([[_result("R1", "ID-1", call_type="2")]])
        call = Call.objects.get(reference="R1")
        self.assertEqual(call.call_identifier, "")
        self.assertEqual(call.keywords, [])
        self.assertIsNone(call.deadline_date)

    def test_sync_populates_budget_and_action_fields(self, _facets):
        overview = {"budgetTopicActionMap": {"1": [{
            "action": "ID-1 - HORIZON-IA Innovation Actions",
            "expectedGrants": 2, "minContribution": 8500000, "maxContribution": 8500000,
            "budgetYearMap": {"2027": "17000000"},
        }]}}
        self._run_sync([[_result(
            "R1", "ID-1",
            budgetOverview=[json.dumps(overview)],
            typesOfAction=["HORIZON Innovation Actions"],
            crossCuttingPriorities=["AI", "DigitalAgenda"],
        )]])
        call = Call.objects.get(reference="R1")
        self.assertEqual(call.budget_total, Decimal("17000000"))
        self.assertEqual(call.expected_grants, 2)
        self.assertEqual(call.types_of_action, "HORIZON Innovation Actions")
        self.assertEqual(call.cross_cutting_priorities, ["AI", "DigitalAgenda"])

    def test_cascade_call_gets_budget_and_description_from_its_own_fields(self, _facets):
        """Uma cascade call (tipo 8) usa `budget` e `description`, não budgetOverview/Byte."""
        self._run_sync([[_result(
            "R1", "ID-1", call_type="8",
            budget=["4200000"],
            description=["<p>Open call for sub-grants</p>"],
            projectName=["European Partnership on Innovative SMEs"],
            duration=["36 months"],
        )]])
        call = Call.objects.get(reference="R1")
        self.assertEqual(call.budget_total, Decimal("4200000"))
        self.assertEqual(call.description, "<p>Open call for sub-grants</p>")
        detail = services.serialize_call_detail(call)
        self.assertEqual(detail["project_name"], "European Partnership on Innovative SMEs")
        self.assertEqual(detail["duration"], "36 months")

    def test_unknown_programme_code_leaves_the_call_importable(self, _facets):
        """Um programa fora do catálogo não pode fazer a call desaparecer."""
        self._run_sync([[_result("R1", "ID-1", programme="99999999")]])
        call = Call.objects.get(reference="R1")
        self.assertIsNone(call.programme)

    def test_result_without_a_metadata_key_still_fills_the_raw_columns(self, _facets):
        """5 das 1339 calls reais (tipo 8) não têm "metadata": os campos vêm soltos no
        topo. Sem o fallback em `_field()`, estas calls ficavam com as colunas cruas vazias
        apesar dos dados existirem no resultado."""
        flat_result = {
            "reference": "R-FLAT",
            "identifier": ["ID-FLAT"],
            "title": ["Sem metadata"],
            "status": ["31094502"],
            "type": ["8"],
            "frameworkProgramme": ["43108390"],
            "startDate": ["2026-12-08T00:00:00.000+0000"],
            "url": ["https://example.test/ID-FLAT"],
            "apiVersion": "2.155",
            "checksum": ["ABC123"],
        }
        self._run_sync([[flat_result]])
        call = Call.objects.get(reference="R-FLAT")
        self.assertEqual(call.identifier, "ID-FLAT")
        self.assertEqual(call.status, "31094502")
        self.assertEqual(call.api_version, "2.155")
        self.assertEqual(call.checksum, "ABC123")

    def test_all_raw_fields_are_populated_from_a_full_result(self, _facets):
        """Cobertura: cada campo que a API pode mandar tem de aterrar nalguma coluna."""
        result = _result(
            "R1", "ID-1",
            DATASOURCE=["SEDIA"], actions=[json.dumps([{"status": {"id": 1}}])],
            allowPartnerSearch=["true"], beneficiaryAdministration=["<p>x</p>"],
            budget=["1000"], budgetOverview=[json.dumps({"a": 1})],
            caName=["Entidade"], callIdentifier=["CALL-1"], callTitle=["Título"],
            callccm2Id=["111"], ccm2Id=["222"], ccmTags=["T1"], ccmTags2=["T2"],
            cenTagsA=["TOPICS-1"], cftId=["0"], closingDate=["2026-01-01T00:00:00Z"],
            contractType=["42893160"], crossCuttingPriorities=["AI"], currency=["EUR"],
            datasource=["SEDIA_X"], deadlineModel=["single-stage"],
            description=["<p>desc</p>"], descriptionByte=["<p>db</p>"],
            destination=["48080639"], destinationDescription=["dd"],
            destinationDetails=["<p>details</p>"], destinationGroup=["999"],
            duration=["36 months"], esDA_FirstIngestDate=["2026-01-01T00:00:00.000+0100"],
            esDA_IngestDate=["2026-01-02T00:00:00.000+0100"],
            esDA_QueueDate=["2026-01-03T00:00:00.000+0100"], esIN_ccmTags=["I1"],
            esIN_ccmTags2=["I2"], esST_FileName=["file.txt"], esST_URL=["https://x"],
            esST_checksum=["CHK"], es_Combine=["2"], es_ContentType=["text/plain"],
            es_SortDate=["2026-01-04T00:00:00.000+0100"], focusArea=[],
            furtherInformation=["<p>fi</p>"], geographicalZone=["20001034"],
            geographicalZones=["20001034"], keywords=["k1"], language=["en"],
            latestInfos=["[]"], links=["[]"], mission=["44798839"],
            missionGroup=["45355175"], programmeDivision=["43108541"],
            programmeDivisionProspect=["42921320"], programmePeriod=["2021 - 2027"],
            projectAcronym=["ACR"], projectId=["999"], projectName=["Nome Projeto"],
            publicationDocuments=[json.dumps([{"docUrl": "https://x/doc"}])],
            sepTemplate=["<p>submeter</p>"], sortStatus=["2"], specificObjective=["SO1"],
            supportInfo=["<p>apoio</p>"], tags=["tag1"], topicConditions=["<p>cond</p>"],
            typeOfMGAs=["43027849"], typesOfAction=["Ação X"],
            updateDate=["2026-01-05T00:00:00.000+0100"],
        )
        self._run_sync([[result]])
        call = Call.objects.get(reference="R1")
        # Amostra representativa dos 90 campos — não todos, mas um de cada categoria.
        self.assertEqual(call.es_datasource, "SEDIA")
        self.assertEqual(call.ca_name, "Entidade")
        self.assertEqual(call.ccm_tags, ["T1"])
        self.assertEqual(call.cen_tags_a, ["TOPICS-1"])
        self.assertEqual(call.currency, "EUR")
        self.assertEqual(call.destination_code, "48080639")
        self.assertEqual(call.mission_code, "44798839")
        self.assertEqual(call.programme_division_codes, ["43108541"])
        self.assertEqual(call.sort_status, "2")
        self.assertEqual(call.type_of_mgas, ["43027849"])
        self.assertEqual(call.topic_conditions_raw, "<p>cond</p>")
        self.assertEqual(call.support_info_raw, "<p>apoio</p>")
        self.assertEqual(call.sep_template, "<p>submeter</p>")
        self.assertEqual(call.corporate_search_version, "")  # não veio neste resultado
        self.assertIsNotNone(call.es_sort_date)
        self.assertIsNotNone(call.es_first_ingest_date)


class CallUrlTests(TestCase):
    """A cascade funding calls (tipo 8) monta-se o url a partir do project_acronym + status —
    a API devolve formatos inconsistentes para essa dimensão, e o acrónimo (o NOME do
    projeto, ex: "PHORTIFY") é a palavra-chave que a pesquisa do portal usa, ao contrário do
    identifier técnico."""

    def test_type_8_with_project_acronym_builds_the_calls_for_proposals_url(self):
        url = services._call_url("8", "PHORTIFY", "31094502", "https://api.example/original")
        self.assertEqual(
            url,
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
            "opportunities/calls-for-proposals?order=DESC&sortBy=startDate"
            "&keywords=PHORTIFY&status=31094502",
        )

    def test_type_8_without_project_acronym_falls_back_to_the_api_url(self):
        url = services._call_url("8", "", "31094502", "https://api.example/original")
        self.assertEqual(url, "https://api.example/original")

    def test_other_types_keep_the_api_url_even_with_a_project_acronym(self):
        # Não é o tipo 8 → não é isto que decide o url, mesmo havendo acrónimo.
        url = services._call_url("1", "PHORTIFY", "31094502", "https://api.example/grant")
        self.assertEqual(url, "https://api.example/grant")

    def test_forthcoming_status_is_reflected_in_the_url(self):
        url = services._call_url("8", "PHORTIFY", "31094501", "https://api.example/original")
        self.assertIn("status=31094501", url)

    def test_acronym_with_spaces_and_punctuation_is_url_encoded(self):
        # Acrónimos reais trazem espaços e pontuação ("Cultural Horizons",
        # "BIRDS in BG - 2") — sem codificar, a query string ficava tecnicamente inválida.
        url = services._call_url(
            "8", "BIRDS in BG - 2", "31094502", "https://api.example/original",
        )
        self.assertNotIn(" ", url)
        self.assertIn("keywords=BIRDS%20in%20BG%20-%202", url)

    def test_sync_applies_the_built_url_to_cascade_calls(self):
        with patch.object(services, "_fetch_facets", return_value=FACETS), \
             patch.object(services, "_fetch_page", side_effect=[
                 [_result("R1", "ID-1", call_type="8", status="31094502",
                          projectAcronym=["PHORTIFY"])], [],
             ]):
            services.sync_calls()
        call = Call.objects.get(reference="R1")
        self.assertEqual(
            call.url,
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
            "opportunities/calls-for-proposals?order=DESC&sortBy=startDate"
            "&keywords=PHORTIFY&status=31094502",
        )

    def test_sync_falls_back_when_the_cascade_call_has_no_acronym(self):
        with patch.object(services, "_fetch_facets", return_value=FACETS), \
             patch.object(services, "_fetch_page", side_effect=[
                 [_result("R1", "ID-1", call_type="8", status="31094502")], [],
             ]):
            services.sync_calls()
        call = Call.objects.get(reference="R1")
        self.assertEqual(call.url, "https://example.test/ID-1")


class BudgetExtractionTests(TestCase):
    """O orçamento tem de ser o DESTA call, não o da call-mãe inteira."""

    OVERVIEW = {
        "budgetTopicActionMap": {
            "1": [{
                "action": "HORIZON-X-01 - HORIZON-IA Innovation Actions",
                "expectedGrants": 2, "minContribution": 8500000, "maxContribution": 8500000,
                "budgetYearMap": {"2027": "17000000"},
            }],
            # Topic IRMÃO, na mesma call-mãe: não pode entrar na conta desta call.
            "2": [{
                "action": "HORIZON-X-02 - HORIZON-CSA Coordination",
                "expectedGrants": 1, "minContribution": 6000000, "maxContribution": 6000000,
                "budgetYearMap": {"2027": "900000000"},
            }],
        }
    }

    def test_only_this_topics_budget_is_taken(self):
        metadata = {"budgetOverview": [json.dumps(self.OVERVIEW)]}
        budget = services._budget(metadata, "HORIZON-X-01")
        self.assertEqual(budget["budget_total"], Decimal("17000000"))
        self.assertEqual(budget["min_contribution"], Decimal("8500000"))
        self.assertEqual(budget["expected_grants"], 2)

    def test_a_longer_identifier_does_not_match_a_shorter_one(self):
        """HORIZON-X-01 não pode apanhar o orçamento de HORIZON-X-01-02 (nem o inverso)."""
        overview = {"budgetTopicActionMap": {"1": [{
            "action": "HORIZON-X-01-02 - HORIZON-IA", "budgetYearMap": {"2027": "5000"},
        }]}}
        budget = services._budget({"budgetOverview": [json.dumps(overview)]}, "HORIZON-X-01")
        self.assertIsNone(budget["budget_total"])

    def test_sums_every_year(self):
        overview = {"budgetTopicActionMap": {"1": [{
            "action": "A-1 - X", "budgetYearMap": {"2026": "1000", "2027": "2500"},
        }]}}
        budget = services._budget({"budgetOverview": [json.dumps(overview)]}, "A-1")
        self.assertEqual(budget["budget_total"], Decimal("3500"))

    def test_cascade_and_proposals_use_the_simple_budget_field(self):
        """Tipos 8 e 2 não trazem budgetOverview — o valor vem em `budget`, já pronto."""
        budget = services._budget({"budget": ["86439583"]}, "HORIZON-EIE-2021-INNOVSMES-01-01")
        self.assertEqual(budget["budget_total"], Decimal("86439583"))
        # Sem gama nem nº de grants: a API não os publica neste formato.
        self.assertIsNone(budget["min_contribution"])
        self.assertIsNone(budget["expected_grants"])

    def test_a_zero_budget_is_treated_as_not_published(self):
        """Uma cascade call traz budget='0' — mostrar "0 €" seria enganador."""
        self.assertIsNone(services._budget({"budget": ["0"]}, "A-1")["budget_total"])

    def test_budget_overview_wins_when_both_could_exist(self):
        overview = {"budgetTopicActionMap": {"1": [{
            "action": "A-1 - X", "budgetYearMap": {"2027": "17000000"},
        }]}}
        budget = services._budget(
            {"budgetOverview": [json.dumps(overview)], "budget": ["999"]}, "A-1",
        )
        self.assertEqual(budget["budget_total"], Decimal("17000000"))

    def test_missing_or_broken_budget_is_not_an_error(self):
        self.assertIsNone(services._budget({}, "A-1")["budget_total"])
        self.assertIsNone(
            services._budget({"budgetOverview": ["não é json"]}, "A-1")["budget_total"]
        )

    def test_json_field_decodes_string_wrapped_json(self):
        self.assertEqual(services._json_field('{"a": 1}'), {"a": 1})
        self.assertEqual(services._json_field([1, 2]), [1, 2])
        self.assertIsNone(services._json_field("lixo"))
        self.assertIsNone(services._json_field(""))


class CodeLabelTests(TestCase):
    """Tradução dos códigos numéricos da API pelos nomes das facetas."""

    CATALOG = {
        ("destination", "48080639"): "Sustainable energy supply",
        ("destination", "48080651"): "48080651",   # a API não publica nome para este
        ("mission", "44798839"): "Mission: Ocean, seas and waters",
    }

    def test_known_codes_become_names(self):
        names = services._labels(
            {"destination": ["48080639"]}, "destination", "destination", self.CATALOG,
        )
        self.assertEqual(names, ["Sustainable energy supply"])

    def test_untranslated_codes_are_omitted(self):
        """Mostrar "48080651" ao utilizador é pior do que não mostrar nada."""
        names = services._labels(
            {"destination": ["48080651"]}, "destination", "destination", self.CATALOG,
        )
        self.assertEqual(names, [])

    def test_unknown_code_is_omitted(self):
        names = services._labels(
            {"destination": ["99999"]}, "destination", "destination", self.CATALOG,
        )
        self.assertEqual(names, [])

    def test_url_encoded_labels_are_decoded(self):
        self.assertEqual(
            services._clean_label("Marie Sk%C5%82odowska-Curie Actions"),
            "Marie Skłodowska-Curie Actions",
        )
        # Um '%' legítimo não pode ser corrompido.
        self.assertEqual(services._clean_label("100% financiado"), "100% financiado")

    def test_catalog_is_synced_from_the_facets(self):
        facets = {
            "destination": [{"rawValue": "48080639", "value": "Sustainable energy supply"}],
            "mission": [{"rawValue": "44798839", "value": "Mission: Ocean"}],
        }
        summary = services._sync_code_labels(facets)
        self.assertEqual(summary["created"], 2)
        self.assertEqual(
            CodeLabel.objects.get(facet="destination", code="48080639").label,
            "Sustainable energy supply",
        )
        # Correr outra vez não duplica.
        self.assertEqual(services._sync_code_labels(facets)["created"], 0)

    def test_a_long_programme_division_code_fits(self):
        """Em programmeDivision o rawValue chega a ser o próprio nome (63 caracteres)."""
        long_code = "Food, Bioeconomy Natural Resources, Agriculture and Environment"
        services._sync_code_labels(
            {"programmeDivision": [{"rawValue": long_code, "value": long_code}]}
        )
        self.assertTrue(
            CodeLabel.objects.filter(facet="programmeDivision", code=long_code).exists()
        )


class DocumentTests(TestCase):
    """Documentos oficiais da call (guias de candidatura, anexos)."""

    def test_documents_are_normalised(self):
        payload = json.dumps([{
            "nameDoc": "Guidelines for grant applicant",
            "typeDoc": "rtf", "languageDoc": "EN",
            "finalPublicDocDate": "2026-09-11 19:30:56.0",
            "docUrl": "https://example.test/doc.rtf",
        }])
        documents = services._documents({"publicationDocuments": [payload]})
        self.assertEqual(documents, [{
            "name": "Guidelines for grant applicant", "type": "rtf", "language": "EN",
            "published_at": "2026-09-11 19:30:56.0", "url": "https://example.test/doc.rtf",
        }])

    def test_documents_without_a_url_are_dropped(self):
        payload = json.dumps([{"nameDoc": "sem link"}, {"docUrl": "https://example.test/a"}])
        documents = services._documents({"publicationDocuments": [payload]})
        self.assertEqual(len(documents), 1)

    def test_missing_or_broken_documents_are_not_an_error(self):
        self.assertEqual(services._documents({}), [])
        self.assertEqual(services._documents({"publicationDocuments": ["lixo"]}), [])

    def test_boolean_parsing(self):
        self.assertIs(services._boolean("true"), True)
        self.assertIs(services._boolean("false"), False)
        self.assertIsNone(services._boolean(""))
        self.assertIsNone(services._boolean(None))


class ListingTests(TestCase):
    """Filtros e ordenação da listagem."""

    def setUp(self):
        self.programme = ProgrammeType.objects.create(code="43108390", name="Horizon Europe")
        self.other = ProgrammeType.objects.create(code="43252405", name="LIFE")
        self.grant = CallType.objects.create(code="1", name="Grant")
        self.cascade = CallType.objects.create(code="8", name="Cascade funding calls")
        now = timezone.now()
        self.with_deadline = Call.objects.create(
            reference="R1", identifier="ID-1", status=Call.STATUS_OPEN,
            call_type=self.grant, programme=self.programme,
            start_date=now, deadline_date=now + timedelta(days=5),
        )
        self.without_deadline = Call.objects.create(
            reference="R2", identifier="ID-2", status=Call.STATUS_OPEN,
            call_type=self.cascade, programme=self.other, start_date=now, deadline_date=None,
        )
        self.forthcoming = Call.objects.create(
            reference="R3", identifier="ID-3", status=Call.STATUS_FORTHCOMING,
            call_type=self.grant, programme=self.programme,
            start_date=now, deadline_date=now + timedelta(days=50),
        )

    def test_filters(self):
        self.assertEqual(services.list_calls(status=Call.STATUS_OPEN).count(), 2)
        self.assertEqual(services.list_calls(programme_code="43252405").count(), 1)
        self.assertEqual(services.list_calls(type_code="8").count(), 1)
        self.assertEqual(
            services.list_calls(status=Call.STATUS_OPEN, type_code="1").count(), 1
        )

    def test_calls_without_a_deadline_are_ordered_last(self):
        ordered = list(services.list_calls(order_by="deadline_earliest"))
        self.assertEqual(ordered[0], self.with_deadline)
        self.assertEqual(ordered[-1], self.without_deadline)

    def test_search_covers_title_and_identifier(self):
        self.with_deadline.title = "Hydropower demonstration"
        self.with_deadline.save(update_fields=["title"])
        self.assertEqual(services.list_calls(search="hydropower").count(), 1)
        self.assertEqual(services.list_calls(search="ID-2").count(), 1)
        self.assertEqual(services.list_calls(search="nada-disto").count(), 0)

    def test_priority_and_action_type_filters(self):
        self.with_deadline.cross_cutting_priorities = ["AI", "DigitalAgenda"]
        self.with_deadline.types_of_action = "HORIZON Innovation Actions"
        self.with_deadline.save(update_fields=["cross_cutting_priorities", "types_of_action"])
        self.assertEqual(services.list_calls(priority="AI").count(), 1)
        self.assertEqual(services.list_calls(priority="SSH").count(), 0)
        self.assertEqual(services.list_calls(action_type="Innovation").count(), 1)

    def test_tag_mission_and_documents_filters(self):
        self.with_deadline.tags = ["Artificial intelligence", "security"]
        self.with_deadline.mission = "Mission: Cancer"
        self.with_deadline.documents = [{"name": "Guia", "url": "https://example.test/a"}]
        self.with_deadline.save(update_fields=["tags", "mission", "documents"])
        self.assertEqual(services.list_calls(tag="security").count(), 1)
        self.assertEqual(services.list_calls(tag="inexistente").count(), 0)
        self.assertEqual(services.list_calls(mission="Cancer").count(), 1)
        self.assertEqual(services.list_calls(has_documents=True).count(), 1)
        self.assertEqual(services.list_calls(has_documents=False).count(), 2)

    def test_search_also_covers_the_entity_and_project(self):
        self.with_deadline.ca_name = "European Partnership on Innovative SMEs"
        self.with_deadline.save(update_fields=["ca_name"])
        self.assertEqual(services.list_calls(search="Innovative SMEs").count(), 1)

    def test_listing_sends_the_document_count_not_the_files(self):
        self.with_deadline.documents = [
            {"name": "a", "url": "https://example.test/a"},
            {"name": "b", "url": "https://example.test/b"},
        ]
        self.with_deadline.save(update_fields=["documents"])
        payload = services.serialize_call(self.with_deadline)
        self.assertEqual(payload["documents_count"], 2)
        self.assertNotIn("documents", payload)
        # O detalhe traz os ficheiros todos.
        self.assertEqual(len(services.serialize_call_detail(self.with_deadline)["documents"]), 2)

    def test_has_budget_filter(self):
        self.with_deadline.budget_total = Decimal("17000000")
        self.with_deadline.save(update_fields=["budget_total"])
        self.assertEqual(services.list_calls(has_budget=True).count(), 1)
        self.assertEqual(services.list_calls(has_budget=False).count(), 2)
        # None = não filtrar
        self.assertEqual(services.list_calls(has_budget=None).count(), 3)

    def test_budget_ordering_puts_calls_without_budget_last(self):
        self.with_deadline.budget_total = Decimal("100")
        self.with_deadline.save(update_fields=["budget_total"])
        ordered = list(services.list_calls(order_by="budget_highest"))
        self.assertEqual(ordered[0], self.with_deadline)
        self.assertIsNone(ordered[-1].budget_total)

    def test_detail_decodes_string_wrapped_json(self):
        # O detalhe lê das colunas próprias (budget_overview, topic_conditions_raw), não do
        # raw diretamente — ver serialize_call_detail.
        self.with_deadline.budget_overview = json.dumps({"budgetYearsColumns": ["2027"]})
        self.with_deadline.topic_conditions_raw = "<p>condições</p>"
        self.with_deadline.raw = {"reference": "R1", "metadata": {}}
        self.with_deadline.save(
            update_fields=["budget_overview", "topic_conditions_raw", "raw"]
        )
        payload = services.serialize_call_detail(self.with_deadline)
        self.assertEqual(payload["budget_overview"], {"budgetYearsColumns": ["2027"]})
        self.assertEqual(payload["topic_conditions"], "<p>condições</p>")
        self.assertIn("raw", payload)
        self.assertEqual(payload["raw"]["reference"], "R1")  # é o envelope, não só metadata
        self.assertIn("raw_fields", payload)  # os 90 campos crus, um a um

    def test_budget_source_distinguishes_the_two_formats(self):
        payload = services.serialize_call(self.without_deadline)
        self.assertIsNone(payload["budget_source"])  # sem orçamento

        self.without_deadline.budget_total = Decimal("4200000")
        self.without_deadline.save(update_fields=["budget_total"])
        payload = services.serialize_call(self.without_deadline)
        self.assertEqual(payload["budget_source"], "simple")

        self.without_deadline.min_contribution = Decimal("100000")
        self.without_deadline.save(update_fields=["min_contribution"])
        payload = services.serialize_call(self.without_deadline)
        self.assertEqual(payload["budget_source"], "detailed")

    def test_serialize_exposes_the_budget_as_numbers(self):
        self.with_deadline.budget_total = Decimal("17000000.50")
        self.with_deadline.save(update_fields=["budget_total"])
        payload = services.serialize_call(self.with_deadline)
        self.assertEqual(payload["budget_total"], 17000000.50)
        self.assertIsInstance(payload["budget_total"], float)

    def test_filter_option_helpers(self):
        self.with_deadline.types_of_action = "HORIZON Innovation Actions"
        self.with_deadline.cross_cutting_priorities = ["AI"]
        self.with_deadline.save(update_fields=["types_of_action", "cross_cutting_priorities"])
        self.assertEqual(services.distinct_action_types(), ["HORIZON Innovation Actions"])
        self.assertEqual(services.distinct_priorities(), ["AI"])

    def test_filter_options_have_no_duplicates(self):
        """O Meta.ordering entrava no SELECT DISTINCT e devolvia uma opção POR CALL."""
        for call in (self.with_deadline, self.without_deadline, self.forthcoming):
            call.types_of_action = "HORIZON Innovation Actions"
            call.mission = "Mission: Cancer"
            call.destination = "Clean energy"
            call.save(update_fields=["types_of_action", "mission", "destination"])
        self.assertEqual(services.distinct_action_types(), ["HORIZON Innovation Actions"])
        self.assertEqual(services.distinct_missions(), ["Mission: Cancer"])
        self.assertEqual(services.distinct_destinations(), ["Clean energy"])

    def test_top_tags_are_ranked_and_capped(self):
        self.with_deadline.tags = ["AI", "security"]
        self.without_deadline.tags = ["AI"]
        self.with_deadline.save(update_fields=["tags"])
        self.without_deadline.save(update_fields=["tags"])
        self.assertEqual(services.top_tags(), ["AI", "security"])  # AI é mais frequente
        self.assertEqual(services.top_tags(limit=1), ["AI"])

    def test_serialize_resolves_the_catalog_names(self):
        payload = services.serialize_call(self.with_deadline)
        self.assertEqual(payload["programme"], "Horizon Europe")
        self.assertEqual(payload["type"], "Grant")
        self.assertEqual(payload["status_label"], "Open")
        self.assertNotIn("raw", payload)  # a listagem não carrega a metadata completa


class SyncRouteTests(TestCase):
    """A rota que o n8n chama — SEM autenticação (decisão explícita: fica mais simples de
    chamar, à custa de ficar aberta a quem souber o endereço)."""

    def setUp(self):
        self.client = Client()

    def test_get_is_not_allowed(self):
        response = self.client.get("/calls/sync/")
        self.assertEqual(response.status_code, 405)

    @patch.object(services, "sync_calls", return_value={"created": 3})
    def test_post_without_any_credentials_runs(self, mocked):
        response = self.client.post("/calls/sync/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        mocked.assert_called_once()

    @patch.object(services, "sync_calls", side_effect=services.CallsSyncError("API em baixo"))
    def test_api_failure_is_reported_as_502(self, _mocked):
        response = self.client.post("/calls/sync/")
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["success"])


class ListingRouteTests(TestCase):
    """As rotas de leitura continuam a exigir sessão."""

    def test_listing_requires_authentication(self):
        self.assertEqual(Client().get("/calls/").status_code, 401)

    def test_filters_require_authentication(self):
        self.assertEqual(Client().get("/calls/filters/").status_code, 401)


class CallEditViewTests(TestCase):
    """Permissões, validação e auditoria da edição de uma call (/calls/<pk>/edit/)."""

    def setUp(self):
        self.programme = ProgrammeType.objects.create(code="43108390", name="Horizon Europe")
        self.call_type = CallType.objects.create(code="1", name="Grant")
        self.call = Call.objects.create(
            reference="R-EDIT-1", identifier="ID-EDIT-1", title="Título original",
            status=Call.STATUS_OPEN, call_type=self.call_type, programme=self.programme,
        )
        self.admin = User.objects.create_user(
            "admin_calls", email="a@x.pt", password=TEST_PASSWORD)
        self.admin.profile.role = UserProfile.ADMIN
        self.admin.profile.save()
        self.client_user = User.objects.create_user("cliente_calls", password=TEST_PASSWORD)
        # o signal já cria o perfil com role=client

    def _edit(self, payload):
        return self.client.put(
            f"/calls/{self.call.pk}/edit/",
            data=json.dumps(payload), content_type="application/json",
        )

    def test_anonymous_gets_401(self):
        self.assertEqual(self._edit({"title": "X"}).status_code, 401)

    def test_client_role_gets_403(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self._edit({"title": "X"}).status_code, 403)

    def test_unknown_call_gets_404(self):
        self.client.force_login(self.admin)
        resp = self.client.put(
            "/calls/999999/edit/", data=json.dumps({"title": "X"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_post_is_not_allowed(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            f"/calls/{self.call.pk}/edit/", data=json.dumps({"title": "X"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 405)

    def test_admin_edits_and_audit_logs_who_and_what(self):
        self.client.force_login(self.admin)
        with self.assertLogs("calls.audit", level="INFO") as logs:
            resp = self._edit({"title": "Título corrigido", "id": 999})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["updated"], ["title"])
        self.assertEqual(body["ignored"], ["id"])  # id nunca é editável
        self.call.refresh_from_db()
        self.assertEqual(self.call.title, "Título corrigido")
        self.assertIn("admin_calls", logs.output[0])
        self.assertIn("'Título original'", logs.output[0])
        self.assertIn("'Título corrigido'", logs.output[0])

    def test_edit_marks_manual_source_and_editor(self):
        self.client.force_login(self.admin)
        self._edit({"title": "Novo título"})
        self.call.refresh_from_db()
        self.assertEqual(self.call.last_update_source, Call.SOURCE_MANUAL)
        self.assertEqual(self.call.last_updated_by, self.admin)

    def test_raw_api_fields_are_not_editable(self):
        # api_version/checksum/es_* e afins não estão na whitelist — não fazem sentido
        # editados à mão e seriam repostos no sync seguinte.
        self.client.force_login(self.admin)
        resp = self._edit({"api_version": "9.9.9", "checksum": "forjado"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["updated"], [])
        self.assertEqual(set(body["ignored"]), {"api_version", "checksum"})
        self.call.refresh_from_db()
        self.assertEqual(self.call.api_version, "")

    def test_fk_fields_are_not_editable(self):
        # call_type/programme (FK) ficam fora — não há como o payload identificar
        # univocamente o catálogo certo.
        self.client.force_login(self.admin)
        resp = self._edit({"call_type": 999, "programme": 999})
        self.assertEqual(resp.json()["ignored"], ["call_type", "programme"])

    def test_edit_multiple_fields(self):
        self.client.force_login(self.admin)
        resp = self._edit({"status": Call.STATUS_FORTHCOMING, "url": "https://x.test/novo"})
        self.assertEqual(resp.status_code, 200)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, Call.STATUS_FORTHCOMING)
        self.assertEqual(self.call.url, "https://x.test/novo")

    def test_invalid_value_returns_400_not_500(self):
        self.client.force_login(self.admin)
        resp = self._edit({"budget_total": "não é um número"})
        self.assertEqual(resp.status_code, 400)
        self.call.refresh_from_db()
        self.assertIsNone(self.call.budget_total)  # nada foi gravado

    def test_no_changes_does_not_error(self):
        self.client.force_login(self.admin)
        resp = self._edit({"title": "Título original"})  # mesmo valor
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["updated"], ["title"])  # está na whitelist, só sem diff

    def test_a_sync_afterwards_overwrites_the_manual_edit(self):
        # Confirma o comportamento pretendido: a edição manual NÃO trava o próximo sync —
        # o sync é sempre a origem mais recente quando corre (mesmo padrão de avisos.Grant).
        self.client.force_login(self.admin)
        self._edit({"title": "Editado à mão"})
        self.call.refresh_from_db()
        self.assertEqual(self.call.last_update_source, Call.SOURCE_MANUAL)

        with patch.object(services, "_fetch_facets", return_value=FACETS), \
             patch.object(services, "_fetch_page", side_effect=[
                 [_result(
                     "R-EDIT-1", "ID-EDIT-1", status=Call.STATUS_OPEN, title=["Da API"],
                 )], [],
             ]):
            services.sync_calls()
        self.call.refresh_from_db()
        self.assertEqual(self.call.title, "Da API")
        self.assertEqual(self.call.last_update_source, Call.SOURCE_API)
        self.assertIsNone(self.call.last_updated_by)


class CallSerializationSourceTests(TestCase):
    """last_update_source/last_updated_by aparecem na resposta (listagem e detalhe)."""

    def test_serialize_call_exposes_source_fields(self):
        programme = ProgrammeType.objects.create(code="43108390", name="Horizon Europe")
        call_type = CallType.objects.create(code="1", name="Grant")
        editor = User.objects.create_user("editora_calls", password=TEST_PASSWORD)
        call = Call.objects.create(
            reference="R-SER-1", identifier="ID-SER-1", status=Call.STATUS_OPEN,
            call_type=call_type, programme=programme,
            last_update_source=Call.SOURCE_MANUAL, last_updated_by=editor,
        )
        payload = services.serialize_call(call)
        self.assertEqual(payload["last_update_source"], Call.SOURCE_MANUAL)
        self.assertEqual(payload["last_updated_by"], "editora_calls")

    def test_default_source_is_api(self):
        programme = ProgrammeType.objects.create(code="43108390", name="Horizon Europe")
        call_type = CallType.objects.create(code="1", name="Grant")
        call = Call.objects.create(
            reference="R-SER-2", identifier="ID-SER-2", status=Call.STATUS_OPEN,
            call_type=call_type, programme=programme,
        )
        payload = services.serialize_call(call)
        self.assertEqual(payload["last_update_source"], Call.SOURCE_API)
        self.assertIsNone(payload["last_updated_by"])
