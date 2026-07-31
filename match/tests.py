"""
Tests for the match engine's pure rule layer: EU SME dimension classification and
the hard eligibility filter (region + CAE + dimension). No database is touched —
these exercise scoring_rules directly, so they run fast and offline.
"""

import json
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from match.scoring_rules import (
    classify_dimension,
    grant_allowed_dimensions,
    grant_allowed_entity_types,
    eligible_cae,
    eligible_location,
    eligible_dimension,
    is_eligible,
    missing_required_fields,
    match_cae,
    match_entity_type,
    _normalize,
)
from datetime import datetime

from match.services import (
    NifMatchingService, MissingClientDataError, _next_nif_key,
    _company_sector_text, _company_general_text,
)
from match.disposable_email import is_disposable_email
from match.models import NifCompany
from match import grant_embeddings
from match.grant_embeddings import build_general_embedding_text, build_sector_embedding_text
from match.ranking import (
    active_phase_id, company_area_id, effective_budget_rate,
    max_financing_rate_from_rates,
)
from users.models import UserProfile
from avisos.models import Grant, GrantEmbedding


class ClassifyDimensionTests(SimpleTestCase):
    """EU SME thresholds: micro <10 & ≤2M, pequena <50 & ≤10M, media <250 & ≤50M."""

    def test_micro(self):
        self.assertEqual(classify_dimension(5, 1_000_000), "micro")
        self.assertEqual(classify_dimension(9, 2_000_000), "micro")

    def test_revenue_promotes_over_headcount(self):
        # 9 workers would be micro, but 2.5M turnover pushes it to pequena.
        self.assertEqual(classify_dimension(9, 2_500_000), "pequena")
        self.assertEqual(classify_dimension(40, 12_000_000), "media")

    def test_pequena_and_media(self):
        self.assertEqual(classify_dimension(40, 9_000_000), "pequena")
        self.assertEqual(classify_dimension(200, 40_000_000), "media")

    def test_grande_by_headcount_or_turnover(self):
        self.assertEqual(classify_dimension(300, 10_000), "grande")   # ≥250 workers
        self.assertEqual(classify_dimension(5, 60_000_000), "grande")  # >50M turnover

    def test_unknown_and_partial(self):
        self.assertIsNone(classify_dimension(None, None))
        self.assertEqual(classify_dimension(None, 500_000), "micro")
        self.assertEqual(classify_dimension(5, None), "micro")

    def test_garbage_revenue_is_ignored(self):
        self.assertEqual(classify_dimension(5, "not-a-number"), "micro")


class GrantAllowedDimensionsTests(SimpleTestCase):

    def test_pme_expands_to_the_three_sizes(self):
        self.assertEqual(grant_allowed_dimensions("apoio a pme"),
                         {"micro", "pequena", "media"})

    def test_specific_sizes(self):
        self.assertEqual(grant_allowed_dimensions("micro e pequenas empresas"),
                         {"micro", "pequena"})
        self.assertEqual(grant_allowed_dimensions("destinado a grandes empresas"),
                         {"grande"})

    def test_no_restriction_is_empty_set(self):
        self.assertEqual(grant_allowed_dimensions(""), set())


class EligibilityCriterionTests(SimpleTestCase):
    """Each criterion is permissive when the grant states no restriction on it."""

    def test_cae_unrestricted_grant_is_open(self):
        client = {"cae_codes": ["10120"]}
        grant = {"included_caes": [], "excluded_caes": []}
        self.assertTrue(eligible_cae(client, grant))

    def test_cae_included_must_match(self):
        grant = {"included_caes": ["62***"], "excluded_caes": []}
        self.assertTrue(eligible_cae({"cae_codes": ["62010"]}, grant))
        self.assertFalse(eligible_cae({"cae_codes": ["10120"]}, grant))

    def test_location_unrestricted_grant_is_open(self):
        self.assertTrue(eligible_location({"region": "Algarve"}, {"eligible_regions": []}))

    def test_location_must_match(self):
        grant = {"eligible_regions": ["Norte"]}
        self.assertTrue(eligible_location({"region": "Norte"}, grant))
        self.assertFalse(eligible_location({"region": "Algarve"}, grant))

    def test_dimension_unknown_never_excludes(self):
        grant = {"eligibility_text": "apoio a pme"}
        self.assertTrue(eligible_dimension({"dimension": None}, grant))

    def test_dimension_grande_excluded_from_pme_grant(self):
        grant = {"eligibility_text": "apoio a pme"}
        self.assertFalse(eligible_dimension({"dimension": "grande"}, grant))
        self.assertTrue(eligible_dimension({"dimension": "micro"}, grant))


class IsEligibleTests(SimpleTestCase):
    """The hard filter: eligible only if it passes every criterion the grant sets."""

    GRANT = {
        "included_caes": ["62***"], "excluded_caes": [],
        "eligible_regions": ["Norte"], "eligibility_text": "apoio a pme",
    }

    def test_wrong_region_is_excluded(self):
        client = {"cae_codes": ["62010"], "region": "Algarve", "dimension": "micro"}
        self.assertFalse(is_eligible(client, self.GRANT)[0])

    def test_wrong_cae_is_excluded(self):
        client = {"cae_codes": ["10120"], "region": "Norte", "dimension": "micro"}
        self.assertFalse(is_eligible(client, self.GRANT)[0])

    def test_grande_in_pme_grant_is_excluded(self):
        client = {"cae_codes": ["62010"], "region": "Norte", "dimension": "grande"}
        self.assertFalse(is_eligible(client, self.GRANT)[0])

    def test_fully_matching_client_is_eligible(self):
        client = {"cae_codes": ["62010"], "region": "Norte", "dimension": "micro"}
        eligible, breakdown = is_eligible(client, self.GRANT)
        self.assertTrue(eligible)
        self.assertEqual({b["criterion"] for b in breakdown},
                         {"cae", "location", "dimension", "entity_type"})
        self.assertTrue(all(b["eligible"] for b in breakdown))

    def test_wrong_entity_type_is_excluded(self):
        # Aviso só para municípios (quem se pode candidatar) → uma empresa não é elegível.
        grant = {"included_caes": [], "excluded_caes": [], "eligible_regions": [],
                 "beneficiary_text": "sao beneficiarios os municipios"}
        client = {"cae_codes": ["62010"], "region": "Norte", "entity_type": "empresa"}
        self.assertFalse(is_eligible(client, grant)[0])
        municipio = {"region": "Norte", "entity_type": "municipio"}
        self.assertTrue(is_eligible(municipio, grant)[0])

    def test_legal_boilerplate_does_not_grant_entity_type(self):
        # Caso real (PACS-2026-12): a cláusula legal "não ser uma empresa em dificuldade" é uma
        # EXCLUSÃO, não diz que o aviso é para empresas. Só o beneficiary_text conta — e aí o
        # beneficiário são "entidades gestoras de RU", que não é nenhum tipo do vocabulário.
        grant = {
            "included_caes": [], "excluded_caes": [], "eligible_regions": [],
            "eligibility_text": _normalize(
                "Nao ser uma empresa em dificuldade, de acordo com a definicao prevista no "
                "ponto 18 do artigo 2.o do Regulamento (UE) n.o 651/2014"
            ),
            "beneficiary_text": _normalize(
                "Entidades gestoras de RU com competencia para realizar os investimentos em alta"
            ),
        }
        # O aviso não declara nenhum tipo reconhecível → tolerante (não dá para provar
        # inelegibilidade), mas NÃO por ter "ganho" o tipo empresa do boilerplate.
        self.assertEqual(grant_allowed_entity_types(grant["beneficiary_text"]), set())
        self.assertEqual(grant_allowed_entity_types(grant["eligibility_text"]), {"empresa"})  # o bug antigo
        # E o matcher (pontuação) não dá pontos de tipo a uma consultora por causa da cláusula.
        consultora = {"cae_codes": ["70220"], "region": "Norte", "entity_type": "empresa"}
        self.assertFalse(match_entity_type(consultora, grant))

    def test_unknown_dimension_still_eligible(self):
        client = {"cae_codes": ["62010"], "region": "Norte", "dimension": None}
        self.assertTrue(is_eligible(client, self.GRANT)[0])

    def test_unrestricted_grant_accepts_anyone(self):
        grant = {"included_caes": [], "excluded_caes": [],
                 "eligible_regions": [], "eligibility_text": ""}
        client = {"cae_codes": ["99999"], "region": "Madeira", "dimension": "grande"}
        self.assertTrue(is_eligible(client, grant)[0])


