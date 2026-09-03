from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.routing.llm_router import infer_semantic_route
from app.agent.routing.router_v2 import route_tools_v2
from scripts.eval.common import load_jsonl, save_report


ROOT_DIR = Path(__file__).resolve().parents[2]

EVAL_PATH = (
    ROOT_DIR
    / "data"
    / "eval"
    / "routing_eval.jsonl"
)

REPORT_NAME = "eval_routing_v2"

SIDE_EFFECT_FIELDS = (
    "need_refund_request",
    "need_ticket",
    "need_handoff",
    "handoff_required",
    "manual_review_required",
)


def compare_expected_route(
    expected_route: dict[str, Any],
    actual_route: dict[str, Any],
) -> list[str]:
    """
    只检查 expected_route 中明确声明的字段。
    """

    errors: list[str] = []

    for key, expected_value in expected_route.items():
        actual_value = actual_route.get(key)

        if actual_value != expected_value:
            errors.append(
                f"route.{key} "
                f"expected={expected_value!r}, "
                f"actual={actual_value!r}"
            )

    return errors


def run_single_case(
    case: dict[str, Any],
) -> dict[str, Any]:

    case_id = case["case_id"]
    query = case["query"]

    # -----------------------------
    # 1. 读取新版 golden semantic
    # -----------------------------
    expected_semantic = case[
        "expected_semantic"
    ]

    expected_intent = expected_semantic[
        "intent"
    ]
    expected_action_type = expected_semantic[
        "action_type"
    ]
    expected_topic = expected_semantic[
        "topic"
    ]

    expected_route = case.get(
        "expected_route",
        {},
    )

    # -----------------------------
    # 2. LLM Semantic Router
    # -----------------------------
    semantic = infer_semantic_route(
        query
    )

    # 避免 evaluator 内第二次调用 LLM
    route = route_tools_v2(
        query,
        semantic=semantic,
    )

    semantic_dict = (
        semantic.model_dump()
        if hasattr(
            semantic,
            "model_dump",
        )
        else dict(semantic)
    )

    route_dict = (
        route.model_dump()
        if hasattr(
            route,
            "model_dump",
        )
        else dict(route)
    )

    # -----------------------------
    # 3. Semantic Accuracy
    # -----------------------------
    intent_correct = (
        semantic.intent
        == expected_intent
    )

    action_type_correct = (
        semantic.action_type
        == expected_action_type
    )

    topic_correct = (
        semantic.topic
        == expected_topic
    )

    semantic_correct = (
        intent_correct
        and action_type_correct
        and topic_correct
    )

    errors: list[str] = []

    if not intent_correct:
        errors.append(
            "semantic.intent "
            f"expected={expected_intent!r}, "
            f"actual={semantic.intent!r}"
        )

    if not action_type_correct:
        errors.append(
            "semantic.action_type "
            f"expected={expected_action_type!r}, "
            f"actual={semantic.action_type!r}"
        )

    if not topic_correct:
        errors.append(
            "semantic.topic "
            f"expected={expected_topic!r}, "
            f"actual={semantic.topic!r}"
        )

    # -----------------------------
    # 4. Route Accuracy
    # -----------------------------
    route_errors = compare_expected_route(
        expected_route=expected_route,
        actual_route=route_dict,
    )

    route_correct = (
        len(route_errors) == 0
    )

    errors.extend(route_errors)

    # -----------------------------
    # 5. Clarification Accuracy
    # -----------------------------
    clarification_correct = True

    if (
        "need_clarification"
        in expected_route
    ):
        clarification_correct = (
            route.need_clarification
            == expected_route[
                "need_clarification"
            ]
        )

    passed = (
        semantic_correct
        and route_correct
    )

    return {
        "case_id": case_id,
        "query": query,

        "expected": {
            "semantic": (
                expected_semantic
            ),
            "route": (
                expected_route
            ),
        },

        "actual": {
            "semantic": (
                semantic_dict
            ),
            "route": (
                route_dict
            ),
        },

        "metrics": {
            "intent_correct": (
                intent_correct
            ),
            "action_type_correct": (
                action_type_correct
            ),
            "topic_correct": (
                topic_correct
            ),
            "semantic_correct": (
                semantic_correct
            ),
            "route_correct": (
                route_correct
            ),
            "clarification_correct": (
                clarification_correct
            ),
        },

        "passed": passed,

        "reason": (
            "; ".join(errors)
            if errors
            else None
        ),
    }


