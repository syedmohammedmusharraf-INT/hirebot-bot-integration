"""Unit tests for the procedural humanized-mouse-movement generator (no X
server needed -- pure math, see meet_voice_bot/google_meet_bot_adapter/
mocap_manager.py)."""

from meet_voice_bot.google_meet_bot_adapter.mocap_manager import MocapManager


def test_initial_mouse_position_within_frame():
    m = MocapManager(video_frame_size=(1920, 1080))
    x, y = m.get_initial_mouse_position()
    assert 0 <= x < 1920
    assert 0 <= y < 1080


def test_sequence_lands_inside_target_rect():
    m = MocapManager(video_frame_size=(1920, 1080))
    current_x, current_y = 100, 100
    rect = (800, 500, 900, 540)
    seq = m.find_random_sequence_landing_in_rect(current_x, current_y, *rect)
    assert seq is not None
    final_x = current_x + seq.total_dx
    final_y = current_y + seq.total_dy
    left, top, right, bottom = rect
    assert left <= final_x <= right
    assert top <= final_y <= bottom


def test_sequence_has_click_timing():
    m = MocapManager(video_frame_size=(1920, 1080))
    seq = m.find_random_sequence_landing_in_rect(0, 0, 500, 500, 560, 540)
    assert seq.click_down_dt > 0
    assert seq.click_up_dt > 0


def test_zero_distance_sequence_has_no_movements():
    m = MocapManager(video_frame_size=(1920, 1080))
    seq = m.find_random_sequence_landing_in_rect(500, 500, 499, 499, 501, 501)
    assert seq.movements == []
