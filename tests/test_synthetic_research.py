import hashlib
import json

import pytest

from scripts import synthetic_research
from scripts.synthetic_experiments import expected_answers, payloads


def test_research_material_is_reproducible_and_expected_answers_are_separate(tmp_path, capsys):
    assert synthetic_research.main(["--output", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["documents"] == 3
    before = {path.relative_to(tmp_path): hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()}
    assert synthetic_research.generate(tmp_path) == 5
    after = {path.relative_to(tmp_path): hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
    assert set(path.name for path in (tmp_path / "documents").iterdir()) == set(synthetic_research.DOCUMENTS)
    for path in (tmp_path / "documents").iterdir():
        assert "Synthetic" in path.read_text().splitlines()[0] and "合成资料" in path.read_text()
    assert json.loads((tmp_path / "experiment_inputs/expected.json").read_text()) == expected_answers()
    for relative, content in payloads().items():
        assert (tmp_path / "experiment_inputs" / relative).read_bytes() == content.encode()
    changed = tmp_path / "documents/attention.md"
    changed.write_text("user edits")
    with pytest.raises(FileExistsError):
        synthetic_research.generate(tmp_path)
    assert changed.read_text() == "user edits"
