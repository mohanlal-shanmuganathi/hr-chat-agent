# ADR-004: Automatic "latest version wins" for policy documents

**Status:** accepted

## Context
The policy folder contained two versions of the Leave Policy (v1.2 and v1.3, with conflicting
entitlements) and two Staff Loan policies (2022 and 2025). Indexing both would produce
contradictory answers.

## Decision
During ingestion each document's version and effective date are read from its "Document and
Version History" table (falling back to the revision block, then the PDF's metadata date, which is
flagged for review). Documents are grouped by normalised policy name. The newest is `current`;
the others are `superseded`, kept in the registry with no chunks. Location-specific documents
(holiday lists) are grouped per location.

## Consequences
Dropping a newer PDF into the folder and re-running ingestion is enough to retire the old version.
The ingestion report lists every document's version and status for human review.