class CaeExceptionTests(SimpleTestCase):
    """'Divisão 91 com exceção do Grupo 911' — o padrão MAIS ESPECÍFICO ganha."""

    def _m(self, cae, inc, exc):
        return match_cae({"cae_codes": [cae]}, {"included_caes": inc, "excluded_caes": exc})

    def test_included_division_with_excluded_subgroup(self):
        # 91 elegível EXCETO 911: '91100' (em 911) fora; '91200' dentro.
        self.assertFalse(self._m("91100", ["91***"], ["911**"]))
        self.assertTrue(self._m("91200", ["91***"], ["911**"]))

    def test_included_65_with_excluded_651(self):
        # Divisão 65 elegível, EXCETO o grupo 651. Uma empresa 65214 é elegível; 65124 (em 651)
        # não é, porque a exclusão mais específica (651**) ganha à inclusão (65***).
        self.assertTrue(self._m("65214", ["65***"], ["651**"]))
        self.assertFalse(self._m("65124", ["65***"], ["651**"]))
        self.assertFalse(self._m("65100", ["65***"], ["651**"]))  # também em 651
        self.assertTrue(self._m("65200", ["65***"], ["651**"]))   # 65 mas fora de 651

    def test_excluded_division_with_included_exception(self):
        # 91 excluído EXCETO 911: '91100' (em 911) elegível; '91200' fora.
        self.assertTrue(self._m("91100", ["911**"], ["91***"]))
        self.assertFalse(self._m("91200", ["911**"], ["91***"]))

    def test_plain_included_and_excluded_still_work(self):
        self.assertTrue(self._m("62010", ["62***"], []))
        self.assertFalse(self._m("10120", ["62***"], []))
        self.assertFalse(self._m("55100", [], ["55***"]))
        self.assertTrue(self._m("62010", [], ["55***"]))

    def test_no_restriction_is_eligible(self):
        self.assertTrue(self._m("99999", [], []))


class MissingRequiredFieldsTests(SimpleTestCase):
    """CAE and location are required; dimension is not (it is tolerant)."""

    def test_complete_client_has_nothing_missing(self):
        client = {"cae_codes": ["62010"], "region": "Norte"}
        self.assertEqual(missing_required_fields(client), [])

    def test_missing_cae_is_reported(self):
        fields = missing_required_fields({"cae_codes": [], "region": "Norte"})
        self.assertEqual([f["field"] for f in fields], ["cae"])

    def test_missing_location_is_reported(self):
        fields = missing_required_fields({"cae_codes": ["62010"]})
        self.assertEqual([f["field"] for f in fields], ["region"])

    def test_missing_both_are_reported(self):
        fields = missing_required_fields({})
        self.assertEqual({f["field"] for f in fields}, {"cae", "region"})

    def test_city_or_county_counts_as_location(self):
        self.assertEqual(missing_required_fields({"cae_codes": ["62010"], "city": "Porto"}), [])
        self.assertEqual(missing_required_fields({"cae_codes": ["62010"], "county": "Braga"}), [])

    def test_dimension_absence_is_never_required(self):
        # No dimension, but CAE + region present -> nothing missing.
        self.assertEqual(missing_required_fields({"cae_codes": ["62010"], "region": "Sul"}), [])


class ApplyOverridesTests(SimpleTestCase):
    """User-supplied data fills ONLY missing fields; it never overrides nif.pt data."""

    def _meta(self, **over):
        base = {"cae_codes": [], "main_cae": None, "secondary_cae": [],
                "region": "", "dimension": None}
        base.update(over)
        return base

    def test_cae_from_comma_string(self):
        md = NifMatchingService._apply_overrides(self._meta(), {"cae": "62010, 47110"})
        self.assertEqual(md["cae_codes"], ["62010", "47110"])
        self.assertEqual(md["main_cae"], "62010")
        self.assertEqual(md["secondary_cae"], ["47110"])

    def test_cae_from_list(self):
        md = NifMatchingService._apply_overrides(self._meta(), {"cae_codes": ["62010"]})
        self.assertEqual(md["cae_codes"], ["62010"])

    def test_region_and_dimension_fill_when_missing(self):
        md = NifMatchingService._apply_overrides(
            self._meta(), {"region": "Algarve", "dimension": "Micro"})
        self.assertEqual(md["region"], "Algarve")
        self.assertEqual(md["dimension"], "micro")  # normalized to lowercase

    def test_does_not_override_existing_data(self):
        md = NifMatchingService._apply_overrides(
            self._meta(cae_codes=["11111"], region="Norte"),
            {"cae": "99999", "region": "Sul"})
        self.assertEqual(md["cae_codes"], ["11111"])  # nif.pt CAE kept
        self.assertEqual(md["region"], "Norte")       # nif.pt region kept

    def test_none_overrides_are_safe(self):
        md = NifMatchingService._apply_overrides(self._meta(), None)
        self.assertEqual(md["cae_codes"], [])


