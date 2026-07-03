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
)
from match.services import NifMatchingService
from match.models import NifCompany
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
                         {"cae", "location", "dimension"})
        self.assertTrue(all(b["eligible"] for b in breakdown))

    def test_unknown_dimension_still_eligible(self):
        client = {"cae_codes": ["62010"], "region": "Norte", "dimension": None}
        self.assertTrue(is_eligible(client, self.GRANT)[0])

    def test_unrestricted_grant_accepts_anyone(self):
        grant = {"included_caes": [], "excluded_caes": [],
                 "eligible_regions": [], "eligibility_text": ""}
        client = {"cae_codes": ["99999"], "region": "Madeira", "dimension": "grande"}
        self.assertTrue(is_eligible(client, grant)[0])


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
