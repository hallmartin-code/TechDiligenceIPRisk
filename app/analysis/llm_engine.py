"""Claude-based analysis engine.

One streamed request reads the slide-tagged deck (plus the PDF itself, so
diagrams are visible) and returns JSON — findings, snapshot facts, dependencies,
red flags, proposed scores, questions and section narratives — validated against
a Pydantic schema, with one repair round if validation fails. Nothing from the
response is trusted as-is:

* every FACT must cite a quote that is found on a slide, or it is downgraded
  to INFERENCE (or dropped, for certifications / patents / ownership claims);
* numeric snapshot values must appear in the deck text;
* narrative sentences asserting unsupported sensitive subjects are removed;
* deterministic CRITICAL/HIGH flags the model missed are merged back in;
* scores are only proposals — `engine.finalize` applies the evidence caps.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date
from typing import Any, Literal
from xml.sax.saxutils import escape

from pydantic import BaseModel, ValidationError

from app.analysis.draft import AnalysisDraft
from app.analysis.grounding import DeckIndex
from app.analysis.red_flags import FLAG_CODES, TEMPLATES
from app.config import Settings
from app.ingestion.document import DeckDocument
from app.ingestion.extractor import extract_company_snapshot
from app.models.schemas import (
    NOT_PROVIDED,
    SEVERITY_RANK,
    AIDataAssessment,
    Assumption,
    Classification,
    CompanySnapshot,
    DevelopmentMaturityAssessment,
    DiligenceQuestion,
    EngineeringOrganizationAssessment,
    EngineInfo,
    Finding,
    IntellectualPropertyAssessment,
    MaturityLevel,
    MissingInformation,
    Polarity,
    RedFlag,
    ScalabilityAssessment,
    ScoreCategory as C,
    SectionAssessment,
    Severity,
    SnapshotField,
    TechnicalArchitectureAssessment,
    ThirdPartyDependency,
    Topic,
    TransactionExposure,
)
from app.utils.logging import get_logger
from app.utils.text import shorten, slide_ref

log = get_logger("analysis.llm")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_DECK_CHARACTERS = 600_000
STRENGTH_WEIGHT = {"minor": 0.5, "moderate": 1.0, "major": 1.5}


class LLMEngineError(RuntimeError):
    """The LLM engine could not produce a usable analysis."""


CategoryLit = Literal[tuple(c.value for c in C)]  # type: ignore[valid-type]
TopicLit = Literal[tuple(t.value for t in Topic)]  # type: ignore[valid-type]
SeverityLit = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
FlagCodeLit = Literal[FLAG_CODES]  # type: ignore[valid-type]
ExposureLit = Literal[tuple(x.value for x in TransactionExposure)]  # type: ignore[valid-type]
MaturityLit = Literal[tuple(m.value for m in MaturityLevel)]  # type: ignore[valid-type]


class LLMField(BaseModel):
    value: str
    classification: Literal["FACT", "INFERENCE", "ASSUMPTION", "NOT PROVIDED"]
    slides: list[int]
    quote: str


class LLMSnapshot(BaseModel):
    name: LLMField
    sector: LLMField
    stage: LLMField
    product: LLMField
    revenue: LLMField
    growth: LLMField
    gross_margin: LLMField
    customers: LLMField
    customer_concentration: LLMField
    funding_to_date: LLMField
    raise_amount: LLMField
    engineering_team: LLMField


class LLMFinding(BaseModel):
    category: CategoryLit
    topic: TopicLit
    statement: str
    classification: Literal["FACT", "INFERENCE", "ASSUMPTION"]
    polarity: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"]
    strength: Literal["minor", "moderate", "major"]
    planned: bool
    slides: list[int]
    quote: str


class LLMDependency(BaseModel):
    name: str
    dependency_type: str
    criticality: SeverityLit
    switching_cost: Literal["Low", "Medium", "High", "Unknown"]
    notes: str
    slides: list[int]
    quote: str


class LLMRedFlag(BaseModel):
    code: FlagCodeLit
    category: CategoryLit
    issue: str
    severity: SeverityLit
    classification: Literal["FACT", "INFERENCE", "NOT PROVIDED"]
    slides: list[int]
    quote: str
    business_consequence: str
    diligence_question: str
    mitigation: str
    exposures: list[ExposureLit]


class LLMScore(BaseModel):
    category: CategoryLit
    score: Literal[1, 2, 3, 4, 5]
    rationale: str


class LLMQuestion(BaseModel):
    question: str
    audience: str
    rationale: str
    category: CategoryLit


class LLMAssumption(BaseModel):
    statement: str
    classification: Literal["INFERENCE", "ASSUMPTION"]
    basis: str


class LLMMissing(BaseModel):
    item: str
    category: CategoryLit
    importance: SeverityLit


class LLMSections(BaseModel):
    architecture_summary: str
    architecture_risk_points: list[str]
    maturity_level: MaturityLit
    maturity_rationale: str
    scalability_summary: str
    economic_risks: list[str]
    engineering_summary: str
    key_person_risk: bool
    documentation_summary: str
    cybersecurity_summary: str
    ip_summary: str
    ip_core: str
    ip_ownership: str
    ip_defensibility: str
    ip_patents: str
    ip_trade_secrets: str
    ip_software_data: str
    ip_third_party: str
    ai_applicable: bool
    ai_summary: str
    ai_proprietary_assessment: str
    exit_summary: str
    conclusion_summary: str


class LLMAnalysisOutput(BaseModel):
    snapshot: LLMSnapshot
    sections: LLMSections
    findings: list[LLMFinding]
    dependencies: list[LLMDependency]
    red_flags: list[LLMRedFlag]
    scores: list[LLMScore]
    questions: list[LLMQuestion]
    assumptions: list[LLMAssumption]
    missing_information: list[LLMMissing]


_UNSUPPORTED_KEYWORDS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
                         "minItems", "maxItems", "pattern", "format", "default", "title"}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema adapted for structured outputs: closed objects, all fields required."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in list(node):
                if key in _UNSUPPORTED_KEYWORDS and not isinstance(node.get(key), dict):
                    node.pop(key)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for key, value in node.items():
                if key == "properties":
                    for child in value.values():
                        walk(child)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return schema


OUTPUT_SCHEMA = strict_schema(LLMAnalysisOutput)

SYSTEM_PROMPT = f"""You are a venture technical-diligence, intellectual-property and exit-readiness expert reviewing an investor pitch deck for a TEN Capital investment committee. Assess whether the company's technology and IP are mature, defensible, scalable, documented and transferable enough to withstand Series B+, growth-equity, strategic or acquisition diligence. Do not summarize the deck; perform an evidence-based diligence assessment focused on issues that delay or derail financings and deal closings.

