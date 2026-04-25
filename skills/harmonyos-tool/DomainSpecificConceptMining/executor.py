#!/usr/bin/env python3
"""
DomainSpecificConceptMining executor.

Read model output JSON, normalize and validate knowledges payload, then persist:
- knowledge payload to ./data/domain/knowledge/<id>.json
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persist DomainSpecificConceptMining knowledges output to data/domain/knowledge."
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        help="Path to model output JSON file.",
    )
    parser.add_argument(
        "--input-json",
        type=str,
        help="Raw JSON string. If provided, it has higher priority than --input-file.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root path (default: current directory).",
    )
    return parser.parse_args()


def load_payload(args: argparse.Namespace) -> Dict[str, Any]:
    if args.input_json:
        return json.loads(args.input_json)
    if args.input_file:
        return json.loads(args.input_file.read_text(encoding="utf-8"))
    raise ValueError("Please provide either --input-json or --input-file.")


def ensure_non_empty(payload: Dict[str, Any], key: str) -> None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required field: {key}")


def _ensure_non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required field: {field}")
    return value.strip()


def _normalize_legacy_knowledge(payload: Dict[str, Any]) -> List[Dict[str, Any]] | None:
    """Backward compatibility for old single `knowledge` format."""
    legacy = payload.get("knowledge")
    if not isinstance(legacy, dict):
        return None

    concept_pairs = legacy.get("concept_pairs")
    if isinstance(concept_pairs, dict):
        concrete_term = concept_pairs.get("concrete_term", "")
        abstract_term = concept_pairs.get("abstract_term", "")
        normalized_pairs = [{"concrete_term": concrete_term, "abstract_term": abstract_term}]
    elif isinstance(concept_pairs, list):
        normalized_pairs = concept_pairs
    else:
        normalized_pairs = []

    similar_examples = legacy.get("similar_examples")
    if isinstance(similar_examples, list) and similar_examples and isinstance(similar_examples[0], str):
        normalized_examples = [similar_examples]
    elif isinstance(similar_examples, list):
        normalized_examples = similar_examples
    else:
        normalized_examples = []

    return [
        {
            "knowledge_sentence": legacy.get("knowledge_sentence", ""),
            "relation_type": legacy.get("relation_type", ""),
            "concept_pairs": normalized_pairs,
            "similar_examples": normalized_examples,
        }
    ]


def normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    knowledges = payload.get("knowledges")
    if knowledges is None:
        knowledges = _normalize_legacy_knowledge(payload)

    if not isinstance(knowledges, list) or not knowledges:
        raise ValueError("Missing required array: knowledges")

    allowed_relation_types = {
        "概念同一",
        "同义关系",
        "近义关系",
        "语境关联",
        "上下位关系",
        "语义包含关系",
    }

    normalized_knowledges: List[Dict[str, Any]] = []
    for idx, item in enumerate(knowledges):
        if not isinstance(item, dict):
            raise ValueError(f"knowledges[{idx}] must be an object")

        knowledge_sentence = _ensure_non_empty_string(
            item.get("knowledge_sentence"),
            f"knowledges[{idx}].knowledge_sentence",
        )
        relation_type = _ensure_non_empty_string(
            item.get("relation_type"),
            f"knowledges[{idx}].relation_type",
        )
        if relation_type not in allowed_relation_types:
            raise ValueError(
                f"knowledges[{idx}].relation_type must be one of: "
                + " | ".join(sorted(allowed_relation_types))
            )

        concept_pairs = item.get("concept_pairs")
        if not isinstance(concept_pairs, list) or len(concept_pairs) != 1:
            raise ValueError(f"knowledges[{idx}].concept_pairs must contain exactly 1 pair")

        pair = concept_pairs[0]
        if not isinstance(pair, dict):
            raise ValueError(f"knowledges[{idx}].concept_pairs[0] must be an object")
        concrete_term = _ensure_non_empty_string(
            pair.get("concrete_term"),
            f"knowledges[{idx}].concept_pairs[0].concrete_term",
        )
        abstract_term = _ensure_non_empty_string(
            pair.get("abstract_term"),
            f"knowledges[{idx}].concept_pairs[0].abstract_term",
        )

        similar_examples = item.get("similar_examples")
        if not isinstance(similar_examples, list) or len(similar_examples) != 1:
            raise ValueError(f"knowledges[{idx}].similar_examples must contain exactly 1 example group")
        examples = similar_examples[0]
        if not isinstance(examples, list) or not (2 <= len(examples) <= 4):
            raise ValueError(
                f"knowledges[{idx}].similar_examples[0] must include 2-4 examples"
            )
        normalized_examples = []
        for ex_idx, example in enumerate(examples):
            normalized_examples.append(
                _ensure_non_empty_string(
                    example,
                    f"knowledges[{idx}].similar_examples[0][{ex_idx}]",
                )
            )

        normalized_knowledges.append(
            {
                "knowledge_sentence": knowledge_sentence,
                "relation_type": relation_type,
                "concept_pairs": [
                    {"concrete_term": concrete_term, "abstract_term": abstract_term}
                ],
                "similar_examples": [normalized_examples],
            }
        )

    item_id = payload.get("id")
    if not item_id:
        time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        item_id = f"domain_knowledge_{time_str}_{uuid.uuid4().hex[:6]}"

    return {"id": item_id, "knowledges": normalized_knowledges}


def write_outputs(payload: Dict[str, Any], workspace: Path) -> Dict[str, Path]:
    knowledge_dir = workspace / "data" / "domain" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    item_id = payload["id"]
    knowledge_path = knowledge_dir / f"{item_id}.json"
    knowledge_payload = {"knowledges": payload["knowledges"]}

    knowledge_path.write_text(
        json.dumps(knowledge_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"knowledge_path": knowledge_path}


def main() -> None:
    args = parse_args()
    payload = load_payload(args)
    payload = normalize(payload)
    result = write_outputs(payload, workspace=args.workspace.resolve())

    print("Persist success")
    print(f"id: {payload['id']}")
    print(f"knowledge: {result['knowledge_path']}")


if __name__ == "__main__":
    main()