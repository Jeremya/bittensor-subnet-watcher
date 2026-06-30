from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

CalibrationStatus = Literal["approved", "caution", "blocked", "unclassified"]

DEFAULT_SWING_CALIBRATION_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "calibration" / "swing_2026-06-30.json"
)


@dataclass(frozen=True)
class CalibrationDecision:
    label: str
    status: CalibrationStatus
    reason: str


@dataclass(frozen=True)
class CalibrationBucket:
    label: str
    low: float | None
    high: float | None
    status: CalibrationStatus
    reason: str

    def contains(self, score: float) -> bool:
        if self.low is not None and score < self.low:
            return False
        if self.high is not None and score >= self.high:
            return False
        return True


@dataclass(frozen=True)
class SwingCalibration:
    model: str
    buckets: tuple[CalibrationBucket, ...]

    def classify(self, score: float | None) -> CalibrationDecision:
        if score is None:
            return CalibrationDecision(
                label="unscored",
                status="unclassified",
                reason="missing swing score",
            )
        for bucket in self.buckets:
            if bucket.contains(score):
                return CalibrationDecision(
                    label=bucket.label,
                    status=bucket.status,
                    reason=bucket.reason,
                )
        return CalibrationDecision(
            label="unclassified",
            status="unclassified",
            reason="score outside configured calibration buckets",
        )


def load_swing_calibration(path: str | Path = DEFAULT_SWING_CALIBRATION_PATH) -> SwingCalibration:
    data = json.loads(Path(path).read_text())
    buckets = tuple(
        CalibrationBucket(
            label=row["label"],
            low=row.get("low"),
            high=row.get("high"),
            status=row["status"],
            reason=row["reason"],
        )
        for row in data.get("buckets", [])
    )
    return SwingCalibration(model=data["model"], buckets=buckets)


@lru_cache(maxsize=1)
def _default_swing_calibration() -> SwingCalibration:
    return load_swing_calibration(DEFAULT_SWING_CALIBRATION_PATH)


def classify_swing_score(score: float | None) -> CalibrationDecision:
    return _default_swing_calibration().classify(score)