Evidence rules (these are audited by software after you respond):
- Use only the deck. Never invent patents, certifications, customers, architecture, open-source usage, IP ownership, metrics or people.
- FACT: directly stated in the deck. Every FACT must include "quote": a short verbatim excerpt (under 25 words) copied exactly from the slide text, and the slide number(s) where it appears. FACTs whose quote cannot be found on the slide are downgraded or discarded.
- INFERENCE: a reasonable interpretation of deck evidence. Quote the supporting text when there is one; evidence visible only in an image or diagram is an INFERENCE with an empty quote and the slide number.
- ASSUMPTION: used only because required information is absent.
- NOT PROVIDED: information diligence needs that the deck does not contain. For snapshot fields that are absent use value "NOT PROVIDED", classification "NOT PROVIDED", slides [], quote "".
- Roadmap, "planned", "in progress", "pursuing" or use-of-funds items are not completed work: mark such findings planned=true and never credit them as achieved.
- A patent's existence is not proof of defensibility; assess claim coverage of the commercial product, ownership and jurisdiction only as far as the deck supports. Never conclude freedom to operate without explicit evidence.
- Only evaluate security or compliance frameworks relevant to the company's sector and customers.
- For AI/ML/data companies, decide whether the claimed advantage is proprietary (data, models, workflow) or primarily built on third-party models and infrastructure.

