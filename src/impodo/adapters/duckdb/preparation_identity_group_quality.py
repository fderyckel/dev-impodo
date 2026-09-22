"""Project symbolic group links and propagate readiness inside a bounded database.

Identity-bearing datasets currently use stored canonical JSON. Read only their
identity, scope, and links in bounded pages; scalar values and lineage are not
decoded here. Transaction-local relations keep graph membership, visited rows,
and the work queue out of Python. They are rebuilt from immutable evidence on
every attempt, so historical runs need no storage migration or in-place repair.
"""

from __future__ import annotations

from hashlib import sha256
import json

from impodo.domain.preparation.quality import quality_dependency_reference_contexts
from impodo.domain.shared.models import canonical_json_bytes, portable_value, restore_portable_value
from .serialization import iter_encoded_json_batches
from .constants import DUCKDB_JSON_BATCH_MAX_BYTES, PREPARATION_SESSION_ROW_BATCH_SIZE
from .native_prepared_projection import projected_hybrid_dependency_rows_sql


_EDGE_STRUCTURE = '[{"child":"BIGINT","parent_dataset":"VARCHAR","identity_hash":"VARCHAR","identity_group":"BOOLEAN","target_field":"VARCHAR"}]'


def _materialize_unsafe_group_rows(connection, session_id, unsafe_row_ids) -> None:
    """Reach each unsafe ordinal once with a database-owned frontier."""

    connection.execute("""
        CREATE TEMP TABLE group_propagating_arc AS
        SELECT arc.source, arc.destination
          FROM group_arc AS arc
          JOIN group_propagating AS allowed ON allowed.ordinal = arc.destination
    """)
    connection.execute("""
        CREATE TEMP TABLE group_unsafe AS
        WITH RECURSIVE unsafe(ordinal) AS (
            SELECT ordinal FROM canonical_staging_row
             WHERE run_id = ? AND row_id IN (SELECT unnest(?))
            UNION
            SELECT child FROM group_match
             WHERE match_count != 1 AND child IN (SELECT ordinal FROM group_propagating)
            UNION
            SELECT arc.destination
              FROM unsafe AS frontier
              JOIN group_propagating_arc AS arc ON arc.source = frontier.ordinal
        )
        SELECT ordinal FROM unsafe
    """, [session_id, list(unsafe_row_ids)])
    # UNION retains each reached ordinal once, including for cycles. DuckDB
    # owns the frontier and visited set without Python calls per group level.


def _iter_group_projection_batches(connection, session_id: str):
    """Bound transferred JSON by rows and bytes, allowing one large record."""

    next_ordinal = 0
    while batch := connection.execute(
        """
        WITH projected AS (
            SELECT ordinal, json_extract(row_json, '$.target_identity') AS identity_json,
                   json_extract(row_json, '$.target_scope') AS scope_json,
                   json_extract(row_json, '$.references') AS references_json
              FROM canonical_staging_row
             WHERE run_id = ? AND ordinal >= ? AND row_json != ''
             ORDER BY ordinal LIMIT ?
        ), budgeted AS (
            SELECT *, ROW_NUMBER() OVER (ORDER BY ordinal) AS page_row,
                   SUM(octet_length(encode(CAST(identity_json AS VARCHAR))) +
                       octet_length(encode(CAST(scope_json AS VARCHAR))) +
                       octet_length(encode(CAST(references_json AS VARCHAR))))
                       OVER (ORDER BY ordinal) AS page_bytes
              FROM projected
        )
        SELECT ordinal, identity_json, scope_json, references_json FROM budgeted
         WHERE page_row = 1 OR page_bytes <= ? ORDER BY ordinal
        """,
        [session_id, next_ordinal, PREPARATION_SESSION_ROW_BATCH_SIZE, DUCKDB_JSON_BATCH_MAX_BYTES],
    ).fetchall():
        yield batch
        next_ordinal = int(batch[-1][0]) + 1


def _project_group_edges(connection, session_id: str) -> None:
    """Build complete symbolic facts from bounded identity and link projections."""

    for batch in _iter_group_projection_batches(connection, session_id):
        def edges():
            for ordinal, identity_json, scope_json, references_json in batch:
                identity = restore_portable_value(json.loads(str(identity_json)))
                scope = restore_portable_value(json.loads(str(scope_json)))
                references = restore_portable_value(json.loads(str(references_json)))
                for reference, identity_group, target_field in quality_dependency_reference_contexts(
                    identity,
                    scope,
                    references,
                ):
                    if not reference.dataset:
                        continue
                    yield {
                        "child": int(ordinal), "parent_dataset": reference.dataset,
                        "identity_group": identity_group,
                        "target_field": target_field,
                        "identity_hash": "sha256:" + sha256(canonical_json_bytes({
                            "dataset": reference.dataset,
                            "source_identity": portable_value(reference.key),
                        })).hexdigest(),
                    }
        for encoded in iter_encoded_json_batches(
            edges(), max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
            max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
        ):
            connection.execute(
                """
                INSERT INTO group_reference SELECT DISTINCT item.child,
                    item.parent_dataset, item.identity_hash, item.identity_group,
                    item.target_field
                  FROM (SELECT UNNEST(from_json_strict(CAST(? AS JSON), ?)) AS item)
                ON CONFLICT DO NOTHING
                """,
                [encoded.payload, _EDGE_STRUCTURE],
            )


