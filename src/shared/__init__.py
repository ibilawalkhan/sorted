"""Shared library for Sorted, deployed as a Lambda layer.

Holds the Pydantic models, the single-table DynamoDB repository (the ONLY place
PK/SK strings are constructed), categorisation rules, and structured logging —
everything the individual Lambda handlers depend on. The import path is kept
identical locally and in Lambda: always `shared.*`.
"""
