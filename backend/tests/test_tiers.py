from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.tiers import classify, effective_interval, is_upload_complete
from app.settings_service import AppSettings

NOW = datetime(2026, 10, 20, tzinfo=UTC)
S = AppSettings()


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def test_active_title_is_level_1():
    assert classify([ago(200), ago(14), ago(7)], False, NOW, S).tier == 1


def test_stops_updating_decays_to_2_then_3():
    assert classify([ago(130), ago(120)], False, NOW, S).tier == 2
    assert classify([ago(410), ago(400)], False, NOW, S).tier == 3


def test_single_late_extra_does_not_wake_dormant_title():
    # last chapter yesterday, but the previous one two years ago
    assert classify([ago(800), ago(730), ago(1)], False, NOW, S).tier == 3


def test_two_close_chapters_after_pause_go_back_to_level_1():
    assert classify([ago(730), ago(30), ago(5)], False, NOW, S).tier == 1


def test_bulk_upload_same_day_counts_as_active():
    assert classify([ago(10)] * 20, False, NOW, S).tier == 1


def test_gap_between_last_two_decides_when_recent():
    # last chapter recent, but the gap before it was 6 months -> level 2
    assert classify([ago(190), ago(10)], False, NOW, S).tier == 2


def test_complete_is_level_3_and_no_chapters_is_level_1():
    info = classify([ago(1), ago(2)], True, NOW, S)
    assert info.tier == 3 and info.complete
    assert classify([], False, NOW, S).tier == 1


def test_future_dates_are_capped():
    assert classify([NOW + timedelta(days=3), ago(1)], False, NOW, S).tier == 1


def test_upload_complete_rules():
    assert is_upload_complete("completed", "104", "10", 104.0, 10.0)
    assert not is_upload_complete("completed", "104", None, 103.0, None)
    assert not is_upload_complete("ongoing", "104", None, 104.0, None)
    assert is_upload_complete("cancelled", None, "5", 30.0, 5.0)
    assert not is_upload_complete("completed", None, None, 50.0, 5.0)


def test_effective_interval_never_shorter_than_custom():
    cfg = SimpleNamespace(interval_minutes=None, update_tier=1)
    assert effective_interval(cfg, S) == S.monitoring_interval_minutes
    cfg.update_tier = 2
    assert effective_interval(cfg, S) == S.tier2_interval_minutes
    cfg.update_tier = 3
    assert effective_interval(cfg, S) == S.tier3_interval_minutes
    cfg.interval_minutes = 20000
    assert effective_interval(cfg, S) == 20000
    off = AppSettings(adaptive_monitoring=False)
    assert effective_interval(cfg, off) == 20000