class NifCompanyProfileConcordanceTests(SimpleTestCase):
    """The SQLite enrichment must map cleanly onto UserProfile so a user can be
    created from it at any time."""

    def test_to_profile_fields_maps_onto_userprofile(self):
        company = NifCompany(
            nif="500829993", name="Empresa X", locality="Porto",
            municipality="Matosinhos", district="Porto", region="Norte",
            postal_code="4464-503", employees=27088, dimension="grande",
        )
        fields = company.to_profile_fields()
        self.assertEqual(fields, {
            "nif": "500829993",
            "region": "Norte",
            "city": "Porto",          # locality -> city
            "county": "Matosinhos",   # municipality -> county
            "postal_code": "4464-503",
            "entity_size": "grande",  # dimension -> entity_size
        })

    def test_every_mapped_key_is_a_real_userprofile_field(self):
        company = NifCompany(nif="1", region="Norte")
        profile_field_names = {f.name for f in UserProfile._meta.get_fields()}
        for key in company.to_profile_fields():
            self.assertIn(key, profile_field_names,
                          f"{key} is not a UserProfile field")

    def test_dimension_choices_match_entity_size_choices(self):
        # Both models must agree on the size vocabulary for the mapping to be valid.
        self.assertEqual(
            [v for v, _ in NifCompany.DIMENSION_CHOICES],
            [v for v, _ in UserProfile.EntitySize],
        )

    def test_to_profile_fields_can_build_a_profile_instance(self):
        # Assigning the mapped fields to a UserProfile must not raise (types/lengths fit).
        company = NifCompany(nif="123456789", region="Centro", locality="Aveiro",
                             municipality="Aveiro", postal_code="3800-000",
                             dimension="micro")
        profile = UserProfile(**company.to_profile_fields())
        self.assertEqual(profile.entity_size, "micro")
        self.assertEqual(profile.county, "Aveiro")


class NifKeyRotationTests(SimpleTestCase):
    """A rotação usa NIF_KEY, NIF_KEY1..N uma de cada vez (round-robin). Testes independentes
    do índice global (que outros testes podem ter avançado): comparam por conjunto e ciclo."""

    @override_settings(NIF_KEYS=["A", "B", "C"])
    def test_rotates_through_all_keys_then_wraps(self):
        seq = [_next_nif_key() for _ in range(3)]
        self.assertEqual(set(seq), {"A", "B", "C"})   # cada uma exatamente uma vez em 3 chamadas
        self.assertEqual(_next_nif_key(), seq[0])     # a 4ª repete a 1ª (ciclo)

    @override_settings(NIF_KEYS=["ONLY"])
    def test_single_key_always_returned(self):
        self.assertEqual(_next_nif_key(), "ONLY")
        self.assertEqual(_next_nif_key(), "ONLY")

    @override_settings(NIF_KEYS=[], NIF_KEY="FALLBACK")
    def test_fallback_to_single_nif_key(self):
        self.assertEqual(_next_nif_key(), "FALLBACK")

    @override_settings(NIF_KEYS=[], NIF_KEY=None)
    def test_no_keys_returns_none(self):
        self.assertIsNone(_next_nif_key())

    @override_settings(NIF_KEYS=["K1", "K2"])
    def test_service_picks_a_configured_key(self):
        self.assertIn(NifMatchingService().api_key, {"K1", "K2"})

    def test_explicit_key_bypasses_rotation(self):
        self.assertEqual(NifMatchingService(api_key="EXPLICIT").api_key, "EXPLICIT")


class GeneralEmbeddingTextTests(SimpleTestCase):
    """Texto do embedding GENERAL: o que o aviso financia, para quem e onde (sem burocracia
    e SEM os setores — esses têm o seu próprio embedding)."""

    def test_includes_content_and_final_recipients(self):
        grant = Grant(
            title="Infraestruturas de valorização de Resíduos Urbanos",
            objective="Promover uma gestão eficiente dos resíduos.",
            specific_objective="RSO2.6 - Economia Circular",
            operation_typology="Subinvestimentos em alta",
            covered_actions="Construção de estações de triagem",
            final_recipients=["Entidades gestoras de RU com competência para investimentos em alta"],
            eligible_regions=["NUTS II Norte", "NUTS II Centro"],
        )
        text = build_general_embedding_text(grant)
        for expected in ("Infraestruturas de valorização", "gestão eficiente dos resíduos",
                         "Economia Circular", "Subinvestimentos em alta",
                         "estações de triagem", "Entidades gestoras de RU", "NUTS II Norte"):
            self.assertIn(expected, text)

    def test_excludes_sectors(self):
        # Os setores pertencem ao embedding SECTOR — se entrassem aqui, diluíam-se no geral.
        grant = Grant(title="Aviso X", objective="Objetivo Y",
                      target_technology_sectors=["Economia circular", "Compostagem"])
        text = build_general_embedding_text(grant)
        self.assertNotIn("Compostagem", text)

    def test_empty_fields_do_not_break(self):
        grant = Grant(title="Aviso X", objective="Objetivo Y")
        text = build_general_embedding_text(grant)
        self.assertIn("Aviso X", text)
        self.assertIn("Objetivo Y", text)


class SectorEmbeddingTextTests(SimpleTestCase):
    """Texto do embedding SECTOR: só o domínio tecnológico/económico."""

    def test_uses_only_target_technology_sectors(self):
        grant = Grant(
            title="Infraestruturas de valorização de Resíduos Urbanos",
            objective="Um objetivo qualquer que não deve entrar",
            target_technology_sectors=["Valorização de resíduos urbanos", "Compostagem"],
        )
        text = build_sector_embedding_text(grant)
        self.assertIn("Valorização de resíduos urbanos", text)
        self.assertIn("Compostagem", text)
        self.assertNotIn("objetivo qualquer", text)   # o texto geral não contamina o setorial

    def test_falls_back_to_title_when_no_sectors(self):
        grant = Grant(title="Apoio à Inovação Produtiva", objective="Objetivo")
        self.assertEqual(build_sector_embedding_text(grant), "Apoio à Inovação Produtiva")

    def test_empty_sectors_list_falls_back(self):
        grant = Grant(title="Aviso Y", target_technology_sectors=[])
        self.assertEqual(build_sector_embedding_text(grant), "Aviso Y")


