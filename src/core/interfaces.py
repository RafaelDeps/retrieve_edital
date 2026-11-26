"""
Core interfaces defining the standard architecture for the Retrieve Edital ETL pipeline.
Follows SOLID principles: Open/Closed, dependency inversion.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Generic, TypeVar

# T represents the Type of the data structure (e.g. Dict, or a Domain Model)
T = TypeVar("T")

# R represents the Return Type of a transformation
R = TypeVar("R")


class ISource(ABC, Generic[T]):
    """
    Interface for data extraction (Extract).
    Responsible solely for fetching raw data from the external source.
    """
    
    @abstractmethod
    def read(self) -> List[T]:
        """Reads and extracts data from the origin."""
        pass


class ITransform(ABC, Generic[T, R]):
    """
    Interface for data normalization and business rules (Transform).
    Responsible solely for converting raw input into validated domain objects.
    """
    
    @abstractmethod
    def process(self, raw_data: T) -> R:
        """Processes raw data, cleans it, and maps it to a domain structure."""
        pass


class ISink(ABC, Generic[T]):
    """
    Interface for data persistence (Load).
    Responsible solely for writing the validated data into the final target.
    """

    @abstractmethod
    def write(self, items: List[T]) -> Dict[str, T]:
        """
        Persists the items sequentially or in batch to the storage solution.

        Returns the items actually persisted, keyed by the identifier the storage
        assigned them (for the JSON sink, the output filename base). Flows use
        this to register only what reached the target: registering an item whose
        write failed would mark it as processed with nothing to show for it.
        """
        pass


class IExtractionService(ABC):
    """
    Interface for the AI extraction used by sources and transforms.

    One implementation per provider (Gemini by default, Mistral optional), picked
    by `get_extraction_service()` from `LLM_PROVIDER`. Callers never know which
    provider is behind it. Contract: specs/004 contracts/extraction-service.md.
    """

    @abstractmethod
    def extract_from_pdf(self, pdf_bytes: bytes, filename: str, check_theme: bool = False) -> Any:
        """
        Reads the edital and returns an `EditalDomain` with `link=""`, or `None`
        on an extraction failure (PDF without text, invalid answer, item error).
        Raises `AIUnavailableError` when the AI is unavailable for this run
        (daily quota, missing or invalid key), so the caller can defer the item.
        """

    @abstractmethod
    def extract_from_text(self, text: str, filename: str, check_theme: bool = False) -> Any:
        """
        Same as `extract_from_pdf`, for a document that already arrived as text
        (the gov.br detour through the Jina Reader). Empty text returns `None`
        without calling the AI.

        With `check_theme`, the AI also says whether the document is a funding
        opportunity; when it is not, `OffTopicError(motivo)` is raised (spec 009).
        """

    @abstractmethod
    def classify_document_titles(self, titles: List[str]) -> Dict[str, str]:
        """Maps each title to "edital", "anexo" or "alteração". Never raises."""

    @abstractmethod
    def categorize_finep_by_description(self, description: str) -> str:
        """Returns one of the FINEP categories, or "" on failure. Never raises."""
