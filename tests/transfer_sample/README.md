# transfer-sample

A tiny package with a native dependency (`msgpack`) used as the payload for the
wheelport cross-platform ZIP transfer CI. Keeping the payload small avoids
bundling wheelport's own solver dependencies (uv, Poetry) into every transferred
bundle.
