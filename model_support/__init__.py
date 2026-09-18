"""Dependency-free model/assistant validation shared by meet_voice_bot (control) and meet-agent (worker).

Both processes import this package so they can never disagree about which
(mode, provider, model) combinations are legal. Stdlib only -- no livekit,
selenium, or provider SDKs may be imported here (plan section 4.2).
"""
