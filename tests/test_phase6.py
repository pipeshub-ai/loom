"""Tests for Phase 6 — Ecosystem.

Covers: n8n importer, template system, toolset kinds, eval framework,
drift detection.
"""

from __future__ import annotations

import pytest

from loom.eval.dataset import (
    AggregateScore,
    EvalCase,
    EvalDataset,
    EvalReport,
    EvalScore,
    aggregate_scores,
)
from loom.importers.n8n import (
    FidelityReport,
    ImportResult,
    N8nImporter,
    N8nNode,
)
from loom.templates.template import (
    TemplateEngine,
    TemplateError,
    TemplateManifest,
    TemplateParam,
)
from loom.toolsets.drift import (
    compare_operations,
)
from loom.toolsets.kinds import (
    ToolsetKind,
    classify_toolset,
    validate_namespace,
)

# ---------------------------------------------------------------------------
# n8n Importer
# ---------------------------------------------------------------------------


class TestN8nImporter:
    def _simple_n8n_json(self) -> dict:
        return {
            "name": "My Test Flow",
            "nodes": [
                {
                    "id": "1",
                    "type": "n8n-nodes-base.webhook",
                    "name": "Webhook Trigger",
                    "parameters": {"path": "/test"},
                    "position": [100, 200],
                },
                {
                    "id": "2",
                    "type": "n8n-nodes-base.httpRequest",
                    "name": "Fetch Data",
                    "parameters": {"url": "https://api.example.com"},
                    "position": [300, 200],
                },
                {
                    "id": "3",
                    "type": "n8n-nodes-base.set",
                    "name": "Transform",
                    "parameters": {},
                    "position": [500, 200],
                },
            ],
            "connections": {
                "Webhook Trigger": {
                    "main": [[{"node": "Fetch Data", "type": "main", "index": 0}]]
                },
                "Fetch Data": {
                    "main": [[{"node": "Transform", "type": "main", "index": 0}]]
                },
            },
        }

    def test_import_produces_result(self) -> None:
        importer = N8nImporter()
        result = importer.import_workflow(self._simple_n8n_json())
        assert isinstance(result, ImportResult)
        assert isinstance(result.code, str)
        assert isinstance(result.report, FidelityReport)

    def test_fidelity_report_counts(self) -> None:
        importer = N8nImporter()
        result = importer.import_workflow(self._simple_n8n_json())
        assert result.report.total_nodes == 3
        assert result.report.mapped_nodes >= 2  # at least webhook + httpRequest

    def test_fidelity_score(self) -> None:
        report = FidelityReport(total_nodes=4, mapped_nodes=3, unmapped_nodes=1)
        assert report.fidelity_score == 0.75

    def test_fidelity_score_zero_total(self) -> None:
        report = FidelityReport()
        assert report.fidelity_score == 0.0

    def test_generated_code_is_valid_python(self) -> None:
        importer = N8nImporter()
        result = importer.import_workflow(self._simple_n8n_json())
        # Should compile without errors
        compile(result.code, "<test>", "exec")

    def test_unknown_node_type(self) -> None:
        n8n_json = {
            "name": "Unknown Nodes",
            "nodes": [
                {"id": "1", "type": "custom.weirdNode", "name": "Weird"},
            ],
            "connections": {},
        }
        importer = N8nImporter()
        result = importer.import_workflow(n8n_json)
        assert result.report.unmapped_nodes >= 1

    def test_empty_workflow(self) -> None:
        importer = N8nImporter()
        result = importer.import_workflow({"name": "Empty", "nodes": [], "connections": {}})
        assert result.report.total_nodes == 0
        assert result.report.fidelity_score == 0.0

    def test_n8n_node_model(self) -> None:
        node = N8nNode(id="1", type="test", name="Test Node")
        assert node.id == "1"
        assert node.parameters == {}


# ---------------------------------------------------------------------------
# Template System
# ---------------------------------------------------------------------------


class TestTemplateSystem:
    def _sample_template(self) -> TemplateManifest:
        return TemplateManifest(
            id="crm-sync",
            name="CRM Sync Template",
            description="Sync leads between systems",
            category="crm",
            parameters=[
                TemplateParam(
                    name="source_api",
                    description="Source API URL",
                    required=True,
                ),
                TemplateParam(
                    name="batch_size",
                    description="Batch size",
                    type="int",
                    default=100,
                    required=False,
                ),
                TemplateParam(
                    name="direction",
                    description="Sync direction",
                    type="enum",
                    enum_values=["push", "pull", "bidirectional"],
                    default="push",
                ),
            ],
            source=(
                'API_URL = "{{ source_api }}"\n'
                "BATCH = {{ batch_size }}\n"
                'DIR = "{{ direction }}"'
            ),
        )

    def test_instantiate_replaces_params(self) -> None:
        engine = TemplateEngine()
        template = self._sample_template()
        code = engine.instantiate(
            template, {"source_api": "https://api.example.com", "batch_size": 50}
        )
        assert 'API_URL = "https://api.example.com"' in code
        assert "BATCH = 50" in code
        assert 'DIR = "push"' in code  # default value

    def test_instantiate_missing_required_param(self) -> None:
        engine = TemplateEngine()
        template = self._sample_template()
        with pytest.raises(TemplateError, match="required"):
            engine.instantiate(template, {})

    def test_instantiate_invalid_enum(self) -> None:
        engine = TemplateEngine()
        template = self._sample_template()
        with pytest.raises(TemplateError, match="direction"):
            engine.instantiate(
                template,
                {"source_api": "https://example.com", "direction": "invalid"},
            )

    def test_register_and_list(self) -> None:
        engine = TemplateEngine()
        t1 = TemplateManifest(id="t1", name="T1", category="crm", source="x")
        t2 = TemplateManifest(id="t2", name="T2", category="devops", source="y")
        engine.register(t1)
        engine.register(t2)

        all_templates = engine.list_templates()
        assert len(all_templates) == 2

        crm = engine.list_templates(category="crm")
        assert len(crm) == 1
        assert crm[0].id == "t1"

    def test_get_template(self) -> None:
        engine = TemplateEngine()
        t1 = TemplateManifest(id="t1", name="T1", source="x")
        engine.register(t1)
        assert engine.get("t1") is not None
        assert engine.get("nonexistent") is None

    def test_unregister(self) -> None:
        engine = TemplateEngine()
        t1 = TemplateManifest(id="t1", name="T1", source="x")
        engine.register(t1)
        engine.unregister("t1")
        assert engine.get("t1") is None


