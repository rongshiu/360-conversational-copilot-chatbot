"""The HOD/EXEC split has to be a database boundary, not a validator rule.

Everything here is checkable without a server: the role a request switches into,
the SQL that builds a persona view, and the grant it carries. The live half --
that PostgreSQL actually refuses the cross-persona SELECT -- is asserted by
revision 20260828_09 itself, which fails the migration rather than leaving a
database in a state these tests would pass against.
"""
from __future__ import annotations

import unittest

from app.core import settings
from app.db.v3_ddl import (
    EXEC,
    HOD,
    MONEY_COLUMNS,
    PERSONA_ROLES,
    PERSONA_SCHEMAS,
    PERSONA_VIEWS,
    SCHEMA_READER,
    persona_view_sql,
)
from app.service.principal_service import PrincipalContext


class PersonaRoleSelectionTests(unittest.TestCase):
    def test_each_role_level_maps_to_its_own_role_and_schema(self) -> None:
        hod = PrincipalContext(principal_id="p", role_level="HOD")
        exec_ = PrincipalContext(principal_id="p", role_level="EXEC")

        self.assertEqual(settings.ci_copilot_hod_role, hod.persona_role)
        self.assertEqual(settings.ci_copilot_exec_role, exec_.persona_role)
        self.assertNotEqual(hod.persona_role, exec_.persona_role)

        # The role and the schema are a matched pair; a mismatch is a permission
        # error rather than a disclosure, but it should not be constructible.
        self.assertEqual(SCHEMA_READER[hod.persona_schema], hod.persona_role)
        self.assertEqual(SCHEMA_READER[exec_.persona_schema], exec_.persona_role)

    def test_an_unknown_role_level_falls_back_to_the_executive_role(self) -> None:
        """Default-deny. A role added upstream before it is added here loses money."""
        unknown = PrincipalContext(principal_id="p", role_level="CFO")

        self.assertEqual(settings.ci_copilot_exec_role, unknown.persona_role)
        self.assertEqual(settings.ci_exec_schema, unknown.persona_schema)

    def test_the_two_persona_roles_are_distinct(self) -> None:
        self.assertEqual(2, len(set(PERSONA_ROLES.values())))
        self.assertEqual(set(PERSONA_SCHEMAS), set(PERSONA_ROLES))


class FakeBind:
    """Enough of a connection for _projection_view_body's column lookup."""

    def __init__(self, columns: list[str]) -> None:
        self._columns = columns

    def execute(self, *_args, **_kwargs):  # noqa: ANN001, ANN201
        columns = self._columns

        class _Result:
            def fetchall(self):  # noqa: ANN202
                return [(c,) for c in columns]

        return _Result()


class PersonaViewSqlTests(unittest.TestCase):
    COLUMNS = [
        "calendar_date",
        "opco_code",
        "store_id",
        "customer_type",
        "transaction_count",
        "quantity",
        "gross_sales_amount",
        "net_sales_amount",
        "discount_amount",
        "gmv_amount",
    ]

    def _statements(self, *, schema: str, include_money: bool) -> list[str]:
        return persona_view_sql(
            FakeBind(self.COLUMNS),
            schema,
            "v_sales_daily",
            "fact_sales_daily",
            include_money=include_money,
            copilot_role=SCHEMA_READER[schema],
            analyst_role="ci_analyst",
        )

    def test_views_are_not_security_invoker(self) -> None:
        """security_invoker checks the BASE table against the calling role.

        Keeping it is what forced the read role to hold SELECT on
        ci_core.fact_sales_daily -- and once it holds that, an executive can read
        gross_sales_amount by naming the base table. Only the validator stopped it.
        """
        create = self._statements(schema=HOD, include_money=True)[0]

        self.assertNotIn("security_invoker", create.lower())
        self.assertTrue(create.startswith("CREATE VIEW"))

    def test_each_schema_grants_to_exactly_one_persona_role(self) -> None:
        hod_grants = [
            s
            for s in self._statements(schema=HOD, include_money=True)
            if s.startswith("GRANT SELECT")
        ]
        exec_grants = [
            s
            for s in self._statements(schema=EXEC, include_money=False)
            if s.startswith("GRANT SELECT")
        ]

        self.assertTrue(any(settings.ci_copilot_hod_role in s for s in hod_grants))
        self.assertFalse(any(settings.ci_copilot_exec_role in s for s in hod_grants))

        self.assertTrue(any(settings.ci_copilot_exec_role in s for s in exec_grants))
        self.assertFalse(any(settings.ci_copilot_hod_role in s for s in exec_grants))

    def test_the_executive_view_omits_money_rather_than_masking_it(self) -> None:
        create = self._statements(schema=EXEC, include_money=False)[0]

        for column in MONEY_COLUMNS["fact_sales_daily"]:
            with self.subTest(column=column):
                self.assertNotIn(column, create)

    def test_the_hod_view_keeps_every_money_column(self) -> None:
        create = self._statements(schema=HOD, include_money=True)[0]

        for column in MONEY_COLUMNS["fact_sales_daily"]:
            if column == "member_sales_amount":
                continue  # generated by MEMBER_SPLITS, asserted below
            with self.subTest(column=column):
                self.assertIn(column, create)

        self.assertIn("member_sales_amount", create)

    def test_every_persona_view_has_a_declared_reader(self) -> None:
        for schema in (HOD, EXEC):
            with self.subTest(schema=schema):
                self.assertIn(schema, SCHEMA_READER)
        self.assertEqual(len(PERSONA_VIEWS), len(set(PERSONA_VIEWS)))


if __name__ == "__main__":
    unittest.main()