def build_report(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:

    total_cases = len(results)

    passed_count = sum(
        1
        for result in results
        if result["passed"]
    )

    failed_count = (
        total_cases
        - passed_count
    )

    intent_correct_count = sum(
        1
        for result in results
        if result[
            "metrics"
        ]["intent_correct"]
    )

    action_type_correct_count = sum(
        1
        for result in results
        if result[
            "metrics"
        ]["action_type_correct"]
    )

    topic_correct_count = sum(
        1
        for result in results
        if result[
            "metrics"
        ]["topic_correct"]
    )

    semantic_correct_count = sum(
        1
        for result in results
        if result[
            "metrics"
        ]["semantic_correct"]
    )

    route_correct_count = sum(
        1
        for result in results
        if result[
            "metrics"
        ]["route_correct"]
    )

    clarification_cases = [
        result
        for result in results
        if (
            "need_clarification"
            in result[
                "expected"
            ]["route"]
        )
    ]

    clarification_correct_count = sum(
        1
        for result
        in clarification_cases
        if result[
            "metrics"
        ]["clarification_correct"]
    )

    # -----------------------------
    # Side-effect false positives
    # -----------------------------
    side_effect_false_positive_cases: list[str] = []

    for result in results:

        expected_route = result[
            "expected"
        ]["route"]

        actual_route = result[
            "actual"
        ]["route"]

        false_positive = False

        for field in SIDE_EFFECT_FIELDS:

            if (
                expected_route.get(
                    field
                )
                is False
                and actual_route.get(
                    field
                )
                is True
            ):
                false_positive = True
                break

        if false_positive:
            side_effect_false_positive_cases.append(
                result["case_id"]
            )

    llm_fallback_cases = [
        result["case_id"]
        for result in results
        if result[
            "actual"
        ]["semantic"].get(
            "source"
        )
        == "fallback"
    ]

    failed_cases = [
        result
        for result in results
        if not result["passed"]
    ]

    def accuracy(
        correct: int,
        total: int,
    ) -> float:

        if total == 0:
            return 1.0

        return round(
            correct / total,
            4,
        )

    return {
        "router_version": (
            "v2_llm_semantic"
        ),

        "side_effects": [
            "[READ ONLY]",
            "[CALLS LLM]",
        ],

        "dataset": str(
            EVAL_PATH
        ),

        "total_cases": (
            total_cases
        ),

        "passed_count": (
            passed_count
        ),

        "failed_count": (
            failed_count
        ),

        "routing_accuracy": accuracy(
            passed_count,
            total_cases,
        ),

        "semantic_accuracy": accuracy(
            semantic_correct_count,
            total_cases,
        ),

        "intent_accuracy": accuracy(
            intent_correct_count,
            total_cases,
        ),

        "action_type_accuracy": accuracy(
            action_type_correct_count,
            total_cases,
        ),

        "topic_accuracy": accuracy(
            topic_correct_count,
            total_cases,
        ),

        "route_accuracy": accuracy(
            route_correct_count,
            total_cases,
        ),

        "clarification_accuracy": accuracy(
            clarification_correct_count,
            len(
                clarification_cases
            ),
        ),

        "side_effect_false_positive_count": (
            len(
                side_effect_false_positive_cases
            )
        ),

        "side_effect_false_positive_cases": (
            side_effect_false_positive_cases
        ),

        "llm_fallback_count": (
            len(
                llm_fallback_cases
            )
        ),

        "llm_fallback_cases": (
            llm_fallback_cases
        ),

        "failed_cases": (
            failed_cases
        ),

        "report_name": (
            REPORT_NAME
        ),
    }


def main() -> None:

    cases = load_jsonl(
        EVAL_PATH
    )

    results = [
        run_single_case(case)
        for case in cases
    ]

    report = build_report(
        cases=cases,
        results=results,
    )

    report_path = save_report(
        REPORT_NAME,
        report,
    )

    print(
        "# Routing V2 Evaluation"
    )
    print()

    for key in (
        "total_cases",
        "passed_count",
        "failed_count",
        "routing_accuracy",
        "semantic_accuracy",
        "intent_accuracy",
        "action_type_accuracy",
        "topic_accuracy",
        "route_accuracy",
        "clarification_accuracy",
        "side_effect_false_positive_count",
        "llm_fallback_count",
    ):
        print(
            f"{key}: "
            f"{report[key]}"
        )

    print(
        "report_path: "
        f"{report_path}"
    )

    if report[
        "failed_count"
    ] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()