# ---------------------------------------------------------------------------
# Toolset Kinds
# ---------------------------------------------------------------------------


class TestToolsetKinds:
    def test_classify_app(self) -> None:
        assert classify_toolset("slack") == ToolsetKind.APP

    def test_classify_knowledge(self) -> None:
        assert classify_toolset("knowledge.rag") == ToolsetKind.KNOWLEDGE

    def test_classify_memory(self) -> None:
        assert classify_toolset("memory.long_term") == ToolsetKind.MEMORY

    def test_classify_skill(self) -> None:
        assert classify_toolset("skill.web_browse") == ToolsetKind.SKILL

    def test_validate_namespace_correct(self) -> None:
        assert validate_namespace("knowledge.rag", ToolsetKind.KNOWLEDGE)
        assert validate_namespace("slack", ToolsetKind.APP)

    def test_validate_namespace_wrong_kind(self) -> None:
        assert not validate_namespace("knowledge.rag", ToolsetKind.APP)

    def test_validate_namespace_reserved_prefix_app(self) -> None:
        # APP kind must not use reserved prefix
        assert not validate_namespace("knowledge.myapp", ToolsetKind.APP)

    def test_all_kinds_exist(self) -> None:
        assert len(ToolsetKind) == 5


# ---------------------------------------------------------------------------
# Eval Framework
# ---------------------------------------------------------------------------


class TestEvalFramework:
    def test_eval_score_overall(self) -> None:
        score = EvalScore(
            case_id="c1",
            compile_pass=True,
            type_check_pass=True,
            behavioral_pass=True,
            structural_score=0.8,
            code_quality_score=0.9,
        )
        # 0.3*1 + 0.2*1 + 0.3*1 + 0.1*0.8 + 0.1*0.9 = 0.97
        assert abs(score.overall - 0.97) < 0.01

    def test_eval_score_zero(self) -> None:
        score = EvalScore(case_id="c1")
        assert score.overall == 0.0

    def test_eval_dataset_size(self) -> None:
        ds = EvalDataset(
            id="basic",
            name="Basic",
            cases=[
                EvalCase(id="c1", input="Build a CRM sync"),
                EvalCase(id="c2", input="Create email pipeline"),
            ],
        )
        assert ds.size == 2

    def test_aggregate_scores(self) -> None:
        scores = [
            EvalScore(
                case_id="c1", compile_pass=True,
                type_check_pass=True, behavioral_pass=True,
                structural_score=0.8, code_quality_score=0.9,
            ),
            EvalScore(
                case_id="c2", compile_pass=True,
                type_check_pass=False, behavioral_pass=False,
                structural_score=0.5, code_quality_score=0.7,
            ),
        ]
        agg = aggregate_scores(scores)
        assert agg.compile_rate == 1.0
        assert agg.type_check_rate == 0.5
        assert agg.behavioral_pass_rate == 0.5

    def test_eval_report_meets_gate(self) -> None:
        report = EvalReport(
            dataset_id="basic",
            aggregate=AggregateScore(compile_rate=0.85),
        )
        assert report.meets_gate("compile_rate>=0.80")
        assert not report.meets_gate("compile_rate>=0.90")

    def test_eval_report_gate_parse_error(self) -> None:
        report = EvalReport(dataset_id="basic")
        with pytest.raises(ValueError):
            report.meets_gate("invalid_gate")


# ---------------------------------------------------------------------------
# Drift Detection
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_no_drift(self) -> None:
        ops = {"items.create": "Create item", "items.list": "List items"}
        report = compare_operations(ops, ops)
        assert not report.has_drift
        assert report.severity == "clean"

    def test_additive_drift(self) -> None:
        current = {"items.create": "Create item"}
        upstream = {"items.create": "Create item", "items.delete": "Delete item"}
        report = compare_operations(current, upstream)
        assert report.has_drift
        assert "items.delete" in report.added_ops
        assert report.severity == "additive"

    def test_breaking_removed(self) -> None:
        current = {"items.create": "Create", "items.delete": "Delete"}
        upstream = {"items.create": "Create"}
        report = compare_operations(current, upstream)
        assert "items.delete" in report.removed_ops
        assert report.severity == "breaking"

    def test_breaking_changed(self) -> None:
        current = {"items.create": "Create item"}
        upstream = {"items.create": "Create a new item with params"}
        report = compare_operations(current, upstream)
        assert len(report.changed_ops) == 1
        assert report.changed_ops[0].op_id == "items.create"
        assert report.severity == "breaking"

    def test_mixed_drift(self) -> None:
        current = {"a": "A desc", "b": "B desc"}
        upstream = {"a": "A changed", "c": "C new"}
        report = compare_operations(current, upstream)
        assert "c" in report.added_ops
        assert "b" in report.removed_ops
        assert len(report.changed_ops) == 1
