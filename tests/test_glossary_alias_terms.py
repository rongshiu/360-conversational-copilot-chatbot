from __future__ import annotations

import unittest

from app.service.glossary_service import derive_schema_alias_terms


class SchemaAliasDerivationTests(unittest.TestCase):
    def test_boolean_purchase_flag_aliases_are_derived_from_schema_metadata(self) -> None:
        aliases = derive_schema_alias_terms(
            "has_soft_line",
            description="Flag indicating whether the customer purchased from the Soft product line in the period.",
            data_type="boolean",
        )

        self.assertIn("soft line", aliases)
        self.assertIn("softline", aliases)
        self.assertIn("softline buyers", aliases)
        self.assertIn("bought from softline", aliases)

    def test_pure_only_field_aliases_are_derived_from_field_name(self) -> None:
        aliases = derive_schema_alias_terms(
            "is_pure_food_only",
            description="Flag indicating whether the customer purchased only food items in the period.",
            data_type="boolean",
        )

        self.assertIn("pure food only", aliases)
        self.assertIn("food only", aliases)
        self.assertIn("only food", aliases)
        self.assertIn("food only buyers", aliases)


if __name__ == "__main__":
    unittest.main()
