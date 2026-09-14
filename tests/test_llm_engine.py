"""LLM engine with a fake Anthropic client: request shape, grounding, fallbacks."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.analysis.engine import AnalysisError, analyze_deck
from app.analysis.llm_engine import FALLBACK_BETA, OUTPUT_SCHEMA, LLMEngineError, build_llm_draft
from app.config import Settings
from app.ingestion.extractor import load_deck
from app.models.schemas import Classification, MaturityLevel, ScoreCategory as C, Severity
from app.reporting.pdf_generator import page_count, render_pdf_bytes
from app.utils.validation import validate_analysis

LLM_SETTINGS = Settings(engine="llm", llm_server_fallbacks=True, llm_attach_pdf=True, ten_capital_endpoint=None)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeMessages:
    """Returns outcomes in order; the last one repeats."""

    def __init__(self, outcome):
        self.outcomes = list(outcome) if isinstance(outcome, list) else [outcome]
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeStream(outcome)


class FakeClient:
    def __init__(self, outcome):
        self.beta = SimpleNamespace(messages=FakeMessages(outcome))


def message(payload, stop_reason="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)],
                           model="claude-opus-5", _request_id="req_test")


def field(value="NOT PROVIDED", classification="NOT PROVIDED", slides=None, quote=""):
    return {"value": value, "classification": classification, "slides": slides or [], "quote": quote}


def model_output() -> dict:
    snapshot = {name: field() for name in ("name", "sector", "stage", "product", "revenue", "growth", "gross_margin",
                                            "customers", "customer_concentration", "funding_to_date",
                                            "raise_amount", "engineering_team")}
    snapshot["name"] = field("QuickMind AI", "FACT", [1], "QuickMind AI")
    snapshot["revenue"] = field("$5M ARR", "FACT", [7], "$5M ARR")  # fabricated
    text = "Concise summary."
    sections = {
        "architecture_summary": "Monolithic application on a single server. The platform is ISO 27001 certified.",
        "architecture_risk_points": ["Single AWS server"], "maturity_level": "Enterprise-Grade",
        "maturity_rationale": "Mature platform.", "scalability_summary": text, "economic_risks": [],
        "engineering_summary": text, "key_person_risk": True, "documentation_summary": text,
        "cybersecurity_summary": "No formal security program.", "ip_summary": text, "ip_core": text,
        "ip_ownership": "University assignment unresolved.", "ip_defensibility": text,
        "ip_patents": "No patents filed.", "ip_trade_secrets": text, "ip_software_data": text,
        "ip_third_party": text, "ai_applicable": True, "ai_summary": text,
        "ai_proprietary_assessment": "Built on third-party models.", "exit_summary": text,
        "conclusion_summary": "QuickMind AI needs significant remediation. It holds HITRUST certification.",
    }
    findings = [
        {"category": "engineering_organization", "topic": "engineering_org",
         "statement": "Platform built by an offshore agency", "classification": "FACT", "polarity": "NEGATIVE",
         "strength": "major", "planned": False, "slides": [4], "quote": "built by an offshore development agency"},
        {"category": "cybersecurity_compliance", "topic": "compliance", "statement": "Platform is ISO 27001 certified",
         "classification": "FACT", "polarity": "POSITIVE", "strength": "major", "planned": False, "slides": [8],
         "quote": "ISO 27001 certified"},
        {"category": "technical_architecture", "topic": "architecture", "statement": "Runs on a single AWS server",
         "classification": "FACT", "polarity": "NEGATIVE", "strength": "major", "planned": False, "slides": [2],
         "quote": "Monolithic Python application running on a single AWS server"},
        {"category": "ip_defensibility", "topic": "patents", "statement": "Company holds 12 issued patents",
         "classification": "FACT", "polarity": "POSITIVE", "strength": "major", "planned": False, "slides": [6],
         "quote": "12 issued patents covering claims parsing"},
    ]
    dependencies = [
        {"name": "OpenAI", "dependency_type": "Foundation-model API", "criticality": "HIGH", "switching_cost": "Medium",
         "notes": "Core product", "slides": [3], "quote": "built on the OpenAI GPT-4 API"},
        {"name": "Salesforce", "dependency_type": "CRM", "criticality": "LOW", "switching_cost": "Low", "notes": "",
         "slides": [3], "quote": ""},
        {"name": "Amazon Web Services", "dependency_type": "Cloud", "criticality": "MEDIUM", "switching_cost": "High",
         "notes": "Deck says AWS", "slides": [4], "quote": "running on a single AWS server"},
    ]
    red_flags = [{
        "code": "SINGLE_POINT_OF_FAILURE", "category": "technical_architecture", "issue": "Single server hosts production",
        "severity": "HIGH", "classification": "FACT", "slides": [4], "quote": "single AWS server",
        "business_consequence": "Outage risk.", "diligence_question": "What is the failover plan?",
        "mitigation": "Add redundancy.", "exposures": ["Diligence question"]}]
    return {
        "snapshot": snapshot, "sections": sections, "findings": findings, "dependencies": dependencies,
        "red_flags": red_flags, "scores": [{"category": c.value, "score": 5, "rationale": "Model proposal."} for c in C],
        "questions": [{"question": "Who owns the agency-written code?", "audience": "IP counsel",
                       "rationale": "Chain of title.", "category": "ip_ownership"}],
        "assumptions": [{"statement": "Pilots are paid.", "classification": "ASSUMPTION", "basis": "Revenue figure."}],
        "missing_information": [{"item": "Invention-assignment agreements", "category": "ip_ownership",
                                 "importance": "HIGH"}],
    }


@pytest.fixture(scope="module")
def risky_deck(risky_pdf):
    return load_deck(risky_pdf, LLM_SETTINGS)


def test_request_uses_opus_schema_prompt_and_fallbacks(risky_deck, risky_pdf):
    client = FakeClient(message(model_output()))
    build_llm_draft(risky_deck, LLM_SETTINGS, client, pdf_bytes=risky_pdf.read_bytes())
    kwargs = client.beta.messages.calls[0]
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["output_config"] == {"effort": "high"}  # no constrained decoding: grammar-size limit
    assert '"red_flags"' in kwargs["system"] and '"additionalProperties":false' in kwargs["system"]
    assert kwargs["fallbacks"] == "default" and FALLBACK_BETA in kwargs["betas"]
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "document" and content[-1]["type"] == "text"
    assert '<slide number="4" title="Technology">' in content[-1]["text"]


def test_model_claims_are_grounded(risky_deck, risky_pdf, rules_settings):
    client = FakeClient(message(model_output()))
    analysis = analyze_deck(risky_deck, LLM_SETTINGS, client=client, engine="llm",
                            source_bytes=risky_pdf.read_bytes())
    assert analysis.engine.name == "llm"
    statements = {f.statement: f for f in analysis.source_evidence}

    agency = statements["Platform built by an offshore agency"]
    assert agency.classification == Classification.FACT and agency.slides == [4] and agency.excerpt

    assert "Platform is ISO 27001 certified" not in statements  # fabricated certification removed
    assert "ISO 27001" not in analysis.technical_architecture.summary
    assert "HITRUST" not in analysis.investor_conclusion.summary

    server = statements["Runs on a single AWS server"]
    assert server.slides == [4]  # wrong slide citation corrected

    patents = statements["Company holds 12 issued patents"]
    assert patents.classification == Classification.INFERENCE and patents.excerpt == ""

    assert analysis.company.revenue.value == "$180K revenue"  # fabricated $5M ARR rejected
    # Unnamed vendor removed; a vendor supported by a verbatim quote ("AWS") kept.
    assert {d.name for d in analysis.third_party_dependencies} == {"OpenAI", "Amazon Web Services"}
    assert analysis.development_maturity.level == MaturityLevel.NOT_DETERMINABLE

    flags = {f.code: f for f in analysis.red_flags}
    assert flags["CONTRACTOR_DEVELOPED_CODE"].severity == Severity.CRITICAL  # merged from deterministic engine
    assert flags["SINGLE_POINT_OF_FAILURE"].slides == [4]

    assert all(item.score <= 3 for item in analysis.scores.categories)  # proposed 5s capped by evidence
    assert analysis.scores.get(C.IP_OWNERSHIP).score <= 2
    assert analysis.warnings

    validate_analysis(analysis)
    assert page_count(render_pdf_bytes(analysis, rules_settings)) == 1


def test_fenced_json_with_prose_is_parsed(risky_deck):
    fenced = "Here is the assessment:\n```json\n" + json.dumps(model_output()) + "\n```\nDone."
    client = FakeClient(message(fenced))
    draft = build_llm_draft(risky_deck, LLM_SETTINGS, client)
    assert draft.engine.name == "llm" and len(client.beta.messages.calls) == 1


def test_invalid_output_is_repaired_on_second_attempt(risky_deck):
    broken = model_output()
    del broken["scores"]
    client = FakeClient([message(broken), message(model_output())])
    draft = build_llm_draft(risky_deck, LLM_SETTINGS, client)
    assert draft.proposed_scores
    repair = client.beta.messages.calls[1]["messages"]
    assert [m["role"] for m in repair] == ["user", "assistant", "user"]
    assert "did not validate" in repair[2]["content"] and "scores" in repair[2]["content"]


def test_refusal_raises_engine_error(risky_deck):
    with pytest.raises(LLMEngineError, match="declined"):
        build_llm_draft(risky_deck, LLM_SETTINGS, FakeClient(message(model_output(), stop_reason="refusal")))


def test_invalid_output_is_retried_then_fails(risky_deck):
    client = FakeClient(message("not json"))
    with pytest.raises(LLMEngineError, match="validated"):
        build_llm_draft(risky_deck, LLM_SETTINGS, client)
    assert len(client.beta.messages.calls) == 2


def test_auto_mode_falls_back_to_rules(risky_deck):
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    analysis = analyze_deck(risky_deck, Settings(engine="auto"), client=FakeClient(error))
    assert analysis.engine.name == "rules"
    assert "LLM" in (analysis.engine.fallback_reason or "")
    assert any("rules engine" in w for w in analysis.warnings)


def test_llm_mode_does_not_silently_fall_back(risky_deck):
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    with pytest.raises(AnalysisError):
        analyze_deck(risky_deck, LLM_SETTINGS, client=FakeClient(error), engine="llm")


def test_output_schema_is_closed_and_fully_required():
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for key, value in node.items():
                if key == "properties":
                    for child in value.values():
                        check(child)
                else:
                    check(value)
        elif isinstance(node, list):
            for item in node:
                check(item)

    check(OUTPUT_SCHEMA)
    assert "$defs" in OUTPUT_SCHEMA
