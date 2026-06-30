import json

from engine.calibration import classify_swing_score, load_swing_calibration


def _artifact(path):
    path.write_text(
        json.dumps(
            {
                "model": "swing_score",
                "buckets": [
                    {
                        "label": "70-80",
                        "low": 70.0,
                        "high": 80.0,
                        "status": "approved",
                        "reason": "14d median +6.1%, 58% positive in Apr-May",
                    },
                    {
                        "label": "80+",
                        "low": 80.0,
                        "high": None,
                        "status": "blocked",
                        "reason": "14d median -12.1%, 46% positive in Apr-May",
                    },
                ],
            }
        )
    )


def test_load_swing_calibration_reads_bucket_artifact(tmp_path):
    path = tmp_path / "swing.json"
    _artifact(path)

    calibration = load_swing_calibration(path)

    assert calibration.model == "swing_score"
    assert calibration.classify(76.0).status == "approved"
    assert calibration.classify(82.0).status == "blocked"
    assert calibration.classify(65.0).status == "unclassified"


def test_classify_swing_score_uses_default_artifact():
    decision = classify_swing_score(82.0)

    assert decision.status == "blocked"
    assert decision.label == "80+"
    assert "14d median -12.1%" in decision.reason
