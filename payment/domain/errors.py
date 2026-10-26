"""Internal payment errors that never reach HTTP (LLD §9.2)."""


class DuplicatePayment(Exception):
    """A payment with the same idempotency key already exists; the charge returns that payment instead."""
