from scripts.palette_lab import e8_auditor as AUD


def _spec():
    return {
        "relation_id": "R1",
        "side_a": {
            "concept": "the red-coated man",
            "referents": [{"phrase": "man in red coat", "relation_to_concept": "whole",
                           "covers": "the figure"}],
            "measurable_expectations": "a localized warm region",
            "interpretive_content": "human agency",
        },
        "side_b": {
            "concept": "the snowy environment",
            "referents": [
                {"phrase": "snow-covered ground", "relation_to_concept": "broader_field",
                 "covers": "the open field"},
                {"phrase": "snowy forest and the horse", "relation_to_concept": "whole",
                 "covers": "everything behind him"},
            ],
            "measurable_expectations": "a large pale field",
            "interpretive_content": "cold and winter",
        },
        "relation": {
            "relational_term": "isolated against",
            "measurable_proxies": "neighbourhood and colour difference",
            "interpretive_content": "psychological isolation",
        },
        "uncertainty": "whether the snow phrase returns the forest floor",
    }


def _critic_relation():
    return {
        "id": "R1",
        "side_a": "The man in the red coat, as a dominant presence",
        "side_b": "The snowy forest and the horse, as a muted environment",
        "figure_side": "side_a",
        "why_these_are_one_relation": "both reach the same claim",
        "curatorial_significance": "the frame's principal opposition",
        "orientation_reasoning": "the man acts",
    }


def _referent_audit(phrase, status="atomic", targets=None, side="side_b"):
    return {
        "side": side,
        "original_referent": phrase,
        "referent_status": status,
        "operationalization": [phrase] if targets is None else list(targets),
        "limits": "locating it leaves the perceptual grouping unsettled",
    }


def _evidence_audit(status="direct", observation="measure both colours"):
    return {
        "claim_component": "the figure is chromatically opposed to the field",
        "proposed_observation": observation,
        "evidence_status": status,
        "what_it_could_support": "a chromatic difference between the regions",
        "what_it_cannot_support": "the narrative weight of the opposition",
    }


def _audit(**overrides):
    audit = {
        "relation_id": "R1",
        "curatorial_relation_preserved": "the red-coated man against the snowy environment",
        "referent_audits": [
            _referent_audit("man in red coat", side="side_a"),
            _referent_audit("snow-covered ground", "distributed_field"),
            _referent_audit("snowy forest and the horse", "compound_needs_decomposition",
                            ["snowy forest", "horse"]),
        ],
        "evidence_audits": [_evidence_audit()],
        "audit_summary": "colour is testable; cold and winter are not",
        "uncertainty": "whether containment really shows isolation",
    }
    audit.update(overrides)
    return audit


class TestRenderProposal:
    def test_relation_and_specification_are_both_passed_through(self):
        text = AUD.render_proposal(_critic_relation(), _spec())
        assert "THE CURATORIAL RELATION" in text
        assert "THE PROPOSED OPERATIONALIZATION" in text
        assert "man in red coat" in text
        assert "isolated against" in text

    def test_what_the_spatializer_called_unmeasurable_is_shown(self):
        text = AUD.render_proposal(_critic_relation(), _spec())
        assert "cold and winter" in text
        assert "psychological isolation" in text


class TestShape:
    def test_a_well_formed_audit_has_no_problems(self):
        assert AUD.shape_problems(_audit(), "R1", _spec()) == []

    def test_unparsed_responses_are_reported(self):
        assert AUD.shape_problems(None, "R1", _spec()) == [
            "response did not parse as a JSON object"
        ]

    def test_relation_id_must_match_the_source(self):
        problems = AUD.shape_problems(_audit(relation_id="R2"), "R1", _spec())
        assert any("does not match" in problem for problem in problems)

    def test_the_preserved_relation_and_summary_are_required(self):
        for field in ("curatorial_relation_preserved", "audit_summary", "uncertainty"):
            problems = AUD.shape_problems(_audit(**{field: "  "}), "R1", _spec())
            assert any(f"{field} is empty" in problem for problem in problems)

    def test_numbers_are_reported(self):
        audit = _audit(evidence_audits=[_evidence_audit()])
        audit["evidence_audits"][0]["what_it_could_support"] = "a 40 percent difference"
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("contains a number" in problem for problem in problems)

    def test_relation_ids_in_prose_are_not_mistaken_for_numbers(self):
        audit = _audit()
        audit["evidence_audits"][0]["what_it_could_support"] = "the same as in R1"
        assert AUD.shape_problems(audit, "R1", _spec()) == []


