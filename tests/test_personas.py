"""组阁逻辑的测试：人数裁剪、反方强制、维度覆盖、信息面隔离。"""

from __future__ import annotations

import unittest

from sparky.config import Config, load_runtime_config
from sparky.errors import SchemaError
from sparky.models import ROLE_ADVERSARY
from sparky.personas import (
    PANEL_MAX,
    PANEL_MIN,
    _clamp_size,
    _enforce_adversary,
    _read_personas,
    _read_sizing,
    _validate_panel,
)

from .support import TempSession, make_brief, persona_payload


def runtime(**overrides):
    base = {
        "raw_input": "测试",
        "api_key_env": "SPARKY_TEST_KEY",
    }
    import os

    os.environ["SPARKY_TEST_KEY"] = "sk-test-not-a-real-key"
    base.update(overrides)
    return load_runtime_config(Config(**base))


class TestClampSize(unittest.TestCase):
    def setUp(self):
        self.temp = TempSession()
        self.addCleanup(self.temp.cleanup)
        self.session = self.temp.session
        self.brief = make_brief()

    @staticmethod
    def _p(index, dimensions, role="specialist"):
        from sparky.models import Persona

        return Persona.from_dict(
            persona_payload(
                index,
                dimensions=dimensions,
                role_type=role,
                blind_spot=None if role == ROLE_ADVERSARY else f"盲点{index}",
            ),
            f"[{index}].",
        )

    def test_below_minimum_is_an_error(self):
        with self.assertRaises(SchemaError) as ctx:
            _clamp_size([], self.brief, self.session)
        self.assertIn("少于下限", str(ctx.exception))

    def test_minimum_passes_through(self):
        built = [self._p(i, ["d1", "d2", "d3"]) for i in range(1, PANEL_MIN + 1)]
        self.assertEqual(len(_clamp_size(built, self.brief, self.session)), PANEL_MIN)

    def test_at_maximum_passes_through(self):
        built = [self._p(i, ["d1", "d2", "d3"]) for i in range(1, PANEL_MAX + 1)]
        kept = _clamp_size(built, self.brief, self.session)
        self.assertEqual(len(kept), PANEL_MAX)
        self.assertEqual(self.session.warnings, [])

    def test_over_maximum_truncates_with_warning(self):
        built = [self._p(i, ["d1", "d2", "d3"]) for i in range(1, 10)]
        kept = _clamp_size(built, self.brief, self.session)
        self.assertEqual(len(kept), PANEL_MAX)
        self.assertTrue(any("panel_size_truncated" in w for w in self.session.warnings))

    def test_adversary_is_never_truncated_away(self):
        """回归：模型把反方排在最后一位时，曾被"从尾部一刀切"切掉，
        随后校验直接报「专家组里没有唱反调者」。"""
        built = [self._p(i, ["d1", "d2", "d3"]) for i in range(1, 8)]
        built.append(self._p(8, ["d1"], role=ROLE_ADVERSARY))
        kept = _clamp_size(built, self.brief, self.session)
        self.assertEqual(len(kept), PANEL_MAX)
        self.assertTrue(any(p.is_adversary for p in kept), "反方被裁掉了")

    def test_truncation_preserves_dimension_coverage(self):
        """独占某个维度的人不能被裁 —— 否则那个维度就没人管了。"""
        built = [self._p(i, ["d1", "d2"]) for i in range(1, 8)]
        built.append(self._p(8, ["d3"]))
        kept = _clamp_size(built, self.brief, self.session)
        self.assertEqual(len(kept), PANEL_MAX)
        covered = {dim for p in kept for dim in p.dimensions}
        self.assertTrue({"d1", "d2", "d3"} <= covered, f"裁剪后出现维度空洞：{covered}")

    def test_prefers_dropping_the_least_contributing(self):
        built = [self._p(i, ["d1", "d2", "d3"]) for i in range(1, 8)]
        built.append(self._p(8, ["d1"]))  # 只贡献 1 个维度，且不是独占
        kept = _clamp_size(built, self.brief, self.session)
        self.assertNotIn("p8", [p.id for p in kept])

    def test_keeps_over_cap_when_nobody_is_droppable(self):
        """每个人都是某个维度的唯一负责人时，宁可超员也不制造空洞。"""
        wide_brief = make_brief(dimension_count=8)
        built = [self._p(i, [f"d{i}"]) for i in range(1, 8)]
        built.append(self._p(8, ["d8"], role=ROLE_ADVERSARY))
        kept = _clamp_size(built, wide_brief, self.session)
        self.assertEqual(len(kept), 8, "不该裁掉任何唯一的维度负责人")
        self.assertTrue(any("panel_size_over_max" in w for w in self.session.warnings))
        self.assertTrue(any(p.is_adversary for p in kept))


