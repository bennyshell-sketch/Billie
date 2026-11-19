# Your notes folder

Anything you drop in here as `.md` or `.txt` becomes part of Billie's memory.
Billie retrieves from these files before answering and cites them by filename,
so you can always see where an answer came from.

Re-index after adding files:

```bash
curl -X POST http://127.0.0.1:8765/api/memory/reindex
```

The contents of this folder are git-ignored (only this README and
`example-*.md` are tracked), so your personal notes stay local.
