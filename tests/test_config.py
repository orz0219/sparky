"""配置、密钥与提示词装载的测试。

重点测**严格性**：这个项目宁可直接报错，也不要用默认值掩盖缺失。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sparky.config import (
    Config,
    build_prompt,
    load_context_dir,
    load_prompt,
    load_runtime_config,
    mask_secret,
    parse_env_file,
    render_prompt,
)
from sparky.errors import ConfigError


class TestMaskSecret(unittest.TestCase):
    def test_masks_middle(self):
        self.assertEqual(mask_secret("sk-abcdefghijklmn"), "sk-a…klmn")

    def test_short_secret_fully_masked(self):
        self.assertEqual(mask_secret("abc"), "***")
        self.assertEqual(mask_secret("abcdefgh"), "********")


class TestParseEnvFile(unittest.TestCase):
    def _write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_basic_forms(self):
        path = self._write(
            "# 注释\n"
            "PLAIN=value\n"
            "export EXPORTED=value2\n"
            'QUOTED="quoted value"\n'
            "SINGLE='single value'\n"
            "\n"
        )
        values = parse_env_file(path)
        self.assertEqual(
            values,
            {
                "PLAIN": "value",
                "EXPORTED": "value2",
                "QUOTED": "quoted value",
                "SINGLE": "single value",
            },
        )

    def test_missing_file_is_empty(self):
        self.assertEqual(parse_env_file(Path("/nonexistent/.env")), {})

    def test_bad_line_is_an_error_not_skipped(self):
        """跳过坏行会让"我明明配了密钥"变成无法排查的玄学问题。"""
        path = self._write("GOOD=1\nthis is not a kv line\n")
        with self.assertRaises(ConfigError) as ctx:
            parse_env_file(path)
        self.assertIn("不是合法的 KEY=VALUE", str(ctx.exception))


class TestLoadRuntimeConfig(unittest.TestCase):
    def test_missing_key_raises_config_error(self):
        config = Config(raw_input="测试", api_key_env="SPARKY_SURELY_MISSING_KEY")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPARKY_SURELY_MISSING_KEY", None)
            with self.assertRaises(ConfigError) as ctx:
                load_runtime_config(config, cwd=Path(tempfile.gettempdir()))
        self.assertIn("SPARKY_SURELY_MISSING_KEY", str(ctx.exception))

    def test_key_is_read_from_environment(self):
        os.environ["SPARKY_TEST_PRESENT"] = "sk-live-value"
        self.addCleanup(os.environ.pop, "SPARKY_TEST_PRESENT", None)
        runtime = load_runtime_config(
            Config(raw_input="测试", api_key_env="SPARKY_TEST_PRESENT"),
            cwd=Path(tempfile.gettempdir()),
        )
        self.assertEqual(runtime.api_key, "sk-live-value")

    def test_repr_never_leaks_the_key(self):
        os.environ["SPARKY_TEST_SECRET"] = "sk-super-secret-value"
        self.addCleanup(os.environ.pop, "SPARKY_TEST_SECRET", None)
        runtime = load_runtime_config(
            Config(raw_input="测试", api_key_env="SPARKY_TEST_SECRET"),
            cwd=Path(tempfile.gettempdir()),
        )
        self.assertNotIn("sk-super-secret-value", repr(runtime))
        self.assertNotIn("sk-super-secret-value", str(runtime))

    def test_bad_base_url_rejected(self):
        os.environ["SPARKY_TEST_URLKEY"] = "sk-x"
        self.addCleanup(os.environ.pop, "SPARKY_TEST_URLKEY", None)
        with self.assertRaises(ConfigError) as ctx:
            load_runtime_config(
                Config(raw_input="测试", api_key_env="SPARKY_TEST_URLKEY",
                       base_url="不是地址"),
                cwd=Path(tempfile.gettempdir()),
            )
        self.assertIn("base_url", str(ctx.exception))

    def test_max_tokens_floor(self):
        """推理模型的 reasoning token 会吃光预算，给小了必然截断。"""
        os.environ["SPARKY_TEST_TOKKEY"] = "sk-x"
        self.addCleanup(os.environ.pop, "SPARKY_TEST_TOKKEY", None)
        with self.assertRaises(ConfigError) as ctx:
            load_runtime_config(
                Config(raw_input="测试", api_key_env="SPARKY_TEST_TOKKEY", max_tokens=64),
                cwd=Path(tempfile.gettempdir()),
            )
        self.assertIn("256", str(ctx.exception))

    def test_cli_value_beats_environment(self):
        os.environ["SPARKY_TEST_MODELKEY"] = "sk-x"
        os.environ["SPARKY_MODEL"] = "从环境变量来"
        self.addCleanup(os.environ.pop, "SPARKY_TEST_MODELKEY", None)
        self.addCleanup(os.environ.pop, "SPARKY_MODEL", None)
        runtime = load_runtime_config(
            Config(raw_input="测试", api_key_env="SPARKY_TEST_MODELKEY", model="从命令行来"),
            cwd=Path(tempfile.gettempdir()),
        )
        self.assertEqual(runtime.model_strong, "从命令行来")

    def test_strong_falls_back_to_model(self):
        os.environ["SPARKY_TEST_FBKEY"] = "sk-x"
        self.addCleanup(os.environ.pop, "SPARKY_TEST_FBKEY", None)
        runtime = load_runtime_config(
            Config(raw_input="测试", api_key_env="SPARKY_TEST_FBKEY", model="m1"),
            cwd=Path(tempfile.gettempdir()),
        )
        self.assertEqual(runtime.model_strong, "m1")
        self.assertEqual(runtime.model_fast, "m1")


class TestPromptLoading(unittest.TestCase):
    def test_all_shipped_prompts_load(self):
        """随包分发的每一个提示词块都必须能取出来 —— 缺一个就是安装不完整。"""
        expected = {
            "brief": ["system", "user"],
            "persona": ["system", "size_user", "expand_user"],
            "recon": ["system", "user", "nosearch_note"],
            "synthesis": ["system", "summary", "problem", "candidates",
                          "final", "risks", "milestones"],
        }
        for name, blocks in expected.items():
            for block in blocks:
                with self.subTest(prompt=f"{name}#{block}"):
                    self.assertTrue(load_prompt(name, block).strip())

    def test_missing_block_is_an_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_prompt("brief", "不存在的块")
        self.assertIn("缺少起始标记", str(ctx.exception))

    def test_missing_file_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_prompt("不存在的提示词", "system")

    def test_render_substitutes(self):
        self.assertEqual(
            render_prompt("你好 ${name}", "t", name="世界"), "你好 世界"
        )

    def test_placeholder_may_be_called_name(self):
        """占位符叫 ${name} 不该和函数自身的参数名冲突（曾经真的撞过）。"""
        self.assertEqual(render_prompt("你是 ${name}", "t", name="陈砚"), "你是 陈砚")

    def test_missing_placeholder_value_is_an_error(self):
        with self.assertRaises(ConfigError) as ctx:
            render_prompt("你好 ${name}", "t")
        self.assertIn("没提供", str(ctx.exception))

    def test_extra_value_is_an_error(self):
        """模板与代码脱节必须当场暴露，否则会积累成隐蔽的漂移。"""
        with self.assertRaises(ConfigError) as ctx:
            render_prompt("你好", "t", name="世界")
        self.assertIn("不存在", str(ctx.exception))

    def test_json_braces_survive_rendering(self):
        """占位符用 ${} 而非 {}，正是为了不被提示词里的 JSON 花括号撞死。"""
        rendered = render_prompt('{"a": 1, "b": "${v}"}', "t", v="x")
        self.assertEqual(rendered, '{"a": 1, "b": "x"}')

    def test_real_prompt_renders(self):
        result = build_prompt("brief", "user", raw_input="做个东西", context_digest="（无）")
        self.assertIn("做个东西", result)
        self.assertNotIn("${", result)


class TestLoadContextDir(unittest.TestCase):
    def test_missing_directory_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_context_dir(Path("/nonexistent/context"))

    def test_reads_text_and_lists_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("离线方案要点", encoding="utf-8")
            (root / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            files, skipped = load_context_dir(root)
            self.assertEqual([f.name for f in files], ["notes.md"])
            self.assertEqual(skipped, ["diagram.png"])

    def test_truncation_is_visible(self):
        """截断必须留下明确痕迹 —— 悄悄截一半会让模型基于残缺信息下结论。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.md").write_text("啊" * 5000, encoding="utf-8")
            files, _ = load_context_dir(root, budget=1000)
            self.assertTrue(files[0].truncated)
            self.assertIn("已截断", files[0].render())
            self.assertIn("5000", files[0].render())

    def test_non_utf8_text_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.md").write_bytes(b"\xff\xfe\x00\x01")
            with self.assertRaises(ConfigError) as ctx:
                load_context_dir(root)
            self.assertIn("UTF-8", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
