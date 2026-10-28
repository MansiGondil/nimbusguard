"""Project success target: >= 80% anomaly-detection accuracy (scored on labelled simulated faults)."""

from scripts.evaluate_detector import evaluate


def test_detection_accuracy_meets_target():
    r = evaluate(episodes=24, seed=11, verbose=False)
    assert r["recall"] >= 0.8, r
    assert r["precision"] >= 0.8, r
    assert r["false_alarms_per_device_day"] < 1.0, r
