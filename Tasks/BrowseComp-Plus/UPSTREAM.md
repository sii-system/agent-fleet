# Upstream submodule

`third_party/BrowseComp-Plus` is an unmodified git submodule of
<https://github.com/texttron/BrowseComp-Plus> at commit
`046949032b0328319cc9a02663a759ec601d9402`.

The upstream MIT license is retained in that directory. Agent Fleet adapters,
runtime provisioning, MCP serving, and compatible-API judging live only under
`Tasks/BrowseComp-Plus`; private data, indexes, models, and virtual
environments live only in the Agent Fleet cache.

The benchmark data is independently pinned to Hugging Face dataset revision
`144cff8e35b5eaef7e526346aa60774a9deb941f`. Agent Fleet reads only
`query_id`, `query`, and `answer` through Parquet range requests and invokes
the unchanged upstream decryptor locally. The multi-gigabyte document columns
are not downloaded while preparing questions.

Published retrieval indexes are pinned to dataset revision
`b3f37f70c33829eb09d04784a54277a31871fd63`. A completion manifest containing
every shard and byte size is written only after the snapshot succeeds, so an
interrupted first download cannot be mistaken for a usable index.

The default retriever additionally pins corpus revision
`1b854ae04817320c2a088c0ff9830ffcb92ca079`, the last corpus state before the
published index, and Qwen3-Embedding-0.6B revision
`c54f2e6e80b2d7b7de06f51cec4959f6b3e03418`. This avoids silently pairing the
fixed index with later corpus uploads or model changes.
