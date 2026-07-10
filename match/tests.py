"""
Tests for the match engine's pure rule layer: EU SME dimension classification and
the hard eligibility filter (region + CAE + dimension). No database is touched —
these exercise scoring_rules directly, so they run fast and offline.
"""

from django.test import SimpleTestCase

from match.scoring_rules import (
    classify_dimension,
    grant_allowed_dimensions,
    eligible_cae,
    eligible_location,
    eligible_dimension,
    is_eligible,
    missing_required_fields,
    match_cae,
)
from datetime import datetime

from match.services import NifMatchingService
from match.models import NifCompany
from match.ranking import (
    active_phase_id, company_area_id, effective_budget_rate,
    max_financing_rate_from_rates,
)
from users.models import UserProfile


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
        # Aviso só para municípios (texto de elegibilidade) → uma empresa não é elegível.
        grant = {"included_caes": [], "excluded_caes": [], "eligible_regions": [],
                 "eligibility_text": "sao beneficiarios os municipios"}
        client = {"cae_codes": ["62010"], "region": "Norte", "entity_type": "empresa"}
        self.assertFalse(is_eligible(client, grant)[0])
        municipio = {"region": "Norte", "entity_type": "municipio"}
        self.assertTrue(is_eligible(municipio, grant)[0])

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
