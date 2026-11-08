"""
Offline tests: fake LLM, in-memory sqlite, stubbed tools. Run:  python -m pytest -q   (or python tests/test_core.py)
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import appstate, config, grounding, orchestrator, safety, tools  # noqa: E402
from app.tools.external import parse_pubmed_xml  # noqa: E402
from ingest.directories import parse_json_array  # noqa: E402

config.SAFETY_LLM_CHECK = False


def text_block(t):
    return NS(type="text", text=t)


class FakeLLM:
    """Scripted responses for the main model; fast-model calls get a canned summary."""
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if kw["model"] == config.MODEL_FAST:
            return NS(content=[text_block("summary of earlier turns")], stop_reason="end_turn")
        return self.script.pop(0)


def stub_tool(sources, results="ok"):
    return lambda args, ctx: {"results": results, "sources": sources}


def test_store_and_recall():
    s = appstate.Store("sqlite:///:memory:")
    s.update_profile("u1", {"zip": "90024", "life_stage": "pregnant", "ignored_field": 1})
    assert s.get_profile("u1") == {"zip": "90024", "life_stage": "pregnant"}
    t = s.create_thread("u1", "medications", "tacrolimus")
    mid = s.add_message(t, "user", "is tacrolimus safe in pregnancy")
    s.add_sources(mid, "u1", "medications", [{"source_id": "LABEL:x", "title": "FDA label: tacrolimus", "url": "u"}])
    s.save_resource("u1", "resources", "American Kidney Fund grants", "https://www.kidneyfund.org")
    kinds = {r["kind"] for r in s.recall("u1", "tacrolimus")}
    assert kinds == {"previously_found", "past_question"}
    assert s.recall("u1", "kidney fund")[0]["kind"] == "saved"
    assert s.recall("someone_else", "tacrolimus") == []


def test_grounding_strips_unknown_ids():
    srcs = [{"source_id": "PMID:111", "title": "a", "url": None}]
    out, rep = grounding.check("Fact one [PMID:111]. Fact two [PMID:999]. A list [1, 2].", srcs, "symptoms")
    assert "[PMID:111]" in out and "999" not in out and "[1, 2]" in out
    assert rep["removed"] == ["PMID:999"] and rep["notices"] == [grounding.REMOVED_NOTICE]
    assert "couldn't be verified" not in out            # notices are structured, not appended to text
    _, rep2 = grounding.check("No sources here.", [], "medications")
    assert rep2["notices"] == [grounding.NO_EVIDENCE_NOTICE]
    _, rep3 = grounding.check("Clinic list.", [], "doctors")
    assert rep3["notices"] == []


def test_safety_rules():
    assert safety.rule_check("I'm 32 weeks pregnant with a severe headache and blurry vision")
    assert not safety.rule_check("What are warning signs of preeclampsia in pregnancy, like severe headache?")
    assert safety.rule_check("I want to end my life") == safety.CRISIS


def test_orchestrator_tool_loop_grounding_and_memory():
    tools.TOOLS["search_documents"] = (tools.TOOLS["search_documents"][0], stub_tool(
        [{"source_id": "DOC:kdigo2024", "title": "KDIGO 2024 CKD guideline", "url": "https://kdigo.org"}]))
    llm = FakeLLM([
        NS(stop_reason="tool_use", content=[
            text_block("Let me check."),
            NS(type="tool_use", id="t1", name="search_documents", input={"query": "fatigue CKD women"})]),
        NS(stop_reason="end_turn", content=[text_block(
            "Fatigue is common in CKD, often from anemia [DOC:kdigo2024]. Also [PMID:424242].")]),
        NS(stop_reason="end_turn", content=[text_block(   # revision after evaluator feedback
            "Fatigue is common in CKD, often from anemia [DOC:kdigo2024].")]),
    ])
    store = appstate.Store("sqlite:///:memory:")
    store.update_profile("u1", {"ckd_stage": "3b"})
    out = orchestrator.handle_chat(store, llm, "u1", "symptoms", "Why am I so tired all the time?")
    assert "[DOC:kdigo2024]" in out["answer"] and "424242" not in out["answer"]
    assert out["revision"] == {"attempted": True, "accepted": True, "reason": "unverified_citations"}
    assert out["removed_citations"] == []                       # the revised draft needed no stripping
    feedback = llm.calls[2]["messages"][-1]["content"]
    assert "PMID:424242" in feedback                            # evaluator told the agent exactly what failed
    assert any(m["role"] == "user" and isinstance(m["content"], list) and isinstance(m["content"][0], dict)
               and m["content"][0].get("type") == "tool_result"
               for m in llm.calls[2]["messages"])                  # revision still sees the evidence
    assert {g["name"]: g["status"] for g in out["guardrails"]}["self_correction"] == "revised"
    assert [c["tool"] for c in out["tool_calls"]] == ["search_documents"]
    # system prompt: static part cached, profile injected after the cache breakpoint
    sys_blocks = llm.calls[0]["system"]
    assert "cache_control" in sys_blocks[0] and "ckd_stage=3b" in sys_blocks[1]["text"]
    # memory: the source found here is recallable from another tab later
    assert store.recall("u1", "KDIGO")[0]["source_id"] == "DOC:kdigo2024"
    assert len(store.get_messages(out["thread_id"])) == 2
    saved = store.get_messages(out["thread_id"], with_meta=True)[1]     # reopened chat keeps citations
    assert saved["citations"][0]["source_id"] == "DOC:kdigo2024" and saved["guardrails"]


def test_tool_budget_forces_answer():
    loop = NS(stop_reason="tool_use", content=[NS(type="tool_use", id="x", name="recall_memory",
                                                  input={"query": "a"})])
    final = NS(stop_reason="end_turn", content=[text_block("Here's what I have.")])
    llm = FakeLLM([loop] * config.MAX_TOOL_ROUNDS + [final])
    store = appstate.Store("sqlite:///:memory:")
    out = orchestrator.handle_chat(store, llm, "u1", "doctors", "find my doctor again")
    assert llm.calls[-1].get("tool_choice") == {"type": "none"}
    assert out["answer"].startswith("Here's what I have.")


def test_urgent_skips_agent():
    llm = FakeLLM([])
    store = appstate.Store("sqlite:///:memory:")
    out = orchestrator.handle_chat(store, llm, "u1", "symptoms",
                                   "I'm pregnant and I have sudden swelling of my face and seeing spots")
    assert out["urgent"] and llm.calls == []


def test_summarization_kicks_in():
    store = appstate.Store("sqlite:///:memory:")
    tid = store.create_thread("u1", "symptoms")
    for i in range(config.SUMMARIZE_AFTER + 2):
        store.add_message(tid, "user" if i % 2 == 0 else "assistant", f"m{i}")
    orchestrator.maybe_summarize(store, FakeLLM([]), store.get_thread(tid))
    t = store.get_thread(tid)
    assert t["summary"] == "summary of earlier turns"
    assert t["summarized_upto"] == config.SUMMARIZE_AFTER + 2 - config.HISTORY_TURNS


def test_parsers():
    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
      <Journal><Title>J Nephrol</Title><JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue></Journal>
      <ArticleTitle>Pregnancy in <i>CKD</i></ArticleTitle><Abstract>
      <AbstractText Label="BACKGROUND">Few data.</AbstractText><AbstractText Label="RESULTS">Risk rose.</AbstractText>
      </Abstract><PublicationTypeList><PublicationType>Review</PublicationType></PublicationTypeList>
      </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    a = parse_pubmed_xml(xml)[0]
    assert a["pmid"] == "123" and a["title"] == "Pregnancy in CKD" and a["year"] == "2023"
    assert a["abstract"] == "BACKGROUND: Few data. RESULTS: Risk rose." and a["pub_types"] == ["Review"]
    assert parse_json_array('Sure! [{"last_name": "Lee"}] done') == [{"last_name": "Lee"}]
    assert parse_json_array("no json") == []


def test_presentation_citations_and_guardrails():
    from app import presentation
    srcs = [{"source_id": "LABEL:abc", "title": "FDA label: lisinopril", "url": "u1"},
            {"source_id": "DOC:acog", "title": "ACOG guidance", "url": "u2", "collection": "research",
             "pub_type": "guideline"},
            {"source_id": "PMID:5", "title": "Unused paper", "url": "u3"}]
    answer = "ACE inhibitors carry a fetal warning [LABEL:abc]. Guidance agrees [DOC:acog, LABEL:abc]. See [note]."
    _, rep = grounding.check(answer, srcs, "medications")
    out = presentation.build("medications", answer, rep, srcs,
                             [{"tool": "drug_label"}, {"tool": "faers_summary"}], grounding.CLINICAL_TABS)
    assert out["answer_display"] == "ACE inhibitors carry a fetal warning [1]. Guidance agrees [2, 1]. See [note]."
    assert [(c["n"], c["source_id"], c["kind"]) for c in out["citations"]] == [
        (1, "LABEL:abc", "FDA drug label"), (2, "DOC:acog", "Research / guideline (guideline)")]
    assert presentation.TOOL_NOTICES["faers_summary"] in out["notices"]
    assert presentation.EDUCATION_NOTICE in out["notices"]
    status = {g["name"]: g["status"] for g in out["guardrails"]}
    assert status == {"safety_check": "passed", "citation_check": "passed",
                      "dosing_check": "passed", "tool_caveats": "applied"}
    flagged = presentation.build("medications", "Take 200 mg twice daily.", {"cited": [], "removed": [],
                                 "notices": []}, [], [], grounding.CLINICAL_TABS)
    g = {x["name"]: x["status"] for x in flagged["guardrails"]}
    assert g["dosing_check"] == "flagged" and g["citation_check"] == "warning"
    assert presentation.DOSING_NOTICE in flagged["notices"]


def test_response_shape_end_to_end():
    out = orchestrator.handle_chat(appstate.Store("sqlite:///:memory:"), FakeLLM([]), "u1", "symptoms",
                                   "I'm pregnant and seeing spots with a severe headache")
    assert out["guardrails"][0] == {**out["guardrails"][0], "name": "safety_check", "status": "triggered"}
    assert out["citations"] == [] and out["answer_display"] == out["answer"]


def _answer(text):
    return [NS(stop_reason="end_turn", content=[text_block(text)])]


def test_pin_board_and_retention():
    from datetime import datetime, timedelta, timezone
    store = appstate.Store("sqlite:///:memory:")
    tools.TOOLS["search_documents"] = (tools.TOOLS["search_documents"][0], stub_tool(
        [{"source_id": "DOC:acog", "title": "ACOG guidance", "url": "u"}]))
    llm = FakeLLM([NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="search_documents",
                                                          input={"query": "x"})]),
                   NS(stop_reason="end_turn", content=[text_block("CKD raises preeclampsia risk [DOC:acog].")])])
    out = orchestrator.handle_chat(store, llm, "u1", "symptoms", "Does kidney disease raise preeclampsia risk?")

    pid = store.pin_message("u1", out["message_id"], note="ask Dr. at 20 wk visit")
    assert store.pin_message("someone_else", out["message_id"]) is None          # can't pin others' answers
    b = store.board("u1")
    pin = b["pinned_answers"][0]
    assert pin["question"] == "Does kidney disease raise preeclampsia risk?"
    assert pin["snapshot"]["citations"][0]["source_id"] == "DOC:acog"
    assert pin["snapshot"]["answer_display"] == "CKD raises preeclampsia risk [1]."

    # chat expires after the retention window; the pin survives and is still findable
    future = datetime.now(timezone.utc) + timedelta(days=config.RETENTION_DAYS + 1)
    assert store.purge_expired(now=future) == 1
    assert store.get_thread(out["thread_id"]) is None                            # chat + messages gone
    assert {r["kind"] for r in store.recall("u1", "preeclampsia")} == {"pinned"}  # only the pin remains
    assert store.board("u1")["pinned_answers"][0]["pin_id"] == pid
    assert store.recall("u1", "preeclampsia")[0]["kind"] == "pinned"

    # a chat used recently is not purged
    t2 = store.create_thread("u2", "symptoms")
    assert store.purge_expired() == 0 and store.get_thread(t2)

    # full deletion
    store.delete_user_data("u1")
    assert store.board("u1") == {"pinned_answers": [], "saved_resources": []} and store.get_profile("u1") == {}


def test_related_history_flags_repeat_questions():
    config.MAX_REVISIONS = 0          # not what this test is about; covered in test_revision_*
    store = appstate.Store("sqlite:///:memory:")
    first = orchestrator.handle_chat(store, FakeLLM(_answer("Answer one.")), "u1", "medications",
                                     "Is tacrolimus safe during pregnancy?")
    assert first["related_history"] == []
    second = orchestrator.handle_chat(store, FakeLLM(_answer("Answer two.")), "u1", "medications",
                                      "tacrolimus safety in pregnancy")
    assert second["related_history"][0]["thread_id"] == first["thread_id"]
    other = orchestrator.handle_chat(store, FakeLLM(_answer("Answer three.")), "u1", "doctors",
                                     "find a nephrologist near me")
    assert other["related_history"] == []
    config.MAX_REVISIONS = 1


def test_revision_rejected_when_not_better():
    store = appstate.Store("sqlite:///:memory:")
    llm = FakeLLM(_answer("Anemia causes fatigue [PMID:1].") + _answer("Still unsourced [PMID:2], [PMID:3]."))
    out = orchestrator.handle_chat(store, llm, "u1", "symptoms", "why am I tired")
    assert out["revision"] == {"attempted": True, "accepted": False, "reason": "unverified_citations"}
    assert "PMID" not in out["answer"] and out["removed_citations"] == ["PMID:1"]   # original kept, cleaned
    assert {g["name"]: g["status"] for g in out["guardrails"]}["self_correction"] == "kept_original"
    assert len(llm.calls) == 2                                   # exactly one retry, never more


def test_revision_for_missing_citations_and_skipped_when_clean():
    tools.TOOLS["search_documents"] = (tools.TOOLS["search_documents"][0], stub_tool(
        [{"source_id": "DOC:nkf", "title": "NKF diet guide", "url": "u"}]))
    llm = FakeLLM(_answer("Limit salt.") + [
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="r1", name="search_documents",
                                               input={"query": "ckd diet"})]),
        NS(stop_reason="end_turn", content=[text_block("Sodium is a common limit in CKD [DOC:nkf].")])])
    out = orchestrator.handle_chat(appstate.Store("sqlite:///:memory:"), llm, "u1", "symptoms", "what should I eat")
    assert out["revision"]["reason"] == "no_citations" and out["revision"]["accepted"]
    assert out["citations"][0]["source_id"] == "DOC:nkf"
    assert any(c.get("revision") for c in out["tool_calls"])    # revision was allowed one tool round
    # doctors tab answers without citations are fine, so no retry
    clean = orchestrator.handle_chat(appstate.Store("sqlite:///:memory:"), FakeLLM(_answer("Here are options.")),
                                     "u1", "doctors", "find an OB")
    assert clean["revision"]["attempted"] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