What to produce:
- snapshot: company name, sector, stage, product, revenue, growth, gross margin, customers, customer concentration, funding to date, current raise, engineering team size. Snapshot values are terse display values of at most 6 words (for example "Series B", "Enterprise SaaS", "$4.1M ARR", "72%") with no commentary; put detail in findings.
- findings: up to 45 material findings across all ten categories ({", ".join(c.value for c in C)}). strength reflects diligence significance (minor / moderate / major). Include negative evidence and gaps as findings only when the deck states them; absent items belong in missing_information.
- dependencies: third-party platforms, model providers, clouds, licenses, suppliers the deck names, with switching cost and criticality.
- red_flags: every material issue, using the closest code from the allowed list (OTHER if none fits). State each issue in at most 12 words; business_consequence and mitigation in at most 30 words each. Severity: CRITICAL = could prevent investment, acquisition, financing, licensing or closing; HIGH = likely a major diligence issue needing remediation; MEDIUM = investigate but manageable; LOW = minor gap. Use classification NOT PROVIDED for flags raised because expected evidence is absent. exposures lists how the issue would surface in a transaction.
- scores: one per category, integer 1-5 (5 Strong: well-supported, mature, limited risk; 4 Good: manageable gaps; 3 Moderate: material unanswered questions; 2 Weak: meaningful concerns; 1 High Risk: potential financing, transaction, scalability or ownership issue). Absence of evidence is not strength: undocumented categories should not score above 3.
- questions: the 5-8 highest-priority questions an investor, technical advisor, IP attorney, CTO, CISO or acquirer should ask next, prioritizing questions that could change the transaction decision. Name the audience.
- sections: concise narratives. Each summary at most 35 words, written for an investment committee; conclusion_summary is 2-3 sentences (at most 70 words) giving the investment-diligence conclusion. ip_* fields at most 30 words each; say NOT PROVIDED where the deck is silent.
- assumptions and missing_information: list what you inferred or could not find, especially items an acquirer's diligence would require."""

SYSTEM_PROMPT += (
    "\n\nOutput format: respond with a single JSON object and nothing else (no prose, no markdown, no code "
    "fences). It must validate against the JSON Schema below. Every property is required: use \"\", [] or "
    "false rather than omitting a field, and use only the enumerated values where an enum is given.\n"
    + json.dumps(OUTPUT_SCHEMA, separators=(",", ":"), sort_keys=True)
)


def build_deck_prompt(deck: DeckDocument) -> str:
    parts = [f'<deck filename="{escape(deck.filename)}" slides="{deck.slide_count}">']
    for slide in deck.slides:
        attributes = f'number="{slide.number}" title="{escape(slide.title, {chr(34): "&quot;"})}"'
        if slide.image_count:
            attributes += f' images="{slide.image_count}"'
        body = slide.full_text or "[no machine-readable text on this slide]"
        parts.append(f"<slide {attributes}>\n{body}\n</slide>")
    parts.append("</deck>")
    return "\n".join(parts)


def _content(deck: DeckDocument, settings: Settings, pdf_bytes: bytes | None) -> list[dict[str, Any]]:
    prompt = build_deck_prompt(deck)
    if len(prompt) > MAX_DECK_CHARACTERS:
        raise LLMEngineError(f"Deck text ({len(prompt):,} characters) exceeds the LLM input limit.")
    content: list[dict[str, Any]] = []
    if (pdf_bytes and deck.file_type == "pdf" and settings.llm_attach_pdf
            and len(pdf_bytes) <= settings.llm_attach_max_mb * 1024 * 1024 and deck.slide_count <= 100):
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                        "data": base64.standard_b64encode(pdf_bytes).decode("ascii")}})
    content.append({"type": "text", "text": (
        f"Today's date is {date.today().isoformat()}. The attached PDF (when present) is the same deck; the "
        "extracted slide text below is the authoritative source for quotes and slide numbers.\n\n"
        f"{prompt}\n\nProduce the technical and IP diligence assessment as specified.")})
    return content


