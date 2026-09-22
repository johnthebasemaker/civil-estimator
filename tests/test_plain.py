"""Plain-language problems (ui/plain.py).

The headline is what a manager reads through a shared link. It must say what
happened in words and never carry the raw exception; the raw text and the fix
belong in the Technical details expander.
"""
from __future__ import annotations

import pytest

from ui import plain

REFUSED = ("Cannot reach Ollama at http://127.0.0.1:11434: "
           "<urlopen error [Errno 61] Connection refused>")
NOT_PULLED = ("Model 'qwen2.5vl:7b' not pulled. Available: llama3.1:8b. "
              "Run `ollama pull qwen2.5vl:7b`.")

RAW_MARKERS = ("Errno", "urlopen", "Traceback", "http://", "Error:", "`")


class TestModelProblems:
    def test_an_unreachable_service_is_called_offline(self):
        p = plain.model_problem(REFUSED)
        assert p.headline == "The drawing-reading service is offline."
        assert p.detail == REFUSED
        assert "ollama serve" in p.fix

    def test_a_missing_model_names_the_one_to_pull(self):
        p = plain.model_problem(NOT_PULLED)
        assert "isn't installed" in p.headline
        assert p.fix == "ollama pull qwen2.5vl:7b"

    def test_a_timeout_is_not_called_offline(self):
        p = plain.model_problem("timed out after 10s")
        assert "isn't responding" in p.headline

    def test_anything_else_still_gets_a_sentence(self):
        p = plain.model_problem("something nobody anticipated")
        assert p.headline.endswith(".") and p.detail

    @pytest.mark.parametrize("message", [REFUSED, NOT_PULLED, "timed out",
                                         "ValueError: weird"])
    def test_no_headline_carries_raw_text(self, message):
        headline = plain.model_problem(message).headline
        assert not any(m in headline for m in RAW_MARKERS), headline


class TestJobProblems:
    @pytest.mark.parametrize("error, expected", [
        ("the file is no longer at Drawings/x.pdf", "File not found"),
        ("the vision model is unavailable: " + REFUSED, "reading service was offline"),
        ("the worker stopped twice on this drawing — check the worker log",
         "stopped part-way twice"),
        ("RuntimeError: page 3 does not exist", "could not be read"),
    ])
    def test_each_failure_reads_as_a_sentence(self, error, expected):
        text = plain.job_problem(error)
        assert expected in text
        assert not any(m in text for m in RAW_MARKERS), text

    def test_no_error_says_nothing(self):
        assert plain.job_problem("") == ""
