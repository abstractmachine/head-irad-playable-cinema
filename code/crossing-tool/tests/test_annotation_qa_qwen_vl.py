import pytest
import json
from types import SimpleNamespace

from services.annotation_qa_qwen_vl import LocalReranker, PREDICATES, cache_key, compatible_control, predicate_for, protected_snapshot, ranking, resolve_model, select_controls


@pytest.mark.parametrize("field", sorted(PREDICATES))
def test_known_field_routes(field):
    result = predicate_for(field, "example")
    assert result["field"] == field
    assert '"example"' in result["query"]


@pytest.mark.parametrize("field", ["action", "setting"])
def test_non_object_semantics(field):
    result = predicate_for(field, "camp")
    assert "object" not in result["query"]
    assert result["input_policy"].startswith("frame_primary")


@pytest.mark.parametrize("field", ["unknown", "dress", "motif"])
def test_undocumented_fields_require_review(field):
    with pytest.raises(ValueError, match="human review"):
        predicate_for(field, "example")


def test_localized_fields():
    for field in ("objects", "animals", "humans"):
        assert predicate_for(field, "example")["input_policy"] == "crop_and_frame"


def test_controls_same_field_deterministic_unique():
    vocabulary = {"objects": ["lamp", "oil lamp", "table", "chair", "rifle", "sign"], "action": ["riding"]}
    controls = select_controls("objects", "lamp", vocabulary)
    assert controls[0] == "lamp"
    assert len(controls) == len(set(controls)) == 5
    assert set(controls).issubset(vocabulary["objects"])
    assert controls == select_controls("objects", "lamp", vocabulary)


@pytest.mark.parametrize("field,label", [("animals", "wagons"), ("wearing", "American flag"), ("action", "head"), ("objects", "yellow object")])
def test_controls_reject_known_incompatible_or_uninformative(field, label):
    assert not compatible_control(field, label)


@pytest.mark.parametrize("field,label", [("animals", "horses"), ("wearing", "holster"), ("wearing", "brown hat"), ("action", "holding head"), ("setting", "camp")])
def test_controls_keep_compatible_propositions(field, label):
    assert compatible_control(field, label)


def test_cache_identity_changes():
    predicate = predicate_for("objects", "lamp")
    original = cache_key({"revision": "one"}, predicate, "lamp", "hash")
    assert original == cache_key({"revision": "one"}, predicate, "lamp", "hash")
    assert original != cache_key({"revision": "two"}, predicate, "lamp", "hash")
    assert original != cache_key({"revision": "one"}, predicate, "lamp", "other")


def test_resolve_only_official_local_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_model(tmp_path)
    model = tmp_path / "models/Qwen3-VL-Reranker-2B"
    (model / "scripts").mkdir(parents=True)
    (model / "model.safetensors").touch()
    (model / "scripts/qwen3_vl_reranker.py").touch()
    (model / "config.json").write_text(json.dumps({"model_type": "qwen3_vl", "text_config": {"num_hidden_layers": 28}}))
    assert resolve_model(tmp_path) == model
    (model / "config.json").write_text('{}')
    with pytest.raises(ValueError):
        resolve_model(tmp_path)


def test_official_loader_bf16_local_only(tmp_path, monkeypatch):
    import torch
    import services.annotation_qa_qwen_vl as qa

    captured = {}
    reference = SimpleNamespace(
        model=SimpleNamespace(parameters=lambda: []),
        score_linear=SimpleNamespace(register_forward_hook=lambda callback: None),
    )
    def constructor(**kwargs):
        captured.update(kwargs)
        return reference
    loader = SimpleNamespace(exec_module=lambda module: setattr(module, "Qwen3VLReranker", constructor))
    monkeypatch.setattr(qa.importlib.util, "spec_from_file_location", lambda *args: SimpleNamespace(loader=loader))
    monkeypatch.setattr(qa.importlib.util, "module_from_spec", lambda spec: SimpleNamespace())
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "synchronize", lambda: None)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    LocalReranker(tmp_path)
    assert captured["model_name_or_path"] == str(tmp_path)
    assert captured["local_files_only"] is True
    assert captured["torch_dtype"] == torch.bfloat16
    assert captured["attn_implementation"] == "sdpa"
    assert qa.os.environ["HF_HUB_OFFLINE"] == "1"
    assert qa.os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_no_cuda_no_silent_substitute(tmp_path, monkeypatch):
    import torch
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="CUDA"):
        LocalReranker(tmp_path)


