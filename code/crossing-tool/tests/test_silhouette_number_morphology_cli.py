from __future__ import annotations

import cli


class _Args:
    silhouette_action = "morphology-audit"
    source_audit_dir = None
    output_dir = None


def test_index_silhouette_morphology_audit_dispatch(monkeypatch, tmp_path, capsys):
    calls = {}

    def fake_audit(project_path, *, source_audit_dir=None, output_dir=None):
        calls["project_path"] = project_path
        calls["source_audit_dir"] = source_audit_dir
        calls["output_dir"] = output_dir
        return {
            "classification": {
                "total_existing_questionable_number_records_examined": 1,
                "number_variant": 1,
                "morphology_unresolved": 0,
                "number_semantically_ambiguous": 0,
                "not_number_variant": 0,
            },
            "artifacts": {
                "report_md": str(tmp_path / "outputs" / "tests" / "silhouette-number-morphology-audit" / "report.md"),
                "report_json": str(tmp_path / "outputs" / "tests" / "silhouette-number-morphology-audit" / "report.json"),
                "morphology_records_csv": str(tmp_path / "outputs" / "tests" / "silhouette-number-morphology-audit" / "morphology_records.csv"),
                "morphology_labels_csv": str(tmp_path / "outputs" / "tests" / "silhouette-number-morphology-audit" / "morphology_labels.csv"),
            },
        }

    monkeypatch.setattr(cli, "_silhouette_number_morphology_audit", lambda args: fake_audit(str(tmp_path), source_audit_dir=args.source_audit_dir, output_dir=args.output_dir))

    cli._index_silhouette(_Args())
    assert calls == {
        "project_path": str(tmp_path),
        "source_audit_dir": None,
        "output_dir": None,
    }
