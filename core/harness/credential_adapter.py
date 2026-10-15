"""Trusted parent-side adapter boundary; credential never forwarded to a child."""


class CredentialAdapter:
    """Only trusted integration code supplies lookup/operation, never model arguments.

    The operation must return literal True/False. Exceptions and arbitrary provider
    payloads are deliberately not serialized. This is not a credentialed shell.
    """

    def __init__(self, lookup, operation):
        self._lookup = lookup
        self._operation = operation

    def perform(self):
        secret = None
        try:
            secret = self._lookup()
            result = self._operation(secret)
            return result is True
        except Exception:
            return False
        finally:
            # Drop this reference; Python strings cannot promise physical zeroization.
            secret = None
