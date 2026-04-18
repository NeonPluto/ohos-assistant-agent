#!/usr/bin/env python3
"""
DomainSpecificConceptMining executor.

Read model output JSON, normalize required fields, then persist:
- data payload to ./data/domain/<id>.json (includes harmonyos_context /
  harmonyos_constraints / api_citations when present — see SKILL.md v4)
- knowledge graph payload to ./data/knowledge/<id>.json
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persist DomainSpecificConceptMining output to data and knowledge folders."
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


def normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_non_empty(payload, "domain")

    knowledge = payload.get("knowledge")
    if not isinstance(knowledge, dict):
        raise ValueError("Missing required object: knowledge")

    concept_pairs = knowledge.get("concept_pairs")
    if not isinstance(concept_pairs, dict):
        raise ValueError("Missing required object: knowledge.concept_pairs")

    concrete_term = concept_pairs.get("concrete_term")
    abstract_term = concept_pairs.get("abstract_term")
    if not concrete_term or not abstract_term:
        raise ValueError("knowledge.concept_pairs requires concrete_term and abstract_term")

    item_id = payload.get("id")
    if not item_id:
        time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        item_id = f"domain_knowledge_{time_str}_{uuid.uuid4().hex[:6]}"
        payload["id"] = item_id

    graph_id = f"kg_{item_id}"

    domain = payload["domain"]
    kg = payload.get("knowledge_graph", {})
    if not isinstance(kg, dict):
        kg = {}

    # Build default knowledge graph if missing.
    entities = kg.get("entities") or [
        {"id": str(concrete_term), "name": str(concrete_term), "type": "concept"},
        {"id": str(abstract_term), "name": str(abstract_term), "type": "concept"},
        {"id": str(domain), "name": str(domain), "type": "domain"},
    ]
    relations = kg.get("relations") or [
        {"subject": str(concrete_term), "predicate": "is_a", "object": str(abstract_term)},
        {"subject": str(concrete_term), "predicate": "belongs_to_domain", "object": str(domain)},
        {"subject": str(abstract_term), "predicate": "belongs_to_domain", "object": str(domain)},
    ]
    mapping = kg.get("mapping") or {
        "concrete_to_abstract": f"{concrete_term} -> {abstract_term}",
        "concept_to_domain": [f"{concrete_term} -> {domain}", f"{abstract_term} -> {domain}"],
    }

    payload["knowledge_graph"] = {
        "graph_id": graph_id,
        "entities": entities,
        "relations": relations,
        "mapping": mapping,
    }

    payload["storage"] = {
        "data_file_path": f"./data/domain/{item_id}.json",
        "knowledge_file_path": f"./data/knowledge/{item_id}.json",
    }

    # Minimal UI payload fallback, so frontend can always render something.
    ui = payload.get("ui_display")
    if not isinstance(ui, dict):
        payload["ui_display"] = {
            "title": f"{concrete_term} 与 {abstract_term} 的关系",
            "summary": knowledge.get("knowledge_sentence", ""),
            "tags": [str(domain), knowledge.get("relation_type", "未标注"), str(concrete_term), str(abstract_term)],
            "graph_preview": {
                "nodes": [
                    {"id": str(concrete_term), "label": str(concrete_term), "type": "concrete"},
                    {"id": str(abstract_term), "label": str(abstract_term), "type": "abstract"},
                    {"id": str(domain), "label": str(domain), "type": "domain"},
                ],
                "edges": [
                    {"source": str(concrete_term), "target": str(abstract_term), "relation": "下位于/同义于"},
                    {"source": str(concrete_term), "target": str(domain), "relation": "属于垂域"},
                    {"source": str(abstract_term), "target": str(domain), "relation": "属于垂域"},
                ],
            },
        }

    return payload


def write_outputs(payload: Dict[str, Any], workspace: Path) -> Dict[str, Path]:
    data_dir = workspace / "data" / "domain"
    knowledge_dir = workspace / "data" / "knowledge"
    data_dir.mkdir(parents=True, exist_ok=True)
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    item_id = payload["id"]
    data_path = data_dir / f"{item_id}.json"
    knowledge_path = knowledge_dir / f"{item_id}.json"

    data_payload = {
        "id": payload["id"],
        "domain": payload["domain"],
        "source": payload.get("source", {}),
        "knowledge": payload["knowledge"],
        "ui_display": payload["ui_display"],
        "storage": payload["storage"],
    }
    for optional_key in ("harmonyos_context", "harmonyos_constraints", "api_citations"):
        if optional_key in payload:
            data_payload[optional_key] = payload[optional_key]
    knowledge_payload = {
        "id": payload["id"],
        "domain": payload["domain"],
        "knowledge_graph": payload["knowledge_graph"],
        "storage": payload["storage"],
    }

    data_path.write_text(json.dumps(data_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    knowledge_path.write_text(json.dumps(knowledge_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"data_path": data_path, "knowledge_path": knowledge_path}


def main() -> None:
    args = parse_args()
    payload = load_payload(args)
    payload = normalize(payload)
    result = write_outputs(payload, workspace=args.workspace.resolve())

    print("Persist success")
    print(f"id: {payload['id']}")
    print(f"data: {result['data_path']}")
    print(f"knowledge: {result['knowledge_path']}")


if __name__ == "__main__":
    main()