class TestReferentAudit:
    def test_every_spatializer_referent_must_be_audited(self):
        audit = _audit(referent_audits=[_referent_audit("man in red coat", side="side_a")])
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("never audited" in problem for problem in problems)

    def test_auditing_one_referent_twice_is_reported(self):
        audit = _audit()
        audit["referent_audits"].append(_referent_audit("man in red coat", side="side_a"))
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("audited more than once" in problem for problem in problems)

    def test_referent_status_must_come_from_the_vocabulary(self):
        audit = _audit()
        audit["referent_audits"][0]["referent_status"] = "probably fine"
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("referent_status" in problem and "not one of" in problem
                   for problem in problems)

    def test_every_status_in_the_vocabulary_is_accepted(self):
        for status in ("atomic", "distributed_field"):
            audit = _audit()
            audit["referent_audits"][0]["referent_status"] = status
            assert AUD.shape_problems(audit, "R1", _spec()) == []

    def test_a_compound_referent_must_actually_be_decomposed(self):
        audit = _audit()
        audit["referent_audits"][2]["operationalization"] = ["snowy forest"]
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("compound but was not decomposed" in problem for problem in problems)

    def test_an_unspatializable_referent_proposes_nothing(self):
        audit = _audit()
        audit["referent_audits"][2]["referent_status"] = "not_spatializable"
        audit["referent_audits"][2]["operationalization"] = ["snowy forest"]
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("not spatializable yet proposes targets" in problem
                   for problem in problems)

    def test_an_unspatializable_referent_with_no_targets_is_valid(self):
        audit = _audit()
        audit["referent_audits"][2]["referent_status"] = "not_spatializable"
        audit["referent_audits"][2]["operationalization"] = []
        assert AUD.shape_problems(audit, "R1", _spec()) == []

    def test_empty_decomposition_entries_are_reported(self):
        audit = _audit()
        audit["referent_audits"][2]["operationalization"] = ["snowy forest", "  "]
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("empty operationalization entry" in problem for problem in problems)

    def test_limits_are_required(self):
        audit = _audit()
        audit["referent_audits"][0]["limits"] = ""
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("has no limits" in problem for problem in problems)


class TestEvidenceAudit:
    def test_evidence_status_must_come_from_the_vocabulary(self):
        audit = _audit(evidence_audits=[_evidence_audit(status="probably")])
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("evidence_status" in problem and "not one of" in problem
                   for problem in problems)

    def test_a_direct_or_proxy_claim_needs_a_proposed_observation(self):
        for status in AUD.EVIDENCE_NEEDING_OBSERVATION:
            audit = _audit(evidence_audits=[_evidence_audit(status, observation=None)])
            problems = AUD.shape_problems(audit, "R1", _spec())
            assert any("without a proposed observation" in problem
                       for problem in problems)

    def test_not_operationalized_may_have_no_observation(self):
        audit = _audit(evidence_audits=[{
            "claim_component": "the snow communicates cold",
            "proposed_observation": None,
            "evidence_status": "not_operationalized",
            "what_it_could_support": "",
            "what_it_cannot_support": "the world-knowledge claim itself",
        }])
        assert AUD.shape_problems(audit, "R1", _spec()) == []

    def test_descriptive_only_may_have_no_observation(self):
        audit = _audit(evidence_audits=[
            _evidence_audit("descriptive_only", observation=None)
        ])
        assert AUD.shape_problems(audit, "R1", _spec()) == []

    def test_what_it_cannot_support_is_always_required(self):
        audit = _audit(evidence_audits=[_evidence_audit()])
        audit["evidence_audits"][0]["what_it_cannot_support"] = ""
        problems = AUD.shape_problems(audit, "R1", _spec())
        assert any("has no what_it_cannot_support" in problem for problem in problems)


class TestSummary:
    def test_statuses_are_tallied(self):
        summary = AUD.summarise(_audit(), _spec())
        assert summary["spatializer_referents"] == 3
        assert summary["referents_audited"] == 3
        assert summary["referent_status"] == {
            "atomic": 1, "distributed_field": 1, "compound_needs_decomposition": 1
        }
        assert summary["decomposed_targets"] == 2

    def test_components_without_a_test_are_counted(self):
        audit = _audit(evidence_audits=[
            _evidence_audit(),
            {"claim_component": "cold", "proposed_observation": None,
             "evidence_status": "not_operationalized", "what_it_could_support": "",
             "what_it_cannot_support": "the semantic claim"},
        ])
        summary = AUD.summarise(audit, _spec())
        assert summary["claim_components"] == 2
        assert summary["components_without_a_test"] == 1
        assert summary["evidence_status"]["direct"] == 1

    def test_an_unparsed_response_summarises_without_raising(self):
        summary = AUD.summarise(None, _spec())
        assert summary["referents_audited"] == 0
        assert summary["spatializer_referents"] == 3
