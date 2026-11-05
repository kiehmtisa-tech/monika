#!/usr/bin/env python3
"""rhythm.classify 词法回归测试（不需要 MaiBot 运行环境）。

运行：python3 scripts/test_rhythm.py
默认加载线上插件目录，可用 RHYTHM_PLUGIN_DIR 覆盖（例如本地镜像路径）。
"""
from __future__ import annotations

import os
import pathlib
import sys
import unittest

PLUGIN_DIR = pathlib.Path(
    os.environ.get("RHYTHM_PLUGIN_DIR", "/opt/qq-fish/data/MaiMBot/plugins/monika_rhythm")
)
sys.path.insert(0, str(PLUGIN_DIR))

import rhythm  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def check(self, text: str, expected: str | None, **kwargs) -> None:
        self.assertEqual(rhythm.classify(text, **kwargs), expected, f"输入={text!r}")

    def test_ack_done(self) -> None:
        for text in ("到了", "刚到家", "换了的，回到宿舍马上就换了 😝", "吃了", "那就好", "好嘞", "已经弄好了"):
            self.check(text, rhythm.ACK_DONE)

    def test_parting(self) -> None:
        for text in ("晚安", "晚安，我睡了", "我先撤了", "去忙了拜拜", "睡了睡了"):
            self.check(text, rhythm.PARTING)

    def test_sharing(self) -> None:
        for text in ("今天写完专利了", "东西搞定了", "终于跑通了", "上线了"):
            self.check(text, rhythm.SHARING)

    def test_emotion(self) -> None:
        for text in ("今天有点烦", "好累", "有点崩溃", "心累"):
            self.check(text, rhythm.EMOTION)

    def test_question(self) -> None:
        for text in ("你觉得漫画算文学吗？", "为什么每次都要发三条", "帮我看看这个报错", "写完了吗"):
            self.check(text, rhythm.QUESTION)

    def test_humor(self) -> None:
        for text in ("哈哈这雨也太离谱了", "笑死我了"):
            self.check(text, rhythm.HUMOR)

    def test_noise(self) -> None:
        for text in ("", "   ", "[表情包]", "[图片]"):
            self.check(text, rhythm.NOISE)
        self.check("", rhythm.NOISE, is_picture=True)

    def test_ambiguous_returns_none(self) -> None:
        for text in ("不赖，但是好多都是重复音节", "外面雨挺大", "你觉得呢"):
            self.check(text, None)

    def test_hint_length(self) -> None:
        for state in rhythm.STATES:
            hint = rhythm.build_hint(state)
            self.assertTrue(0 < len(hint) <= 60, f"{state} 提示长度异常: {len(hint)}")
        self.assertEqual(rhythm.build_hint(None), "")
        self.assertEqual(rhythm.build_hint("UNKNOWN"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
