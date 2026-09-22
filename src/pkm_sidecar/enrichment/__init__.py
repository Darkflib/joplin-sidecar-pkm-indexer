"""Enrichment: suggested titles and tags (docs/enrichment.md).

Deliberately a subpackage rather than flat modules. The plan's write-back
increment has to prove that no indexing or enrichment code can reach a mutating
Joplin client, and a package boundary makes that testable as an import rule
rather than a convention.

Nothing here writes to Joplin. This increment is suggest-only.
"""