class TestEnforceAdversary(unittest.TestCase):
    def setUp(self):
        self.temp = TempSession()
        self.addCleanup(self.temp.cleanup)
        self.session = self.temp.session
        self.brief = make_brief()

    def _personas(self, count: int):
        from sparky.models import Persona

        return [
            Persona.from_dict(persona_payload(i), f"[{i}].") for i in range(1, count + 1)
        ]

    def test_present_adversary_is_untouched(self):
        from sparky.models import Persona

        personas = self._personas(2)
        personas.append(
            Persona.from_dict(
                persona_payload(3, role_type=ROLE_ADVERSARY, blind_spot=None), "[3]."
            )
        )
        result = _enforce_adversary(personas, self.brief, self.session)
        self.assertEqual(len(result), 3)
        self.assertEqual(self.session.warnings, [])

    def test_missing_adversary_is_added_with_warning(self):
        personas = self._personas(3)
        result = _enforce_adversary(personas, self.brief, self.session)
        self.assertEqual(len(result), 4)
        self.assertTrue(any(p.is_adversary for p in result))
        self.assertTrue(any("adversary_auto_added" in w for w in self.session.warnings))

    def test_added_adversary_prefers_thinly_covered_dimensions(self):
        """反方应当去盯"只有一个人管"的维度 —— 那里最缺第二视角。"""
        from sparky.models import Persona

        personas = [
            Persona.from_dict(persona_payload(1, dimensions=["d1"]), "1."),
            Persona.from_dict(persona_payload(2, dimensions=["d1", "d2"]), "2."),
            Persona.from_dict(persona_payload(3, dimensions=["d1", "d2", "d3"]), "3."),
        ]
        result = _enforce_adversary(personas, self.brief, self.session)
        self.assertIn("d3", result[-1].dimensions)


class TestReadPersonas(unittest.TestCase):
    def test_duplicate_question_breaks_information_scoping(self):
        """信息面隔离被破坏时必须报错，这是 ADR 0004 的可执行约束。"""
        payload = {
            "personas": [
                persona_payload(1, info_scope={
                    "questions": ["同一个问题？"], "search_queries": [], "budget": {}}),
                persona_payload(2, info_scope={
                    "questions": ["同一个问题？ "], "search_queries": [], "budget": {}}),
            ]
        }
        with self.assertRaises(SchemaError) as ctx:
            _read_personas(payload)
        self.assertIn("重复", str(ctx.exception))

    def test_duplicate_ids_rejected(self):
        payload = {"personas": [persona_payload(1), persona_payload(1)]}
        with self.assertRaises(SchemaError) as ctx:
            _read_personas(payload)
        self.assertIn("id 重复", str(ctx.exception))

    def test_missing_bias_rejected(self):
        payload = {"personas": [persona_payload(1, bias="")]}
        with self.assertRaises(SchemaError):
            _read_personas(payload)

    def test_non_adversary_without_blind_spot_rejected(self):
        payload = {"personas": [persona_payload(1, blind_spot=None)]}
        with self.assertRaises(SchemaError) as ctx:
            _read_personas(payload)
        self.assertIn("盲点", str(ctx.exception))

    def test_adversary_may_omit_blind_spot(self):
        payload = {
            "personas": [
                persona_payload(1, role_type=ROLE_ADVERSARY, blind_spot=None),
            ]
        }
        self.assertEqual(len(_read_personas(payload)), 1)


class TestReadSizing(unittest.TestCase):
    def setUp(self):
        self.brief = make_brief()
        self.runtime = runtime()

    def test_missing_dimension_assignment_rejected(self):
        data = {"panel_size": 3, "dimension_assignment": {"d1": ["甲"], "d2": ["乙"]}}
        with self.assertRaises(SchemaError) as ctx:
            _read_sizing(data, self.brief, self.runtime)
        self.assertIn("没有分配负责人", str(ctx.exception))

    def test_unknown_dimension_id_rejected(self):
        data = {
            "panel_size": 3,
            "dimension_assignment": {"d1": ["甲"], "d2": ["乙"], "d3": ["丙"], "d9": ["丁"]},
        }
        with self.assertRaises(SchemaError) as ctx:
            _read_sizing(data, self.brief, self.runtime)
        self.assertIn("不存在的维度", str(ctx.exception))

    def test_model_reported_size_loses_to_actual_assignment(self):
        """模型自报人数与实际分配打架时，以实际算出来的为准。"""
        data = {
            "panel_size": 7,
            "dimension_assignment": {"d1": ["甲"], "d2": ["乙"], "d3": ["丙"]},
        }
        size, _ = _read_sizing(data, self.brief, self.runtime)
        self.assertEqual(size, 3)

    def test_cli_override_wins(self):
        forced = runtime(panel_size=5)
        data = {
            "panel_size": 3,
            "dimension_assignment": {"d1": ["甲"], "d2": ["乙"], "d3": ["丙"]},
        }
        size, _ = _read_sizing(data, self.brief, forced)
        self.assertEqual(size, 5)


class TestValidatePanel(unittest.TestCase):
    def setUp(self):
        self.brief = make_brief()

    def test_uncovered_dimension_rejected(self):
        from sparky.models import Persona

        personas = [
            Persona.from_dict(persona_payload(1, dimensions=["d1"]), "1."),
            Persona.from_dict(
                persona_payload(2, dimensions=["d2"], role_type=ROLE_ADVERSARY,
                                blind_spot=None), "2."),
        ]
        with self.assertRaises(SchemaError) as ctx:
            _validate_panel(personas, self.brief)
        self.assertIn("d3", str(ctx.exception))

    def test_full_coverage_passes(self):
        from sparky.models import Persona

        personas = [
            Persona.from_dict(persona_payload(1, dimensions=["d1", "d2"]), "1."),
            Persona.from_dict(
                persona_payload(2, dimensions=["d3"], role_type=ROLE_ADVERSARY,
                                blind_spot=None), "2."),
        ]
        _validate_panel(personas, self.brief)


if __name__ == "__main__":
    unittest.main()
