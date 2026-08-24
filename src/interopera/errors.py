class InterOperaError(Exception):
    """Base error for expected pipeline failures."""


class ExtractionApprovalError(InterOperaError):
    """Raised when reviewed extraction material is missing or changed."""


class TraceabilityError(InterOperaError):
    """Raised when a figure cannot resolve to graph-backed source chunks."""


class ConfigurationError(InterOperaError):
    """Raised for invalid or unsupported firm configuration."""


class NarrativeFirewallError(InterOperaError):
    """Raised when narrative introduces a number not produced by the engine."""
