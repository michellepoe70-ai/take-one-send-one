import copy
import unittest

from app.policy import contains_safe_harbor_identifiers, load_policy
from test_generation import SAFE_OUTPUT


class SafeHarborTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy()

    def unsafe(self, value, field="language"):
        funder = copy.deepcopy(SAFE_OUTPUT["funder_view"])
        funder[field] = value
        return contains_safe_harbor_identifiers(funder, self.policy, "DEMO-REFERENCE")

    def test_identifier_categories_are_rejected_in_permitted_field_values(self):
        examples = {
            "names": "María Doe",
            "geographic_subdivision": "Miami",
            "finer_date": "2026-11-07",
            "telephone": "+1 (305) 555-0199",
            "fax": "Fax: 305-555-0198",
            "email": "fictional@example.test",
            "ssn": "123-45-6789",
            "medical_record": "MRN: ABC123",
            "health_plan": "Health plan member: ABC123",
            "account": "Account: ABC123",
            "certificate_license": "License: ABC123",
            "vehicle": "VIN: 1HGCM82633A004352",
            "device": "Device serial: ABC123",
            "url": "https://example.test/client/123",
            "ip_address": "192.0.2.45",
            "biometric": "Fingerprint template: ABC123",
            "full_face_photo": "data:image/png;base64,ZmFrZQ==",
            "unique_code": "2f7f1d90-a988-4f83-a3b7-f8f579af9824",
        }
        for category, value in examples.items():
            with self.subTest(category=category):
                self.assertTrue(self.unsafe(value))

    def test_exact_program_codes_and_reporting_month_exceptions_are_field_specific(self):
        for area in self.policy.schema["properties"]["service_area"]["enum"]:
            with self.subTest(program_site=area):
                self.assertFalse(self.unsafe(area, "service_area"))
        self.assertFalse(self.unsafe("2026-11", "reporting_period"))
        for value in ("33125", "Miami", "123 Main Street", "Little Havana ", "Little Havana; client lives in Miami"):
            with self.subTest(geography=value):
                self.assertTrue(self.unsafe(value, "service_area"))
        for value in ("2026-11-07", "2026-11 extra", "11/07/2026", "November 7, 2026"):
            with self.subTest(date=value):
                self.assertTrue(self.unsafe(value, "reporting_period"))
        self.assertTrue(self.unsafe("2026-11", "language"))
        self.assertTrue(self.unsafe({"photo": "encoded"}))


if __name__ == "__main__":
    unittest.main()
