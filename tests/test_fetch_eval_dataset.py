# Copyright SUSE LLC
"""Unit tests for fetch_eval_dataset."""

from __future__ import annotations

import unittest

from scripts.benchmarking.fetch_eval_dataset import (
    CLASS_INFRA,
    CLASS_PRODUCT,
    CLASS_TEST,
    classify_bugref,
    extract_ground_truth,
    parse_llm_comment,
)


class TestFetchEvalDataset(unittest.TestCase):
    """Parses the LLM comment and derives the ground truth of a job."""

    def test_parse_llm_comment(self) -> None:
        cases = [
            {
                "desc": "Standard bold format with Product bug and restart NO",
                "text": ("**LLM Investigation summary:**\n\nClassification: Product bug\nRestart: NO\nBISECT: NO"),
                "expected_class": CLASS_PRODUCT,
                "expected_restart": "NO",
            },
            {
                "desc": "Backtick and asterisk formatted with Test issue and restart YES",
                "text": ("*Classification*: `Test issue`\n*Restart*: `YES`\n"),
                "expected_class": CLASS_TEST,
                "expected_restart": "YES",
            },
            {
                "desc": "Infrastructure problem without restart",
                "text": ("Some preamble\nClassification: Infrastructure problem\nMore explanation..."),
                "expected_class": CLASS_INFRA,
                "expected_restart": None,
            },
            {
                "desc": "Malformed or human comment without classification",
                "text": "This is just a regular human comment without any LLM header.",
                "expected_class": None,
                "expected_restart": None,
            },
        ]
        for case in cases:
            with self.subTest(desc=case["desc"]):
                res = parse_llm_comment(case["text"])
                if case["expected_class"] is None:
                    assert res is None
                else:
                    assert res is not None
                    assert res.classification == case["expected_class"]
                    assert res.restart == case["expected_restart"]

    def test_classify_bugref(self) -> None:
        cases = [
            {
                "desc": "Bugzilla bsc reference",
                "ref": "bsc#1290026",
                "expected": CLASS_PRODUCT,
            },
            {
                "desc": "Bugzilla boo reference",
                "ref": "boo#1288617",
                "expected": CLASS_PRODUCT,
            },
            {
                "desc": "Bugzilla full url",
                "ref": "https://bugzilla.suse.com/show_bug.cgi?id=123",
                "expected": CLASS_PRODUCT,
            },
            {"desc": "Progress ticket", "ref": "poo#199181", "expected": CLASS_TEST},
            {"desc": "Unknown reference format", "ref": "foo#12345", "expected": None},
        ]
        for case in cases:
            with self.subTest(desc=case["desc"]):
                assert classify_bugref(case["ref"]) == case["expected"]

    def test_extract_ground_truth_from_bugs_metadata(self) -> None:
        cases = [
            {
                "desc": "Job with bsc bug in comment_data",
                "job": {
                    "comment_data": {
                        "bugs": [
                            {
                                "content": "bsc#1290026",
                                "url": "https://bugzilla.suse.com/1290026",
                            }
                        ]
                    }
                },
                "comments": [],
                "expected_class": CLASS_PRODUCT,
                "expected_source": "comment_data.bugs",
            },
            {
                "desc": "Job with poo ticket in comment_data",
                "job": {
                    "comment_data": {
                        "bugs": [
                            {
                                "content": "poo#209976",
                                "url": "https://progress.opensuse.org/209976",
                            }
                        ]
                    }
                },
                "comments": [],
                "expected_class": CLASS_TEST,
                "expected_source": "comment_data.bugs",
            },
        ]
        for case in cases:
            with self.subTest(desc=case["desc"]):
                gt = extract_ground_truth(case["job"], case["comments"])
                assert gt is not None
                assert gt.ground_truth_class == case["expected_class"]
                assert gt.source == case["expected_source"]

    def test_extract_ground_truth_from_human_comments(self) -> None:
        cases = [
            {
                "desc": "Human reviewer referenced a Bugzilla bug in follow-up comment",
                "job": {"comment_data": {"bugs": []}},
                "comments": [
                    {
                        "userName": "geekotest",
                        "text": "**LLM Investigation summary:**\nClassification: Test issue",
                    },
                    {
                        "userName": "alice",
                        "text": "Confirmed product defect, filed bsc#1234567 for this regression.",
                    },
                ],
                "expected_class": CLASS_PRODUCT,
                "expected_ref": "bsc#1234567",
            },
            {
                "desc": "Human reviewer noted infrastructure failure in comment",
                "job": {"comment_data": {"bugs": []}},
                "comments": [
                    {
                        "userName": "geekotest",
                        "text": "**LLM Investigation summary:**\nClassification: Product bug",
                    },
                    {
                        "userName": "bob",
                        "text": "Worker network died, this is clearly an infrastructure failure.",
                    },
                ],
                "expected_class": CLASS_INFRA,
                "expected_ref": "Worker network died, this is clearly an infrastructure failure.",
            },
        ]
        for case in cases:
            with self.subTest(desc=case["desc"]):
                gt = extract_ground_truth(case["job"], case["comments"])
                assert gt is not None
                assert gt.ground_truth_class == case["expected_class"]
                assert gt.reference == case["expected_ref"]


if __name__ == "__main__":
    unittest.main()