class RelevanceTests(SimpleTestCase):
    """Score combinado: 0.60 setorial + 0.40 geral, com renormalização quando falta uma."""

    SECTOR = GrantEmbedding.Type.SECTOR
    GENERAL = GrantEmbedding.Type.GENERAL

    def test_weighted_combination(self):
        # Vetores idênticos → cosseno 1.0; ortogonais → 0.0.
        same, orthogonal = [1.0, 0.0], [0.0, 1.0]
        # setor igual (1.0), geral ortogonal (0.0) → 0.60*1 + 0.40*0 = 0.60
        final, sector, general = grant_embeddings.relevance(
            {self.SECTOR: same, self.GENERAL: same},
            {self.SECTOR: same, self.GENERAL: orthogonal},
        )
        self.assertAlmostEqual(sector, 1.0)
        self.assertAlmostEqual(general, 0.0)
        self.assertAlmostEqual(final, 0.60)

    def test_weights_reflect_constants(self):
        self.assertAlmostEqual(
            grant_embeddings.SECTOR_WEIGHT + grant_embeddings.GENERAL_WEIGHT, 1.0)

    def test_renormalizes_when_sector_missing(self):
        # Só há geral (1.0): sem renormalizar dava 0.40 e o aviso caía injustamente.
        same = [1.0, 0.0]
        final, sector, general = grant_embeddings.relevance(
            {self.GENERAL: same}, {self.GENERAL: same},
        )
        self.assertIsNone(sector)
        self.assertAlmostEqual(general, 1.0)
        self.assertAlmostEqual(final, 1.0)

    def test_renormalizes_when_general_missing(self):
        same = [1.0, 0.0]
        final, sector, general = grant_embeddings.relevance(
            {self.SECTOR: same}, {self.SECTOR: same},
        )
        self.assertAlmostEqual(sector, 1.0)
        self.assertIsNone(general)
        self.assertAlmostEqual(final, 1.0)

    def test_none_when_nothing_comparable(self):
        # Aviso ainda sem embeddings → relevância None → ranking cai para taxa+dotação.
        final, sector, general = grant_embeddings.relevance({self.SECTOR: [1.0, 0.0]}, {})
        self.assertIsNone(final)
        self.assertIsNone(sector)
        self.assertIsNone(general)

    def test_empty_company_vectors(self):
        self.assertEqual(grant_embeddings.relevance({}, {self.SECTOR: [1.0, 0.0]}),
                         (None, None, None))


class DisposableEmailTests(SimpleTestCase):
    def test_known_disposable_domain(self):
        self.assertTrue(is_disposable_email("qualquer.coisa@mailinator.com"))

    def test_known_disposable_domain_is_case_insensitive(self):
        self.assertTrue(is_disposable_email("Ana@MAILINATOR.COM"))

    def test_generic_free_webmail_is_also_rejected(self):
        # Intencional: o gate quer um email de EMPRESA, não pessoal — gmail/outlook/hotmail
        # ficam no mesmo ficheiro que os descartáveis (ver disposable_email_domains.json).
        for domain in ("gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"):
            self.assertTrue(is_disposable_email(f"ana@{domain}"), domain)

    def test_real_company_domain_is_not_disposable(self):
        self.assertFalse(is_disposable_email("ana@aliados.consulting"))

    def test_no_at_sign(self):
        self.assertFalse(is_disposable_email("nao-e-um-email"))

    def test_empty(self):
        self.assertFalse(is_disposable_email(""))
        self.assertFalse(is_disposable_email(None))


