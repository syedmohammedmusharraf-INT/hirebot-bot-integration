"""Chat-vs-reasoning knob gating for OpenAI models (plan section 4.3).

Chat-family models accept `temperature`; reasoning-family models reject it and
expect `reasoning_effort`/`verbosity` instead. Sending the wrong knob to the
wrong family produces a 400 on every turn, which shows up as a silently dead
call rather than a startup error (plan section 4.3, reference/troubleshooting.md).
"""

# Model-id prefixes/names that belong to the reasoning family as opposed to
# the chat family. Kept as a simple prefix list so new dated snapshots of a
# known reasoning model are recognized without an allowlist update.
_REASONING_MODEL_PREFIXES = (
    "o1",
    "o3",
    "o4",
    "gpt-5-reasoning",
)


def is_reasoning_model(model_id: str) -> bool:
    """Return True if `model_id` belongs to the reasoning family (uses
    reasoning_effort/verbosity) rather than the chat family (uses temperature)."""
    if not model_id:
        return False
    normalized = model_id.strip().lower()
    return any(normalized.startswith(prefix) for prefix in _REASONING_MODEL_PREFIXES)


def llm_sampling_kwargs(model_id: str, *, temperature: float = 0.7, reasoning_effort: str = "medium", verbosity: str = "medium") -> dict:
    """Return the correct sampling kwargs dict for `model_id`'s family.

    Callers should splat this into the plugin LLM constructor instead of
    hardcoding `temperature=...` so cascade/pipeline/realtime factories can't
    accidentally send a chat-family knob to a reasoning-family model.
    """
    if is_reasoning_model(model_id):
        return {"reasoning_effort": reasoning_effort, "verbosity": verbosity}
    return {"temperature": temperature}
