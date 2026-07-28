"""Fail-closed domain errors."""


class ContractError(RuntimeError):
    """Raised whenever an isolation, binding, or public-safety contract fails."""


class SchemaContractError(ContractError):
    """Raised when a JSON document does not satisfy its versioned schema."""


class IsolationError(ContractError):
    """Raised when a process or path would escape the current research cycle."""


class PatchPolicyError(ContractError):
    """Raised when a candidate patch exceeds the approved change scope."""


class PublicSafetyError(ContractError):
    """Raised when public release material contains prohibited content."""