class ViewerCreationTests(TestCase):
    """O viewer (lead) só nasce de um match NÃO autenticado. Um utilizador autenticado
    (admin, composer…) está a consultar — não polui a BD com viewers."""

    NIF_RECORD = {
        "nif": "500829993", "title": "Consultora XPTO", "cae": ["70220"],
        "status": "active", "address": "Rua X", "city": "Porto",
        "activity": "Consultadoria de gestão",
        "geo": {"region": "Norte", "county": "Porto"},
        "structure": {"nature": "LDA"}, "contacts": {},
    }

    CONTACT = {"email": "ana@xpto.pt", "name": "Ana Silva", "job_title": "Sócia-Gerente"}

    def setUp(self):
        # fetch_company (nif.pt) e os embeddings (OpenAI) mockados — testes offline.
        self.fetch = mock.patch.object(
            NifMatchingService, "fetch_company", return_value=self.NIF_RECORD)
        self.vectors = mock.patch.object(
            NifMatchingService, "_company_vectors", return_value={})
        cache.clear()  # isola o cache do gate de contacto entre testes (mesmo NIF em vários)

    def test_anonymous_match_creates_viewer(self):
        with self.fetch, self.vectors:
            result = NifMatchingService().evaluate(
                "500829993", create_viewer=True, contact=self.CONTACT)
        self.assertIsNotNone(result["viewer_user_id"])
        profile = UserProfile.objects.filter(nif="500829993").first()
        self.assertIsNotNone(profile)
        self.assertEqual(profile.role, UserProfile.VIEWER)

    def test_authenticated_match_does_not_create_viewer(self):
        with self.fetch, self.vectors:
            result = NifMatchingService().evaluate("500829993", create_viewer=False)
        self.assertIsNone(result["viewer_user_id"])
        self.assertFalse(UserProfile.objects.filter(nif="500829993").exists())
        self.assertFalse(User.objects.filter(username="500829993").exists())

    def test_anonymous_match_saves_only_minimal_fields(self):
        # Sem login: só dados DIRETOS do nif.pt (NIF, CAE, natureza, atividade, morada) +
        # NUTS (resolvida) + dimensão, MAIS o contacto (email/nome/função) do pop-up. NADA de
        # tipo de entidade (é INFERIDO por nós, não um campo do nif.pt), capital social,
        # faturação/nº empregados, ou telefone/site/fax.
        with self.fetch, self.vectors:
            NifMatchingService().evaluate(
                "500829993", create_viewer=True, contact=self.CONTACT)
        profile = UserProfile.objects.get(nif="500829993")

        # Guardados — campos DIRETOS do nif.pt.
        self.assertEqual(profile.activity, "Consultadoria de gestão")
        self.assertEqual(profile.address, "Rua X")
        self.assertEqual(profile.city, "Porto")
        self.assertEqual(profile.main_cae, "70220")
        self.assertEqual(profile.nature, "LDA")
        # NUTS: a região resolvida (nuts.json a partir da cidade/concelho), não o texto bruto
        # do nif.pt ("Norte" vindo de geo.region coincide aqui, mas a fonte é a NUTS resolvida).
        self.assertEqual(profile.region, "Norte")
        # Contacto — vem do pop-up, não do nif.pt, mas é guardado.
        self.assertEqual(profile.job_title, "Sócia-Gerente")

        # NÃO guardados — entity_type é INFERIDO (não um campo do nif.pt); capital/telefone/
        # site/fax ficam fora por decisão de minimização, mesmo vindo do nif.pt.
        self.assertIsNone(profile.entity_type)
        self.assertIsNone(profile.capital)
        self.assertIsNone(profile.phone)
        self.assertIsNone(profile.website)
        self.assertIsNone(profile.fax)
        user = User.objects.get(username="500829993")
        self.assertEqual(user.email, "ana@xpto.pt")
        self.assertEqual(user.first_name, "Ana Silva")

    def test_authenticated_match_does_not_touch_existing_profile(self):
        # Um perfil já existente não pode ser alterado por um match de um admin.
        user = User.objects.create_user("cliente_x", password="Xk93!vTq21mZ")
        user.profile.nif = "500829993"
        user.profile.role = UserProfile.CLIENT
        user.profile.address = "Morada original"
        user.profile.save()
        with self.fetch, self.vectors:
            NifMatchingService().evaluate("500829993", create_viewer=False)
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.role, UserProfile.CLIENT)
        self.assertEqual(user.profile.address, "Morada original")  # intacto

    # --- Gate de contacto (email/nome/função) — só quem não tem sessão ---------------

    def test_anonymous_match_without_contact_is_gated(self):
        # A procura corre sempre (o mock de fetch_company é chamado), mas sem contacto os
        # matches ficam retidos e o lead (dados da empresa) já fica gravado mesmo assim.
        with self.fetch, self.vectors:
            with self.assertRaises(MissingClientDataError) as ctx:
                NifMatchingService().evaluate("500829993", create_viewer=True)
        fields = {f["field"] for f in ctx.exception.fields}
        self.assertEqual(fields, {"email", "name", "job_title"})
        profile = UserProfile.objects.get(nif="500829993")  # o lead foi criado na mesma
        self.assertEqual(profile.role, UserProfile.VIEWER)
        self.assertIsNone(profile.job_title)
        self.assertEqual(profile.user.email, "")

    def test_partial_contact_is_still_gated(self):
        with self.fetch, self.vectors:
            with self.assertRaises(MissingClientDataError) as ctx:
                NifMatchingService().evaluate(
                    "500829993", create_viewer=True,
                    contact={"email": "ana@xpto.pt"})  # falta nome e função
        fields = {f["field"] for f in ctx.exception.fields}
        self.assertEqual(fields, {"name", "job_title"})

    def test_disposable_email_domain_is_gated_as_invalid(self):
        # Domínio de email descartável conhecido (ver match/disposable_email_domains.json) —
        # tratado como "em falta", com label distinta para o front-end saber porquê.
        with self.fetch, self.vectors:
            with self.assertRaises(MissingClientDataError) as ctx:
                NifMatchingService().evaluate(
                    "500829993", create_viewer=True,
                    contact={"email": "ana@mailinator.com", "name": "Ana Silva",
                            "job_title": "Sócia-Gerente"})
        fields = {(f["field"], f["label"]) for f in ctx.exception.fields}
        self.assertEqual(fields, {("email", "Email inválido")})

        # O email descartável não pode ficar gravado no perfil nem disparar o email de
        # boas-vindas — é tratado como se não tivesse vindo (ver evaluate/safe_contact).
        user = User.objects.get(username="500829993")
        self.assertEqual(user.email, "")
        self.assertEqual(len(mail.outbox), 0)

    def test_authenticated_match_ignores_missing_contact(self):
        # Um admin/comercial nunca é gated — o gate só existe para captar leads.
        with self.fetch, self.vectors:
            result = NifMatchingService().evaluate("500829993", create_viewer=False)
        self.assertEqual(result["matches"], [])  # não rebenta, devolve normalmente

    def test_contact_reveals_cached_matches_without_recomputing(self):
        with self.fetch as fetch_mock, self.vectors:
            with self.assertRaises(MissingClientDataError):
                NifMatchingService().evaluate("500829993", create_viewer=True)
            self.assertEqual(fetch_mock.call_count, 1)

            # 2ª chamada, agora com contacto — vem do cache, NÃO volta a chamar fetch_company.
            result = NifMatchingService().evaluate(
                "500829993", create_viewer=True, contact=self.CONTACT)
            self.assertEqual(fetch_mock.call_count, 1)  # continua 1 — não recalculou

        self.assertIsNotNone(result["viewer_user_id"])
        profile = UserProfile.objects.get(nif="500829993")
        self.assertEqual(profile.job_title, "Sócia-Gerente")
        self.assertEqual(profile.user.email, "ana@xpto.pt")

        # O cache é consumido — uma 3ª chamada sem contacto volta a ficar gated (não a
        # "reaproveitar" um resultado já entregue).
        with self.fetch as fetch_mock2, self.vectors:
            with self.assertRaises(MissingClientDataError):
                NifMatchingService().evaluate("500829993", create_viewer=True)
            self.assertEqual(fetch_mock2.call_count, 1)  # recalculou de raiz

    def test_contact_reveal_sends_welcome_email_once(self):
        with self.fetch, self.vectors:
            NifMatchingService().evaluate(
                "500829993", create_viewer=True, contact=self.CONTACT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@xpto.pt"])
        self.assertIn("Bem-vindo", mail.outbox[0].subject)
        self.assertTrue(mail.outbox[0].alternatives)  # versão HTML anexada

        # Um 2º match do MESMO NIF (email já em ficha) não reenvia o email de boas-vindas.
        with self.fetch, self.vectors:
            NifMatchingService().evaluate(
                "500829993", create_viewer=True, contact=self.CONTACT)
        self.assertEqual(len(mail.outbox), 1)  # continua 1 — não duplicou

    def test_view_passes_create_viewer_false_when_authenticated(self):
        admin = User.objects.create_user("admin_m", password="Xk93!vTq21mZ")
        admin.profile.role = UserProfile.ADMIN
        admin.profile.save()
        self.client.force_login(admin)
        with mock.patch.object(NifMatchingService, "evaluate",
                               return_value={"company": {}, "nif": "1", "viewer_user_id": None,
                                             "matches": []}) as ev:
            self.client.post("/match/evaluate-nif/",
                             data=json.dumps({"nif": "500829993"}),
                             content_type="application/json")
        self.assertFalse(ev.call_args.kwargs["create_viewer"])

    def test_view_passes_create_viewer_true_when_anonymous(self):
        with mock.patch.object(NifMatchingService, "evaluate",
                               return_value={"company": {}, "nif": "1", "viewer_user_id": 1,
                                             "matches": []}) as ev:
            self.client.post("/match/evaluate-nif/",
                             data=json.dumps({"nif": "500829993"}),
                             content_type="application/json")
        self.assertTrue(ev.call_args.kwargs["create_viewer"])

    def test_view_passes_contact_fields_to_evaluate(self):
        with mock.patch.object(NifMatchingService, "evaluate",
                               return_value={"company": {}, "nif": "1", "viewer_user_id": 1,
                                             "matches": []}) as ev:
            self.client.post(
                "/match/evaluate-nif/",
                data=json.dumps({
                    "nif": "500829993", "email": "ana@xpto.pt",
                    "name": "Ana Silva", "job_title": "Sócia-Gerente",
                }),
                content_type="application/json",
            )
        self.assertEqual(ev.call_args.kwargs["contact"], {
            "email": "ana@xpto.pt", "name": "Ana Silva", "job_title": "Sócia-Gerente",
        })

    def test_view_returns_422_with_missing_contact_fields(self):
        with mock.patch.object(
            NifMatchingService, "evaluate",
            side_effect=MissingClientDataError(
                [{"field": "email", "label": "Email"}, {"field": "name", "label": "Nome"},
                 {"field": "job_title", "label": "Função"}]),
        ):
            resp = self.client.post(
                "/match/evaluate-nif/",
                data=json.dumps({"nif": "500829993"}), content_type="application/json",
            )
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertTrue(body["needs_more_info"])
        self.assertEqual(
            {f["field"] for f in body["missing_fields"]}, {"email", "name", "job_title"})


class CaePrefilterTests(TestCase):
    """Prefiltro CAE em SQL (via tabela GrantCae): estreita o conjunto no Postgres mantendo a
    semântica EXATA — é impossível ser CAE-elegível fora do conjunto devolvido."""

    def _grant(self, code, **kw):
        from avisos.models import Grant
        defaults = dict(source="portugal", scraping_url=f"https://x/{code}/", grant_code=code,
                        ai_processed=True, active=True)
        defaults.update(kw)
        return Grant.objects.create(**defaults)

    def setUp(self):
        self.restrito = self._grant("RESTRITO", included_caes=["62***"])   # só TI
        self.aberto = self._grant("ABERTO")                               # sem CAE
        self.so_excluido = self._grant("SO_EXCLUIDO", excluded_caes=["55***"])

    def test_prefilter_narrows_to_candidates(self):
        svc = NifMatchingService()
        # Cliente TI (62010): candidato ao RESTRITO (bate 62), ao ABERTO e ao SO_EXCLUIDO.
        ti = {"cae_codes": ["62010"]}
        codes = {g.grant_code for g in svc._active_opportunities(ti)}
        self.assertEqual(codes, {"RESTRITO", "ABERTO", "SO_EXCLUIDO"})

        # Cliente fora do 62 (10120): o RESTRITO é ELIMINADO no SQL (não pode ser elegível).
        outro = {"cae_codes": ["10120"]}
        codes = {g.grant_code for g in svc._active_opportunities(outro)}
        self.assertEqual(codes, {"ABERTO", "SO_EXCLUIDO"})   # RESTRITO fora

    def test_prefilter_matches_python_exactly(self):
        # Para qualquer cliente, {avisos elegíveis com prefiltro} == {sem prefiltro}.
        from avisos.models import Grant
        from match.scoring_rules import is_eligible
        svc = NifMatchingService()
        for cae in ("62010", "10120", "55100", "621**".replace("*", "0")):
            client = {"cae_codes": [cae]}
            todos = Grant.objects.filter(ai_processed=True, active=True)
            sem = {g.grant_code for g in todos
                   if is_eligible(client, svc._grant_to_opportunity(g))[0]}
            com = {g.grant_code for g in svc._active_opportunities(client)
                   if is_eligible(client, svc._grant_to_opportunity(g))[0]}
            self.assertEqual(sem, com, f"divergência para CAE {cae}")

    def test_no_client_cae_returns_all(self):
        svc = NifMatchingService()
        codes = {g.grant_code for g in svc._active_opportunities({})}
        self.assertEqual(codes, {"RESTRITO", "ABERTO", "SO_EXCLUIDO"})


class LlmValidationTests(TestCase):
    """Camada final de validação por LLM (OpenRouter). O HTTP é sempre MOCKADO — os testes
    NUNCA fazem chamadas reais ao OpenRouter."""

    from match import llm_validation as _llm  # atalho

    def _grant(self, code, **kw):
        from avisos.models import Grant
        defaults = dict(source="portugal", scraping_url=f"https://x/{code}/", grant_code=code,
                        ai_processed=True, active=True)
        defaults.update(kw)
        return Grant.objects.create(**defaults)

    def _fake_response(self, content):
        m = mock.Mock()
        m.raise_for_status.return_value = None
        m.status_code = 200
        m.json.return_value = {"choices": [{"message": {"content": content}}]}
        m.text = json.dumps(m.json.return_value)
        return m

    def _fake_error_body(self, body: dict, status: int = 200):
        """Resposta HTTP `status` cujo corpo é um objeto SEM `choices` (ex: erro do OpenRouter/
        modelo esgotado devolvido com 200) — o caso real do free tier da Nvidia."""
        m = mock.Mock()
        m.raise_for_status.return_value = None
        m.status_code = status
        m.json.return_value = body
        m.text = json.dumps(body)
        return m

    def test_no_key_returns_empty(self):
        from match import llm_validation
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}):
            self.assertEqual(
                llm_validation.validate_matches({"nif": "1"}, [self._grant("A")]), {})

    def test_empty_grants_returns_empty(self):
        from match import llm_validation
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}):
            self.assertEqual(llm_validation.validate_matches({"nif": "1"}, []), {})

    def test_parses_verdicts(self):
        from match import llm_validation
        g = self._grant("A")
        content = json.dumps([
            {"id": g.id, "adequate": False, "reason": "É para gestoras de resíduos."},
        ])
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}), \
             mock.patch("match.llm_validation.requests.post", return_value=self._fake_response(content)):
            verdicts = llm_validation.validate_matches({"nif": "1", "activity": "consultoria"}, [g])
        self.assertEqual(verdicts[g.id]["adequate"], False)
        self.assertIn("resíduos", verdicts[g.id]["reason"])

    def test_parses_json_wrapped_in_text(self):
        from match import llm_validation
        g = self._grant("B")
        content = "Aqui está:\n```json\n[{\"id\": %d, \"adequate\": true, \"reason\": \"ok\"}]\n```" % g.id
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}), \
             mock.patch("match.llm_validation.requests.post", return_value=self._fake_response(content)):
            verdicts = llm_validation.validate_matches({"nif": "1"}, [g])
        self.assertTrue(verdicts[g.id]["adequate"])

    def test_http_error_returns_empty(self):
        from match import llm_validation
        import requests
        g = self._grant("C")
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}), \
             mock.patch("match.llm_validation.requests.post",
                        side_effect=requests.RequestException("boom")):
            self.assertEqual(llm_validation.validate_matches({"nif": "1"}, [g]), {})

    def test_illegible_response_returns_empty(self):
        from match import llm_validation
        g = self._grant("D")
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}), \
             mock.patch("match.llm_validation.requests.post",
                        return_value=self._fake_response("desculpa, não percebi")):
            self.assertEqual(llm_validation.validate_matches({"nif": "1"}, [g]), {})

    def test_error_body_with_200_returns_empty(self):
        # Caso REAL: o OpenRouter devolve HTTP 200 mas o corpo é um erro (modelo grátis esgotado),
        # sem `choices`. Não pode rebentar nem filtrar — degrada para {} (nada filtrado).
        from match import llm_validation
        g = self._grant("E")
        body = {"error": {"message": "Upstream error from Nvidia: ResourceExhausted", "code": 502}}
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "k"}), \
             mock.patch("match.llm_validation.requests.post",
                        return_value=self._fake_error_body(body, status=200)):
            self.assertEqual(llm_validation.validate_matches({"nif": "1"}, [g]), {})

    def test_apply_llm_validation_filters_non_adequate(self):
        # A camada de integração: remove os não-adequados, mantém os adequados e os desconhecidos.
        results = [
            {"opportunity_id": 1, "grant_code": "OK"},
            {"opportunity_id": 2, "grant_code": "FORA"},
            {"opportunity_id": 3, "grant_code": "DESCONHECIDO"},
        ]
        grant_by_id = {1: object(), 2: object(), 3: object()}
        verdicts = {
            1: {"adequate": True, "reason": "adequado"},
            2: {"adequate": False, "reason": "fora do setor"},
            # 3 ausente → desconhecido → mantém-se
        }
        with mock.patch("match.services.llm_validation.validate_matches", return_value=verdicts):
            final = NifMatchingService._apply_llm_validation({"nif": "1"}, results, grant_by_id)
        codes = [r["grant_code"] for r in final]
        self.assertEqual(codes, ["OK", "DESCONHECIDO"])   # FORA removido
        self.assertTrue(final[0]["llm_adequate"])
        self.assertEqual(final[0]["llm_reason"], "adequado")
        self.assertIsNone(final[1]["llm_adequate"])       # desconhecido

    def test_apply_llm_validation_keeps_all_when_no_verdicts(self):
        # Sem chave/em falha (verdicts vazios) → nada filtrado.
        results = [{"opportunity_id": 1, "grant_code": "A"}, {"opportunity_id": 2, "grant_code": "B"}]
        with mock.patch("match.services.llm_validation.validate_matches", return_value={}):
            final = NifMatchingService._apply_llm_validation({}, results, {1: object(), 2: object()})
        self.assertEqual(len(final), 2)
        self.assertIsNone(final[0]["llm_adequate"])

    def test_apply_llm_validation_caps_grants_sent_to_llm(self):
        # Só os LLM_VALIDATION_CAP mais relevantes (results já vem ordenado) vão ao LLM; os
        # restantes passam sem validação e ficam no fundo com llm_adequate=None.
        cap = NifMatchingService.LLM_VALIDATION_CAP
        n = cap + 5
        results = [{"opportunity_id": i, "grant_code": f"G{i}"} for i in range(n)]
        grant_by_id = {i: object() for i in range(n)}
        with mock.patch("match.services.llm_validation.validate_matches",
                        return_value={}) as validate:
            final = NifMatchingService._apply_llm_validation({}, results, grant_by_id)
        # Foi chamado exatamente com os `cap` primeiros avisos (os mais relevantes).
        sent = validate.call_args.args[1]
        self.assertEqual(len(sent), cap)
        self.assertEqual(sent, [grant_by_id[i] for i in range(cap)])
        # Verdicts vazios → nada removido; os além do cap ficam com llm_adequate=None.
        self.assertEqual(len(final), n)
        self.assertIsNone(final[-1]["llm_adequate"])

    def test_apply_llm_validation_does_not_filter_beyond_cap(self):
        # Um aviso FORA do top-N nunca é removido, mesmo que o LLM (que não o viu) devolvesse
        # um veredito negativo para outro id — o cap protege-o.
        cap = NifMatchingService.LLM_VALIDATION_CAP
        n = cap + 3
        results = [{"opportunity_id": i, "grant_code": f"G{i}"} for i in range(n)]
        grant_by_id = {i: object() for i in range(n)}
        # LLM remove o primeiro (dentro do cap); os de fora do cap não têm veredito.
        verdicts = {0: {"adequate": False, "reason": "fora"}}
        with mock.patch("match.services.llm_validation.validate_matches", return_value=verdicts):
            final = NifMatchingService._apply_llm_validation({}, results, grant_by_id)
        codes = {r["grant_code"] for r in final}
        self.assertNotIn("G0", codes)                 # removido (dentro do cap)
        self.assertIn(f"G{n - 1}", codes)             # fora do cap → intocado
        self.assertEqual(len(final), n - 1)


