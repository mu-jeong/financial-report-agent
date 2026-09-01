# V1 → Native V2 migration architecture

The supported migration is an offline, one-way import boundary. V1-specific
SQLite, LangChain pickle, and FAISS knowledge lives only under
`src/migrations/v2`; normal application and retrieval modules never select or
query a V1 runtime.

```text
V1 reports.db + index.pkl + index.faiss
                 │ read only
                 ▼
       migration-only reconstruction
                 │ same chunks and vectors
                 ▼
       current Native V2 candidate
                 │ normal publication protocol
                 ▼
       writable Native V2 runtime
                 │
                 └─ retire reports.db and vector_db
```

The importer reconstructs V1 parent/child spans and remaps the existing float32
vectors to the dense positive physical IDs required by the native snapshot. It
does not parse source PDFs, invoke an embedding provider, or retain a live V1
fallback. The `downloaded` PDF corpus remains in place for later incremental
updates and full rebuilds.

Deletion is ordered after successful native publication and runtime inspection.
Before publication, any failure leaves all V1 artifacts intact. If cleanup is
interrupted after publication, rerunning the migration command detects the
healthy Native V2 runtime and removes only the remaining known V1 artifacts.
