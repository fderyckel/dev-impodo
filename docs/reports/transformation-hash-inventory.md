# Preparation hash inventory — B0

This inventory implements the Phase 0 hash review from the transformation-scale
architecture plan. Measurements use the instrumented 100,000-row Products B0
captured on 2026-08-11. CPU figures are inclusive stage bounds unless explicitly
stated; they must not be presented as time spent in SHA-256 alone.

| Evidence/purpose | Current call site and frequency | B0 bytes or characters processed | B0 inclusive CPU | Temporary representation | Decision |
| --- | --- | ---: | ---: | --- | --- |
| Registered source artifact integrity | `LocalArtifactStore.store_source`; once per upload | 35,100,277 bytes | Fixture phase, not isolated | File stream | **Keep** exact artifact SHA-256 |
| Immutable source snapshot integrity | source snapshot publisher/materializer; write once, verify on materialization | 976,068 bytes | Fixture phase, not isolated | File stream | **Keep** exact-byte verification |
| Prepared snapshot integrity | Polars prepared writer and artifact materializer; once per dataset, reused on repeat preparation | 1,397,045 bytes | Part of transformation, not isolated | File stream | **Keep** exact-byte verification and cache only immutable binding metadata |
| Source selection, mapping, schema, compiled plan, ruleset, policy | domain `content_hash` properties; several accesses per run/repository validation | Small manifest-sized inputs | Not material at B0 scale | Re-created portable dictionaries/JSON on some accesses | **Cache/persist once** when each immutable revision is created; retain all boundary hashes |
| Per-row identity grouping key | `PreparationSessionRepository.append_*`; once or more per canonical row | 100,000 identities; encoded width varies | Included in transformation/finalization | Portable identity JSON plus SHA-256 digest | **Consolidate** after Phase 1 benchmark: emit one reusable native identity representation and do not re-encode downstream |
| Canonical row JSON | `_canonical_session_row`; once per row | 223,966,700 characters persisted | 1.03–1.14 s serialization observed; construction and append are additional | Full canonical Python object, portable dictionary, UTF-8 JSON | **Remove from high-volume bulk-value plane** in Phase 2; retain logical projector and stage root |
| Canonical staging root | `PreparationSessionRepository._hash_direct_run`; one full ordered scan per run | 223,966,700 characters | 5.52 s median finalization bound | Reads JSON, decodes every row, reconstructs `CanonicalRow`, validates, then feeds the same stored bytes to the root hasher | **Keep root, remove decode-only-to-rehash** after append-time constraints and projector parity exist |
| Quality ruleset root | `QualityRuleSet.content_hash`; several small calls | Manifest-sized | Not material | Complete ruleset dictionary/JSON | **Persist/reuse** immutable hash |
| Quality row and accounting root | `QualityRepository._insert_quality_evidence`; once while publishing each logical row/accounting entry | 26,488,900 quality-row + 19,488,900 accounting characters | 11.12–12.62 s persistence-and-hash bound | Reconstructs quality/accounting Python objects and encodes JSON; encoded bytes feed both persistence and root | **Keep encoded-once root**, replace clean rows/accounting with manifest defaults plus sparse exceptions in Phase 3 |
| Quality issue/quarantine root | same quality publisher; once per exception | Zero in clean B0 | Negligible in clean B0; must be remeasured in dirty fixture | Exception object and JSON | **Keep** exact sparse evidence |
| Normalization effect/group IDs | normalization domain `_hash`; per actual effect/group | 100,000 effects, one group | Included in 6.03 s aggregation bound | Candidate/effect dictionaries and SHA-256 | **Keep deterministic IDs**, construct once and reuse durable facts in Phase 4 |
| Normalization stage root | `NormalizationRepository._insert_normalization_evidence`; one ordered effect scan per run | 38,588,900 effect + 840 group characters | 2.44 s median persistence-and-hash bound | Effects are encoded for a temporary table, sorted into durable storage, then scanned again for root hashing | **Keep root**, feed it from construct-once durable encoded facts without a second logical construction pass |
| Row IDs, issue IDs, rule/group/effect IDs | staging, quality, and normalization domain helpers; per row/exception/effect | Cardinality-dependent | Not yet isolated | Small canonical dictionaries | **Keep** because they are durable references; eliminate duplicate input encoding, not the identifiers |

## Conclusions

The boundary hashes are not the reason a small compressed source becomes a
large working set. The dominant inputs to hashing are already the repeated
row-oriented evidence forms: 224 million canonical-row characters, 46 million
quality/accounting characters, and 39 million effect characters. The safe
optimization is to stop constructing and persisting redundant payloads while
retaining source, artifact, revision, and stage-root hashes.

Phase 1 must add isolated counters for identity encoding and immutable-manifest
hash access. Phase 3 and Phase 4 must repeat this inventory on dirty/high-effect
fixtures so sparse evidence is not optimized only for the clean case.