class SaveGrantEmbeddingsTests(TestCase):
    """Persistência: um registo por tipo, e NENHUMA chamada à OpenAI quando o texto não mudou.
    A API é mockada — os testes não tocam na OpenAI."""

    def setUp(self):
        self.grant = Grant.objects.create(
            source="portugal", scraping_url="https://x/emb/", grant_code="EMB-1",
            ai_processed=True, title="Aviso de resíduos",
            objective="Gestão de resíduos urbanos",
            target_technology_sectors=["Economia circular"],
        )
        # embed_many devolve um vetor por texto pedido (dimensão real do modelo).
        self.fake = mock.patch(
            "match.grant_embeddings.emb.embed_many",
            side_effect=lambda texts: [[0.1] * 1536 for _ in texts],
        )

    def test_creates_one_row_per_type(self):
        with self.fake as m:
            saved = grant_embeddings.save_grant_embeddings(self.grant)
        self.assertEqual(set(saved), {GrantEmbedding.Type.GENERAL, GrantEmbedding.Type.SECTOR})
        rows = GrantEmbedding.objects.filter(grant=self.grant)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(m.call_count, 1)  # UMA chamada para os dois tipos (embed_many)
        for row in rows:
            self.assertEqual(row.model, "text-embedding-3-small")
            self.assertTrue(row.text_hash)

    def test_second_call_without_changes_does_not_hit_openai(self):
        with self.fake:
            grant_embeddings.save_grant_embeddings(self.grant)
        self.grant.refresh_from_db()
        with self.fake as m:
            saved = grant_embeddings.save_grant_embeddings(self.grant)
        self.assertEqual(saved, {})       # nada recalculado
        m.assert_not_called()             # e ZERO chamadas à API
        self.assertEqual(GrantEmbedding.objects.filter(grant=self.grant).count(), 2)

    def test_only_the_affected_type_is_recalculated(self):
        with self.fake:
            grant_embeddings.save_grant_embeddings(self.grant)
        # Muda só os setores → só o SECTOR precisa de recálculo; o GENERAL fica intacto.
        self.grant.target_technology_sectors = ["Compostagem", "Biogás"]
        self.grant.save(update_fields=["target_technology_sectors"])
        with self.fake as m:
            saved = grant_embeddings.save_grant_embeddings(self.grant)
        self.assertEqual(set(saved), {GrantEmbedding.Type.SECTOR})
        self.assertEqual(m.call_args[0][0], ["Compostagem\nBiogás"])  # só este texto foi pedido

    def test_force_recalculates_everything(self):
        with self.fake:
            grant_embeddings.save_grant_embeddings(self.grant)
        with self.fake:
            saved = grant_embeddings.save_grant_embeddings(self.grant, force=True)
        self.assertEqual(set(saved), {GrantEmbedding.Type.GENERAL, GrantEmbedding.Type.SECTOR})
        self.assertEqual(GrantEmbedding.objects.filter(grant=self.grant).count(), 2)  # update, não duplica

    def test_unique_per_grant_and_type(self):
        with self.fake:
            grant_embeddings.save_grant_embeddings(self.grant)
        # Regravar o mesmo tipo atualiza a linha existente (constraint uniq_grant_embedding_per_type).
        grant_embeddings.store_embedding(self.grant, GrantEmbedding.Type.SECTOR, [0.5] * 1536, "novo")
        rows = GrantEmbedding.objects.filter(grant=self.grant, embedding_type=GrantEmbedding.Type.SECTOR)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().text_hash, "novo")

    def test_no_api_leaves_no_rows(self):
        # Sem OPENAI_API_KEY, embed_many devolve [None, None] → nada é gravado (sem rebentar).
        with mock.patch("match.grant_embeddings.emb.embed_many",
                        side_effect=lambda texts: [None for _ in texts]):
            saved = grant_embeddings.save_grant_embeddings(self.grant)
        self.assertEqual(saved, {})
        self.assertEqual(GrantEmbedding.objects.filter(grant=self.grant).count(), 0)

    def test_grant_vectors_reads_saved_rows(self):
        with self.fake:
            grant_embeddings.save_grant_embeddings(self.grant)
        self.grant.refresh_from_db()
        vectors = grant_embeddings.grant_vectors(self.grant)
        self.assertEqual(set(vectors), {GrantEmbedding.Type.GENERAL, GrantEmbedding.Type.SECTOR})


