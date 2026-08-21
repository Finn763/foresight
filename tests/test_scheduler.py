from datetime import datetime

from predictor.scheduler import build_weekly_questions


def test_builds_short_horizon_questions():
    qs = build_weekly_questions(week=datetime(2026, 9, 1))
    assert len(qs) >= 4
    assert all((q["closes_at"] - datetime(2026, 9, 1)).days <= 31 for q in qs)
    assert all(q["is_public"] for q in qs)