def iter_identity_group_findings(connection, session_id, unsafe_row_ids, propagating_datasets,
                                 native_projections=()):
    """Yield exact forward and reverse readiness findings after queue convergence."""

    connection.begin()
    try:
        connection.execute("""
            CREATE TEMP TABLE group_reference (
                child BIGINT, parent_dataset VARCHAR, identity_hash VARCHAR,
                identity_group BOOLEAN, target_field VARCHAR,
                PRIMARY KEY (
                    child, parent_dataset, identity_hash, identity_group,
                    target_field
                )
            )
        """)
        connection.execute("""
            INSERT INTO group_reference SELECT DISTINCT child_ordinal,
                parent_dataset, parent_identity_hash, FALSE, target_field
              FROM preparation_relationship_edge AS edge
              JOIN canonical_staging_row AS row ON row.run_id = edge.session_id
               AND row.ordinal = edge.child_ordinal
             WHERE edge.session_id = ? AND row.row_json = ''
        """, [session_id])
        _project_group_edges(connection, session_id)
        for projection, path in native_projections:
            connection.execute("""
                DELETE FROM group_reference WHERE child >= ? AND child < ? AND NOT identity_group
            """, [projection.ordinal_start, projection.ordinal_start + projection.row_count])
            connection.execute(
                "INSERT INTO group_reference " + projected_hybrid_dependency_rows_sql(projection)
                + " ON CONFLICT DO NOTHING", [str(path)],
            )
        connection.execute("""
            CREATE TEMP TABLE group_match AS
            WITH parent_keys AS (
                SELECT dataset, identity_hash, COUNT(*) AS match_count, MIN(ordinal) AS ordinal
                  FROM preparation_direct_identity WHERE session_id = ?
                 GROUP BY dataset, identity_hash
            )
            SELECT ref.child, ref.parent_dataset, ref.target_field,
                   ref.identity_group, COALESCE(parent.match_count, 0) AS match_count,
                   CASE WHEN parent.match_count = 1 THEN parent.ordinal END AS parent
              FROM group_reference AS ref
              LEFT JOIN parent_keys AS parent
                ON parent.dataset = ref.parent_dataset
               AND parent.identity_hash = ref.identity_hash
        """, [session_id])
        connection.execute("""
            CREATE TEMP TABLE group_arc AS
            SELECT DISTINCT parent AS source, child AS destination,
                   FALSE AS reverse, parent_dataset, target_field
              FROM group_match WHERE match_count = 1
            UNION
            SELECT DISTINCT child, parent, TRUE, parent_dataset, target_field
              FROM group_match WHERE match_count = 1 AND identity_group
        """)
        connection.execute("""
            CREATE TEMP TABLE group_propagating AS
            SELECT ordinal FROM canonical_staging_row
             WHERE run_id = ? AND dataset IN (SELECT unnest(?))
        """, [session_id, list(propagating_datasets)])
        _materialize_unsafe_group_rows(connection, session_id, unsafe_row_ids)
        connection.execute("""
            CREATE TEMP TABLE group_finding AS
            SELECT child AS ordinal,
                   CASE WHEN match_count = 0 THEN 'MISSING' ELSE 'AMBIGUOUS' END AS state,
                   parent_dataset, target_field, NULL::BIGINT AS related_ordinal
              FROM group_match WHERE match_count != 1
            UNION
            SELECT arc.destination,
                   CASE WHEN arc.reverse THEN 'IDENTITY_GROUP' ELSE 'UNSAFE_PARENT' END,
                   arc.parent_dataset, arc.target_field, arc.source
              FROM group_arc AS arc JOIN group_unsafe AS unsafe ON unsafe.ordinal = arc.source
        """)
        connection.execute("""
            CREATE TEMP TABLE group_result AS
            SELECT ordinal, state, parent_dataset, target_field,
                   MIN(related_ordinal) AS related_ordinal
              FROM group_finding
             GROUP BY ordinal, state, parent_dataset, target_field
        """)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    cursor = connection.execute("""
        SELECT row.ordinal, row.row_id, row.dataset, row.source_row, row.disposition,
               lineage.physical_dataset_id, lineage.physical_source_row,
               finding.state, finding.parent_dataset, finding.target_field,
               related.row_id, related.source_row, related.dataset
          FROM group_result AS finding
          JOIN canonical_staging_row AS row ON row.run_id = ? AND row.ordinal = finding.ordinal
          LEFT JOIN canonical_staging_row AS related
            ON related.run_id = row.run_id
           AND related.ordinal = finding.related_ordinal
          JOIN preparation_lineage AS lineage ON lineage.session_id = row.run_id
           AND lineage.dataset = row.dataset AND lineage.output_source_row = row.source_row
         ORDER BY row.ordinal, finding.state, finding.parent_dataset,
                  finding.target_field
    """, [session_id])
    while batch := cursor.fetchmany(PREPARATION_SESSION_ROW_BATCH_SIZE):
        yield from batch