class CompanyTextTests(SimpleTestCase):
    """Textos da empresa: o setorial isola a atividade; o geral junta todo o perfil."""

    META = {
        "activity": "Recolha e tratamento de resíduos urbanos",
        "name": "Resíduos SA", "entity_type": "empresa",
        "cae_codes": ["38112"], "region": "Norte", "city": "Porto",
    }

    def test_sector_text_is_only_the_activity(self):
        text = _company_sector_text(self.META)
        self.assertEqual(text, "Recolha e tratamento de resíduos urbanos")
        self.assertNotIn("Porto", text)   # localização não pertence ao sinal setorial

    def test_sector_text_falls_back_to_cae_and_name(self):
        text = _company_sector_text({**self.META, "activity": ""})
        self.assertIn("38112", text)
        self.assertIn("Resíduos SA", text)

    def test_general_text_has_full_profile(self):
        text = _company_general_text(self.META)
        for expected in ("Recolha e tratamento", "Resíduos SA", "empresa", "38112", "Porto"):
            self.assertIn(expected, text)


class ActivePhaseTests(SimpleTestCase):
    """The relevant phase (by DB id) drives which budget/rate row applies now."""

    PHASES = [
        {"id": 1, "start_date": "2026-04-30T15:00:00", "end_date": "2026-07-30T18:00:00"},
        {"id": 2, "start_date": "2026-07-30T18:00:00", "end_date": "2026-10-30T18:00:00"},
        {"id": 3, "start_date": "2026-10-30T18:00:00", "end_date": "2027-01-15T18:00:00"},
    ]

    def test_current_phase(self):
        self.assertEqual(active_phase_id(self.PHASES, datetime(2026, 8, 15)), 2)

    def test_before_all_picks_next_to_open(self):
        self.assertEqual(active_phase_id(self.PHASES, datetime(2026, 1, 1)), 1)

    def test_after_all_picks_latest(self):
        self.assertEqual(active_phase_id(self.PHASES, datetime(2027, 6, 1)), 3)

    def test_no_dates_returns_none(self):
        self.assertIsNone(active_phase_id([{"id": 1}]))