@pytest.mark.parametrize("score,raw", [(float('nan'), 0), (1.1, 0), (0.5, float('inf'))])
def test_wrapper_rejects_invalid_scores(tmp_path, monkeypatch, score, raw):
    import torch

    image = tmp_path / "image.png"
    image.touch()
    wrapper = object.__new__(LocalReranker)
    wrapper.raw_scores = [raw]
    class Inputs(dict):
        def to(self, device):
            return self
    wrapper.reference = SimpleNamespace(
        model=SimpleNamespace(config=SimpleNamespace(image_token_id=2), device="cpu"),
        format_mm_instruction=lambda *args, **kwargs: [],
        tokenize=lambda pairs: Inputs(input_ids=torch.tensor([[2]])),
        compute_scores=lambda inputs: [score],
    )
    monkeypatch.setattr(torch.cuda, "synchronize", lambda: None)
    monkeypatch.setattr(torch.cuda, "reset_peak_memory_stats", lambda: None)
    monkeypatch.setattr(torch.cuda, "max_memory_allocated", lambda: 0)
    with pytest.raises(RuntimeError, match="score"):
        wrapper.score([{"image": str(image), "predicate": predicate_for("objects", "lamp")}])


def test_ranking_diagnostic_no_relabeling():
    scores = [{"label": "lamp", "raw_score": 0, "normalized_score": .5}, {"label": "table", "raw_score": 1, "normalized_score": .73}]
    before = json.dumps(scores)
    result = ranking(scores, "lamp")
    assert result["proposed_label_rank"] == 2
    assert result["strongest_competitor"] == "table"
    assert result["margin"] < 0
    assert json.dumps(scores) == before


@pytest.mark.parametrize("image_present", [True, False])
def test_transformers_token_type_adapter_and_no_text_fallback(tmp_path, monkeypatch, image_present):
    import torch

    image = tmp_path / "image.png"
    image.touch()
    wrapper = object.__new__(LocalReranker)
    wrapper.raw_scores = [0.0]
    class Inputs(dict):
        def to(self, device):
            return self
    tokens = [[0, 2, 3]] if image_present else [[0, 3, 3]]
    def compute(inputs):
        assert isinstance(inputs["mm_token_type_ids"], torch.Tensor)
        assert inputs["mm_token_type_ids"].tolist() == [[0, 1, 0]]
        return [.5]
    wrapper.reference = SimpleNamespace(
        model=SimpleNamespace(config=SimpleNamespace(image_token_id=2), device="cpu"),
        processor=SimpleNamespace(create_mm_token_type_ids=lambda ids: [[0, 1, 0]]),
        format_mm_instruction=lambda *args, **kwargs: [],
        tokenize=lambda pairs: Inputs(input_ids=torch.tensor(tokens), mm_token_type_ids=[[0, 1, 0]]),
        compute_scores=compute,
    )
    monkeypatch.setattr(torch.cuda, "synchronize", lambda: None)
    monkeypatch.setattr(torch.cuda, "reset_peak_memory_stats", lambda: None)
    monkeypatch.setattr(torch.cuda, "max_memory_allocated", lambda: 0)
    request = {"image": str(image), "predicate": predicate_for("objects", "lamp")}
    if image_present:
        assert wrapper.score([request])[0]["normalized_score"] == .5
        assert wrapper.score([request])[0]["raw_score"] == 0.0
    else:
        with pytest.raises(RuntimeError, match="lost image"):
            wrapper.score([request])


def test_safety_snapshot_read_only_and_detects_changes(tmp_path):
    record = tmp_path / "data/silhouettes/catalog/object.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"png": "object.png", "assignment": {"state": "active"}}))
    (record.parent / "object.png").write_bytes(b"image")
    paths = []
    for relative in ("data/annotations/annotation.json", "data/indexes/illustration/movie-silhouettes.sqlite3", "data/engravings/catalog/engraving.json", "outputs/tests/engraving-benchmark/review.json"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
        paths.append(path)
    cases = [{"source": {"record_path": str(record.relative_to(tmp_path))}}]
    baseline = protected_snapshot(tmp_path, cases)
    assert protected_snapshot(tmp_path, cases) == baseline
    assert json.loads(record.read_text())["assignment"]["state"] == "active"
    for path in paths:
        path.write_text('{"changed": true}')
        assert protected_snapshot(tmp_path, cases) != baseline
        path.write_text('{}')