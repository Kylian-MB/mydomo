"""Errors raised by the gatekeeper.

Messages carry refs, paths and other metadata only: never a secret value or key material.
"""


class GatekeeperError(Exception):
    """Base class of every gatekeeper-specific error."""


class ConfigurationError(GatekeeperError):
    """The gatekeeper cannot start with the configuration it was given."""


class ServerStartupError(GatekeeperError):
    """A web server of the gatekeeper could not start (a port already in use, for instance)."""


class MasterKeyMissingError(GatekeeperError):
    """The master key file is missing although the vault already holds secrets."""


class MasterKeyCorruptError(GatekeeperError):
    """The master key file exists but is not a valid 32-byte key."""


class VaultDecryptError(GatekeeperError):
    """A stored secret cannot be decrypted: wrong master key or tampered data."""


class BindingMismatchError(GatekeeperError):
    """The bindings of a secret forbid the operation.

    Either the secret already exists with other bindings (they are immutable), or none of its
    bindings allows the destination it is about to be sent to.
    """


class InsecureTransportError(BindingMismatchError):
    """A secret would travel over plain http to a host that is not local.

    A subclass of ``BindingMismatchError``: the destination is refused whatever its bindings say.
    """


class InvalidSecretValueError(GatekeeperError, ValueError):
    """A secret value cannot be stored or sent: empty, invalid Unicode, or unfit for a header.

    It is a ``ValueError`` too, so callers that only care about bad input can catch that.
    """


class RequestNotPendingError(GatekeeperError):
    """A secret request is not pending: already fulfilled, expired or superseded."""


class InvalidRelayPath(GatekeeperError):
    """A relay path is not an acceptable path below the provider's base URL."""


class UnknownProviderError(GatekeeperError):
    """No provider route is registered under the requested id."""


class UpstreamError(GatekeeperError):
    """A provider could not be reached, or its answer cannot be relayed safely.

    The message never carries request details: they include the injected key.
    """


class UpstreamTimeoutError(UpstreamError):
    """A provider did not answer in time."""
