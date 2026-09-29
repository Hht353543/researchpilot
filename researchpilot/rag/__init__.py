"""Retrieval-augmented generation pipeline."""

from researchpilot.rag.chunker import MarkdownChunker
from researchpilot.rag.cleaner import TextCleaner
from researchpilot.rag.embeddings import Embedder, HashEmbedder, build_embedder
from researchpilot.rag.knowledge_base import IngestReport, KnowledgeBase
from researchpilot.rag.loader import DocumentLoader
from researchpilot.rag.models import Chunk, Document, RetrievedChunk
from researchpilot.rag.retriever import RetrievalResult, Retriever
from researchpilot.rag.vector_store import VectorStore

__all__ = [
    "Chunk",
    "Document",
    "DocumentLoader",
    "Embedder",
    "HashEmbedder",
    "IngestReport",
    "KnowledgeBase",
    "MarkdownChunker",
    "RetrievalResult",
    "RetrievedChunk",
    "Retriever",
    "TextCleaner",
    "VectorStore",
    "build_embedder",
]
