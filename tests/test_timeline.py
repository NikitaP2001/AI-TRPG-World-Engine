"""Unit tests for scheduler/timeline.py — the docs/scene_design.md §4 segment
resolution engine.

Run with: python3 -m unittest tests.test_timeline
"""

from __future__ import annotations

import unittest

from scheduler.timeline import (
    Intent,
    SegmentScheduler,
    parse_duration_seconds,
    validate_schema,
    format_resolved_history_for_sm,
)


class DurationParsingTests(unittest.TestCase):
    def test_zero_is_valid(self):
        self.assertEqual(parse_duration_seconds("0s"), 0)
        self.assertEqual(parse_duration_seconds(""), 0)
        self.assertEqual(parse_duration_seconds(None, default=5), 5)

    def test_units(self):
        self.assertEqual(parse_duration_seconds("5m"), 300)
        self.assertEqual(parse_duration_seconds("2h"), 7200)
        self.assertEqual(parse_duration_seconds("1d"), 86400)
        self.assertEqual(parse_duration_seconds("30 seconds"), 30)


class SchemaValidationTests(unittest.TestCase):
    def test_cosmetic_cannot_be_hostile(self):
        i = Intent(actor="A", action_type="cosmetic", target="self", is_hostile=True)
        violations = validate_schema(i)
        self.assertTrue(any("is_hostile" in v for v in violations))

    def test_private_combat_requires_detection_difficulty(self):
        i = Intent(actor="A", action_type="combat", target="B", visibility="private")
        violations = validate_schema(i)
        self.assertTrue(any("detection_difficulty" in v for v in violations))

    def test_valid_intent_has_no_violations(self):
        i = Intent(actor="A", action_type="speech", target="all")
        self.assertEqual(validate_schema(i), [])


class SegmentSchedulerTests(unittest.TestCase):
    def test_plain_action_recorded_once(self):
        intent = Intent(actor="Alice", action_type="speech", target="all",
                         description="Hello", t_start=0, t_end=5)
        sched = SegmentScheduler([intent], now=0, horizon=60, all_actors=["Alice", "Bob"])
        result = sched.run()
        actions = [e for e in result.resolved_history if e.kind == "action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].actor, "Alice")
        self.assertEqual(result.carry_over_intents, [])
        self.assertIn("Bob", actions[0].perceivers)

    def test_prepared_reaction_fires_on_structural_match(self):
        alice = Intent(actor="Alice", action_type="speech", target="Bob",
                        description="Hello Bob", t_start=0, t_end=5)
        bob = Intent(
            actor="Bob", action_type="speech", target="Alice", description="(waiting)",
            t_start=0, t_end=10,
            trigger_condition={"actor": "Alice", "action_type": "speech", "target_match": "self"},
            on_trigger="apply_prepared_reaction",
            prepared_reaction={"action_type": "speech", "target": "Alice", "description": "Hi Alice!"},
        )
        sched = SegmentScheduler([alice, bob], now=0, horizon=60, all_actors=["Alice", "Bob"])
        result = sched.run()
        reactions = [e for e in result.resolved_history if e.kind == "prepared_reaction"]
        self.assertEqual(len(reactions), 1)
        self.assertEqual(reactions[0].actor, "Bob")
        self.assertEqual(reactions[0].description, "Hi Alice!")
        # bob's subscription intent itself is bounded and consumed by the trigger.
        self.assertEqual(result.carry_over_intents, [])

    def test_reinvoke_requested_without_prepared_reaction(self):
        alice = Intent(actor="Alice", action_type="speech", target="Bob",
                        description="Hello Bob", t_start=0, t_end=5)
        bob = Intent(
            actor="Bob", action_type="speech", target="Alice", description="(waiting)",
            t_start=0, t_end=10,
            trigger_condition={"actor": "Alice", "action_type": "speech", "target_match": "self"},
            on_trigger="interrupt_with_new_decision",
        )
        sched = SegmentScheduler([alice, bob], now=0, horizon=60, all_actors=["Alice", "Bob"])
        result = sched.run()
        self.assertIn("Bob", result.reinvoke)
        self.assertTrue(any("Alice" in note for note in result.reinvoke["Bob"]))

    def test_persistent_subscription_survives_and_carries_over(self):
        alice = Intent(actor="Alice", action_type="speech", target="all",
                        description="Hello everyone", t_start=0, t_end=5)
        bob_sub = Intent(
            actor="Bob", action_type="speech", target="all", description="(standing watch)",
            t_start=0, t_end=None,  # persistent subscription (§3.2)
            trigger_condition={"actor": "any", "action_type": "speech", "target_match": "any"},
            on_trigger="interrupt_with_new_decision",
        )
        sched = SegmentScheduler([alice, bob_sub], now=0, horizon=60, all_actors=["Alice", "Bob"])
        result = sched.run()
        self.assertIn("Bob", result.reinvoke)
        carried_ids = {i.id for i in result.carry_over_intents}
        self.assertIn(bob_sub.id, carried_ids)  # persistent subs remain active after firing

    def test_timeout_fires_when_no_trigger_match(self):
        bob = Intent(
            actor="Bob", action_type="speech", target="all", description="(waiting for reply)",
            t_start=0, t_end=10,
            trigger_condition={"actor": "Alice", "action_type": "speech", "target_match": "self"},
            on_trigger="interrupt_with_new_decision",
            on_timeout={"action_type": "cosmetic", "target": "self", "description": "Bob gives up waiting."},
        )
        sched = SegmentScheduler([bob], now=0, horizon=60, all_actors=["Bob"])
        result = sched.run()
        timeouts = [e for e in result.resolved_history if e.kind == "timeout"]
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0].description, "Bob gives up waiting.")
        self.assertEqual(result.carry_over_intents, [])  # bob's wait completed, no carry-over

    @staticmethod
    def _sneak_and_guard():
        eve = Intent(actor="Eve", action_type="movement", target="vault", visibility="private",
                      detection_difficulty=15, description="sneaks toward the vault",
                      t_start=0, t_end=5)
        guard = Intent(
            actor="Guard", action_type="speech", target="all", description="(alert watch)",
            t_start=0, t_end=10,
            trigger_condition={"actor": "Eve", "action_type": "movement", "target_match": "any"},
            on_trigger="interrupt_with_new_decision",
        )
        return eve, guard

    def test_private_visibility_gated_by_perception_check(self):
        # Scheduler.run() mutates intent status in place, so each scenario
        # needs its own fresh (unrun) Intent instances.
        eve1, guard1 = self._sneak_and_guard()
        failed_gate = SegmentScheduler(
            [eve1, guard1], now=0, horizon=60, all_actors=["Eve", "Guard"],
            perception_gate=lambda intent, observer: False,
        ).run()
        self.assertNotIn("Guard", failed_gate.reinvoke)

        eve2, guard2 = self._sneak_and_guard()
        passed_gate = SegmentScheduler(
            [eve2, guard2], now=0, horizon=60, all_actors=["Eve", "Guard"],
            perception_gate=lambda intent, observer: True,
        ).run()
        self.assertIn("Guard", passed_gate.reinvoke)

    def test_format_resolved_history_for_sm(self):
        intent = Intent(actor="Alice", action_type="speech", target="all",
                         description="Hello", t_start=100, t_end=105)
        result = SegmentScheduler([intent], now=100, horizon=110, all_actors=["Alice"]).run()
        text = format_resolved_history_for_sm(result.resolved_history, base_sec=100)
        self.assertIn("Alice speech -> all", text)
        self.assertIn("+0s", text)


if __name__ == "__main__":
    unittest.main()
