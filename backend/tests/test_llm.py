"""Model output handling that doesn't need a model."""

from relay_backend.llm import clean


def test_internal_ids_are_stripped_from_user_facing_text():
    assert clean("Alex is bringing the mics (msg_3057847fffd14517).") == "Alex is bringing the mics."
    assert clean("Updated [req_0123456789abcdef] for Cy.") == "Updated for Cy."
    assert clean("Nothing outstanding.") == "Nothing outstanding."
