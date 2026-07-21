from __future__ import annotations

import unittest
from dataclasses import fields

from merv.brain.application.gate_checklist import present_gate_checklist
from merv.brain.research_core.domain.gates import (
    ForwardTransition,
    ReviewRequirement,
    RoleRequirement,
)


class GateChecklistPresentationTest(unittest.TestCase):
    def test_research_contract_contains_no_agent_guidance_metadata(self) -> None:
        self.assertEqual(
            {field.name for field in fields(RoleRequirement)},
            {"role", "error", "validator", "gate", "missing", "label"},
        )
        self.assertEqual(
            {field.name for field in fields(ReviewRequirement)},
            {"role", "error", "blocker_code", "label"},
        )
        self.assertEqual(
            {field.name for field in fields(ForwardTransition)},
            {
                "name",
                "to_status",
                "requires_prose",
                "requirements",
                "review",
            },
        )

    def test_resource_item_restores_exact_agent_fields_and_order(self) -> None:
        checklist = {
            "status": "planned",
            "transition": "submit_design",
            "leads_to": "design_review",
            "ready": False,
            "items": [
                {
                    "id": "resource:plan",
                    "kind": "resource",
                    "role": "plan",
                    "label": "Plan associated and valid",
                    "satisfied": False,
                    "status": "missing",
                    "gate": "plan_required",
                    "validator": "plan",
                    "missing": "experiment plan resource",
                }
            ],
        }

        item = present_gate_checklist(checklist)["items"][0]

        self.assertEqual(
            list(item),
            [
                "id",
                "kind",
                "role",
                "label",
                "satisfied",
                "status",
                "gate",
                "action",
                "validator",
                "missing",
            ],
        )
        self.assertEqual(item["action"], "write_and_associate_plan_resource")

    def test_review_item_uses_application_owned_skill_and_pass_action(self) -> None:
        base = {
            "id": "review:design_reviewer",
            "kind": "review",
            "role": "design_reviewer",
            "label": "Design review passed",
            "satisfied": True,
            "status": "passed",
            "gate": "design_review",
        }

        item = present_gate_checklist(
            {"items": [base]}
        )["items"][0]

        self.assertEqual(item["action"], "mark_ready_to_run")
        self.assertEqual(item["skill"], "experiment-design-review")
        self.assertEqual(list(item)[-2:], ["action", "skill"])

    def test_reflection_lens_keeps_lens_id_before_label_and_inserts_action_after_gate(self) -> None:
        item = present_gate_checklist(
            {
                "items": [
                    {
                        "id": "reflection_lens:amplify",
                        "kind": "reflection_lens",
                        "role": "reflection_lens_doc",
                        "lens_id": "amplify",
                        "label": "Amplify reflection submitted",
                        "satisfied": False,
                        "status": "missing",
                        "gate": "reflection_roster_incomplete",
                        "missing": "reflection doc for lens 'amplify'",
                    }
                ]
            }
        )["items"][0]

        self.assertEqual(
            list(item),
            [
                "id",
                "kind",
                "role",
                "lens_id",
                "label",
                "satisfied",
                "status",
                "gate",
                "action",
                "missing",
            ],
        )
        self.assertEqual(item["action"], "fan_out_reflection_subagents")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