class CompanyAreaTests(SimpleTestCase):

    def test_single_area_always_matches(self):
        self.assertEqual(
            company_area_id([], [{"id": 1, "geographic_area": "Qualquer"}]), 1)

    def test_matches_by_location_token(self):
        areas = [{"id": 1, "geographic_area": "Área Metropolitana do Porto (AMP)"},
                 {"id": 2, "geographic_area": "CIM do Ave"}]
        self.assertEqual(company_area_id(["ave"], areas), 2)

    def test_no_match_returns_none(self):
        areas = [{"id": 1, "geographic_area": "Norte"},
                 {"id": 2, "geographic_area": "Centro"}]
        self.assertIsNone(company_area_id(["algarve"], areas))


class EffectiveBudgetRateTests(SimpleTestCase):

    def test_fund_and_global_uses_global_budget_and_fund_rate(self):
        # Dotação Global (100%) dá o maior pote; a taxa é a de comparticipação do fundo (85%).
        # Linhas de fundo/global têm phase_id None (não são por fase).
        pa = [
            {"phase_id": None, "area_id": 1, "fund_name": "FSE+",
             "budget_allocation": 4000000.0, "max_financing_rate": 85.0},
            {"phase_id": None, "area_id": 1, "fund_name": "Dotação Global",
             "budget_allocation": 4705882.34, "max_financing_rate": 100.0},
        ]
        self.assertEqual(effective_budget_rate(pa, None, 1), (4705882.34, 85.0))

    def test_phase_specific_row_is_used_when_phase_matches(self):
        pa = [{"phase_id": 1, "area_id": 1, "budget_allocation": 3000000.0, "max_financing_rate": 60.0},
              {"phase_id": 2, "area_id": 1, "budget_allocation": 5000000.0, "max_financing_rate": 60.0}]
        self.assertEqual(effective_budget_rate(pa, 2, 1), (5000000.0, 60.0))

    def test_area_filter(self):
        pa = [{"phase_id": None, "area_id": 1, "budget_allocation": 1000000.0, "max_financing_rate": 50.0},
              {"phase_id": None, "area_id": 2, "budget_allocation": 9000000.0, "max_financing_rate": 70.0}]
        self.assertEqual(effective_budget_rate(pa, None, 1), (1000000.0, 50.0))

    def test_empty_returns_none(self):
        self.assertEqual(effective_budget_rate([], None, None), (None, None))

    def test_rate_fallback_from_financing_rates(self):
        self.assertEqual(
            max_financing_rate_from_rates([{"max_global_rate": "60,0"}, {"base_rate": "40%"}]), 60.0)
        self.assertIsNone(max_financing_rate_from_rates([]))
