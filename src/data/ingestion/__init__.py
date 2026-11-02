"""Streaming source readers; no business normalization or label access."""

from src.data.ingestion.csv_reader import csv_tables
from src.data.ingestion.excel_reader import excel_tables
from src.data.ingestion.source import Cell, ReaderOptions, SourceRow, SourceTable
from src.data.ingestion.sqlite_reader import sqlite_tables

READERS = {".csv": csv_tables, ".xlsx": excel_tables, ".sqlite": sqlite_tables}

__all__ = ["READERS", "Cell", "ReaderOptions", "SourceRow", "SourceTable"]