def build_client(settings: Settings) -> Any:
    try:
        import anthropic
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency is required
        raise LLMEngineError("The anthropic package is not installed.") from exc
    try:
        return anthropic.Anthropic(timeout=settings.llm_timeout_seconds, max_retries=2)
    except Exception as exc:
        raise LLMEngineError(f"Anthropic client could not be created: {exc}") from exc


def _request(client: Any, settings: Settings, messages: list[dict[str, Any]], use_fallbacks: bool) -> str:
    # The schema travels in the system prompt rather than as constrained decoding
    # (output_config.format): the full diligence schema exceeds the API's grammar-size
    # limit for structured outputs. The response is validated with Pydantic instead.
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": messages,
        "output_config": {"effort": settings.llm_effort},
    }
    if use_fallbacks:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    with client.beta.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise LLMEngineError("The model declined to analyze this deck.")
    if message.stop_reason == "max_tokens":
        raise LLMEngineError("Model output was truncated; raise TDIP_LLM_MAX_TOKENS.")
    usage = getattr(message, "usage", None)
    log.info("LLM analysis complete (model=%s, message_id=%s, input_tokens=%s, output_tokens=%s)",
             getattr(message, "model", settings.llm_model), getattr(message, "id", None),
             getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")


def call_model(client: Any, settings: Settings, content: list[dict[str, Any]]) -> LLMAnalysisOutput:
    import anthropic

    use_fallbacks = settings.llm_server_fallbacks
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    repairs = 0
    while True:
        try:
            text = _request(client, settings, messages, use_fallbacks)
        except anthropic.AuthenticationError as exc:
            raise LLMEngineError("Anthropic authentication failed; check ANTHROPIC_API_KEY.") from exc
        except anthropic.BadRequestError as exc:
            if use_fallbacks and "fallback" in str(exc).lower():
                log.warning("Server-side fallbacks rejected; retrying without them")
                use_fallbacks = False
                continue
            raise LLMEngineError(f"Anthropic rejected the request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMEngineError("Anthropic rate limit reached; try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMEngineError(f"Anthropic API error ({exc.status_code}).") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMEngineError("Could not reach the Anthropic API.") from exc
        except LLMEngineError:
            raise
        except Exception as exc:  # unexpected client failure: surface as an engine error, keep the traceback
            log.exception("Unexpected failure calling the model")
            raise LLMEngineError(f"Unexpected LLM failure ({type(exc).__name__}).") from exc

        try:
            return parse_output(text)
        except (ValidationError, ValueError) as exc:
            repairs += 1
            log.warning("LLM output failed schema validation (attempt %d): %s", repairs, shorten(str(exc), 500))
            if repairs >= 2:
                raise LLMEngineError(f"Model output could not be validated: {shorten(str(exc), 300)}") from exc
            messages = [
                {"role": "user", "content": content},
                {"role": "assistant", "content": text.strip() or "{}"},
                {"role": "user", "content": (
                    "That response did not validate against the required JSON schema:\n"
                    f"{shorten(str(exc), 2000)}\n\nReturn the complete, corrected JSON object only, "
                    "with no prose or code fences.")},
            ]


def parse_output(text: str) -> LLMAnalysisOutput:
    """Extract the JSON object from the response (tolerating fences or stray prose) and validate it."""
    cleaned = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object found in the model response.")
    return LLMAnalysisOutput.model_validate_json(cleaned[start:end + 1])


def build_llm_draft(deck: DeckDocument, settings: Settings, client: Any | None = None,
                    pdf_bytes: bytes | None = None) -> AnalysisDraft:
    client = client or build_client(settings)
    content = _content(deck, settings, pdf_bytes)
    output = call_model(client, settings, content)
    return convert_output(output, deck, settings)


def convert_output(output: LLMAnalysisOutput, deck: DeckDocument, settings: Settings) -> AnalysisDraft:
    """Ground, filter and convert validated model output into an engine-neutral draft."""
    from app.analysis.engine import STANDARD_ASSUMPTIONS, build_rules_draft

    index = DeckIndex(deck)
    warnings: list[str] = []
    rejections = 0

    # --- Snapshot ---------------------------------------------------------------
    snapshot = CompanySnapshot()
    rules_snapshot = extract_company_snapshot(deck)
    numeric = {"revenue", "growth", "gross_margin", "customers", "customer_concentration", "funding_to_date",
               "raise_amount", "engineering_team"}
    for name in CompanySnapshot.model_fields:
        field: LLMField = getattr(output.snapshot, name)
        value = field.value.strip()
        if field.classification == "NOT PROVIDED" or not value or value.upper() == NOT_PROVIDED:
            setattr(snapshot, name, getattr(rules_snapshot, name))
            continue
        located = index.locate(field.quote, field.slides) if field.quote else []
        if name in numeric and (not located or not index.numbers_supported(value)):
            rejections += 1
            warnings.append(f"Snapshot {name.replace('_', ' ')} '{shorten(value, 40)}' was not verifiable in the "
                            "deck text and was not used.")
            setattr(snapshot, name, getattr(rules_snapshot, name))
            continue
        if field.classification == "FACT" and located:
            classification = Classification.FACT
        elif field.classification == "ASSUMPTION":
            classification = Classification.ASSUMPTION
        else:
            classification = Classification.INFERENCE
        setattr(snapshot, name, SnapshotField(
            value=shorten(value, 90), classification=classification,
            slides=located or index.valid_slides(field.slides),
            excerpt=shorten(field.quote, 240) if located else ""))

    # --- Findings ---------------------------------------------------------------
    findings: list[Finding] = []
    for item in output.findings:
        cited = index.valid_slides(item.slides)
        located = index.locate(item.quote, cited) if item.quote else []
        classification = Classification(item.classification)
        unsupported = index.unsupported_subjects(item.statement)
        if unsupported and not located:
            rejections += 1
            warnings.append(f"Removed unverifiable {unsupported[0].replace('_', ' ')} claim: {shorten(item.statement, 90)}")
            continue
        if classification == Classification.FACT and not located:
            rejections += 1
            classification = Classification.INFERENCE
        planned = bool(item.planned)
        findings.append(Finding(
            id=f"F{len(findings) + 1:03d}", category=C(item.category), topic=Topic(item.topic),
            statement=shorten(item.statement, 220),
            classification=Classification.INFERENCE if planned and classification == Classification.FACT else classification,
            polarity=Polarity.NEUTRAL if planned else Polarity(item.polarity),
            weight=0.0 if planned else STRENGTH_WEIGHT[item.strength],
            slides=located or cited, excerpt=shorten(item.quote, 240) if located else "",
            signal_key=None, planned=planned))

    # --- Dependencies -----------------------------------------------------------
    dependencies: list[ThirdPartyDependency] = []
    for dep in output.dependencies:
        located = index.locate(dep.quote, dep.slides) if dep.quote else []
        # Accept a verbatim quote as support too: the deck may say "AWS" where the model says "Amazon Web Services".
        if not located and not index.mentions(dep.name):
            rejections += 1
            warnings.append(f"Removed dependency not named in the deck: {shorten(dep.name, 40)}")
            continue
        dependencies.append(ThirdPartyDependency(
            name=shorten(dep.name, 60), dependency_type=shorten(dep.dependency_type, 60),
            criticality=Severity(dep.criticality), switching_cost=dep.switching_cost, notes=shorten(dep.notes, 200),
            classification=Classification.FACT if located else Classification.INFERENCE,
            slides=located or index.valid_slides(dep.slides)))

    # --- Red flags --------------------------------------------------------------
    flags: dict[str, RedFlag] = {}
    for position, item in enumerate(output.red_flags):
        if index.unsupported_subjects(item.issue):
            rejections += 1
            warnings.append(f"Removed red flag asserting unverifiable subject: {shorten(item.issue, 90)}")
            continue
        located = index.locate(item.quote, item.slides) if item.quote else []
        classification = Classification(item.classification)
        if classification == Classification.FACT and not located:
            classification = Classification.INFERENCE
            rejections += 1
        if located:
            evidence = f'{slide_ref(located)}: "{shorten(item.quote, 160)}"'
        elif classification == Classification.NOT_PROVIDED:
            evidence = "Not addressed anywhere in the deck."
        else:
            evidence = "Inferred from deck context; no verbatim support located."
        template = TEMPLATES.get(item.code)
        flag = RedFlag(
            code=item.code, category=C(item.category), issue=shorten(item.issue, 160), severity=Severity(item.severity),
            classification=classification, evidence=evidence,
            slides=located if classification != Classification.NOT_PROVIDED else [],
            business_consequence=shorten(item.business_consequence or (template.consequence if template else "—"), 260),
            diligence_question=shorten(item.diligence_question or (template.question if template else "—"), 240),
            mitigation=shorten(item.mitigation or (template.mitigation if template else "—"), 240),
            exposures=[TransactionExposure(x) for x in item.exposures] or (list(template.exposures) if template else []))
        key = item.code if item.code != "OTHER" else f"OTHER_{position}"
        if key not in flags or SEVERITY_RANK[flag.severity] < SEVERITY_RANK[flags[key].severity]:
            flags[key] = flag

    # Safety net: deterministic, signal-backed CRITICAL/HIGH flags are never dropped.
    rules = build_rules_draft(deck, settings)
    for rule_flag in rules.flags:
        if rule_flag.severity not in (Severity.CRITICAL, Severity.HIGH):
            continue
        if rule_flag.classification == Classification.NOT_PROVIDED:
            continue
        existing = flags.get(rule_flag.code)
        if existing is None:
            flags[rule_flag.code] = rule_flag
        elif SEVERITY_RANK[rule_flag.severity] < SEVERITY_RANK[existing.severity]:
            flags[rule_flag.code] = existing.model_copy(update={"severity": rule_flag.severity})

    # --- Sections ---------------------------------------------------------------
    sections = output.sections
    removed_total = 0

    def clean_text(text: str) -> str:
        nonlocal removed_total
        result, removed = index.scrub(text)
        removed_total += removed
        return shorten(result, 420)

    def ids(*categories: C) -> list[str]:
        return [f.id for f in findings if f.category in categories]

    missing = [MissingInformation(item=shorten(m.item, 120), category=C(m.category), importance=Severity(m.importance))
               for m in output.missing_information]

    def gaps(*categories: C) -> list[str]:
        return [m.item for m in missing if m.category in categories]

    maturity_level = MaturityLevel(sections.maturity_level)
    grounded_maturity = [f for f in findings if f.category == C.DEVELOPMENT_MATURITY
                         and f.classification == Classification.FACT and f.polarity == Polarity.POSITIVE]
    maturity_rationale = clean_text(sections.maturity_rationale)
    if maturity_level in (MaturityLevel.PRODUCTION, MaturityLevel.SCALED_PRODUCTION,
                          MaturityLevel.ENTERPRISE_GRADE) and not grounded_maturity:
        maturity_level = MaturityLevel.NOT_DETERMINABLE
        maturity_rationale = "Production maturity claimed but not supported by verifiable deck text."
        rejections += 1
    if maturity_level == MaturityLevel.ENTERPRISE_GRADE and not any(
            f.category == C.CYBERSECURITY_COMPLIANCE and f.classification == Classification.FACT for f in findings):
        maturity_level = MaturityLevel.SCALED_PRODUCTION

    draft = AnalysisDraft(
        engine=EngineInfo(name="llm", model=settings.llm_model),
        snapshot=snapshot,
        findings=findings,
        technical_architecture=TechnicalArchitectureAssessment(
            summary=clean_text(sections.architecture_summary), finding_ids=ids(C.TECHNICAL_ARCHITECTURE),
            gaps=gaps(C.TECHNICAL_ARCHITECTURE),
            risk_points=[shorten(p, 160) for p in sections.architecture_risk_points][:8]),
        development_maturity=DevelopmentMaturityAssessment(
            summary=maturity_rationale, finding_ids=ids(C.DEVELOPMENT_MATURITY), gaps=gaps(C.DEVELOPMENT_MATURITY),
            level=maturity_level, rationale=maturity_rationale),
        engineering_organization=EngineeringOrganizationAssessment(
            summary=clean_text(sections.engineering_summary), finding_ids=ids(C.ENGINEERING_ORGANIZATION),
            gaps=gaps(C.ENGINEERING_ORGANIZATION), key_person_risk=sections.key_person_risk),
        technical_documentation=SectionAssessment(
            summary=clean_text(sections.documentation_summary), finding_ids=ids(C.TECHNICAL_DOCUMENTATION),
            gaps=gaps(C.TECHNICAL_DOCUMENTATION)),
        cybersecurity=SectionAssessment(
            summary=clean_text(sections.cybersecurity_summary), finding_ids=ids(C.CYBERSECURITY_COMPLIANCE),
            gaps=gaps(C.CYBERSECURITY_COMPLIANCE)),
        intellectual_property=IntellectualPropertyAssessment(
            summary=clean_text(sections.ip_summary),
            finding_ids=ids(C.IP_OWNERSHIP, C.IP_DEFENSIBILITY, C.THIRD_PARTY_DEPENDENCY),
            gaps=gaps(C.IP_OWNERSHIP, C.IP_DEFENSIBILITY, C.THIRD_PARTY_DEPENDENCY),
            core_ip=clean_text(sections.ip_core), ownership=clean_text(sections.ip_ownership),
            defensibility=clean_text(sections.ip_defensibility), patents=clean_text(sections.ip_patents),
            trade_secrets=clean_text(sections.ip_trade_secrets), software_data=clean_text(sections.ip_software_data),
            third_party=clean_text(sections.ip_third_party)),
        ai_data=AIDataAssessment(
            applicable=sections.ai_applicable, summary=clean_text(sections.ai_summary),
            finding_ids=[f.id for f in findings if f.topic == Topic.AI_DATA],
            proprietary_assessment=clean_text(sections.ai_proprietary_assessment)),
        scalability=ScalabilityAssessment(
            summary=clean_text(sections.scalability_summary), finding_ids=ids(C.SCALABILITY), gaps=gaps(C.SCALABILITY),
            economic_risks=[shorten(r, 160) for r in sections.economic_risks][:8]),
        dependencies=dependencies,
        flags=list(flags.values()),
        proposed_scores={C(s.category): (int(s.score), shorten(s.rationale, 300)) for s in output.scores},
        questions=[DiligenceQuestion(priority=1, question=shorten(q.question, 240), audience=shorten(q.audience, 40),
                                     rationale=shorten(q.rationale, 200), category=C(q.category))
                   for q in output.questions],
        assumptions=list(STANDARD_ASSUMPTIONS) + [
            Assumption(statement=shorten(a.statement, 240), classification=Classification(a.classification),
                       basis=shorten(a.basis, 160)) for a in output.assumptions],
        missing=missing,
        conclusion_summary=None,
        exit_summary=clean_text(sections.exit_summary) if sections.exit_summary else None,
        warnings=warnings,
    )
    conclusion, removed = index.scrub(sections.conclusion_summary)
    removed_total += removed
    draft.conclusion_summary = shorten(conclusion, 520) if conclusion != NOT_PROVIDED else None
    if removed_total:
        warnings.append(f"Removed {removed_total} narrative sentence(s) asserting subjects absent from the deck.")
    draft.grounding_rejections = rejections + removed_total
    if rejections:
        log.info("Grounding adjusted %d model claims", rejections)
    return draft
