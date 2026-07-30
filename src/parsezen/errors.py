"""Stable application errors exposed by the local processing core."""


class ParsezenError(Exception):
    """Base class for expected Parsezen failures."""


class RequestValidationError(ParsezenError):
    """The requested operation or one of its paths is invalid."""


class ConversionError(ParsezenError):
    """A supported local document could not be converted to Markdown."""


class OutputWriteError(ParsezenError):
    """A Markdown result could not be written safely."""


class FinalIntegrityError(ParsezenError):
    """The staged final artifact does not match the validated content."""


class EarlyCheckError(ParsezenError):
    """A representative sample found a material risk before the full run."""


class ReviewUnavailableError(ParsezenError):
    """A Markdown file cannot be loaded safely into the in-app review."""


class ProcessingCancelledError(ParsezenError):
    """The user requested cancellation at a safe processing boundary."""


class SettingsError(ParsezenError):
    """Application settings could not be loaded, validated or saved."""


class LocalModelUnavailableError(ParsezenError):
    """Ollama or the selected local model could not be reached."""


class ModelRecommendationError(ParsezenError):
    """Hardware-aware model recommendations could not be prepared safely."""


class ImprovementError(ParsezenError):
    """A local model response was invalid or unsafe to publish."""


class TranslationError(ParsezenError):
    """A free offline translation could not be completed safely."""


class UnexpectedProcessingError(ParsezenError):
    """An unexpected failure was recorded without exposing private document data."""
