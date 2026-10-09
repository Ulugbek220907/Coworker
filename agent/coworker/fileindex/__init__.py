"""File index: names and extracted text for the user's folders.

Import from the package root:
    FileIndex, Budgets, default_roots   - the index and its settings
    Crawler, CrawlReport, BudgetExceeded - the crawl, for callers that drive a pass themselves
"""
from .crawl import BudgetExceeded, Crawler, CrawlReport, FileRecord
from .index import Budgets, FileIndex, default_roots

__all__ = [
    "BudgetExceeded",
    "Budgets",
    "Crawler",
    "CrawlReport",
    "FileIndex",
    "FileRecord",
    "default_roots",
]
