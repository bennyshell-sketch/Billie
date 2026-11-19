# Workspace

The one folder Billie is allowed to write to — and only through a diff you
have approved.

How a write happens:

1. Billie (or you, via `POST /api/workspace/propose`) proposes a change. This
   produces a unified diff and touches nothing on disk.
2. The diff appears under **Files & permissions → Pending changes** in the UI.
3. You approve or reject it. Only an approval writes.

Two further guards: the `file.write` permission must be granted, and if the
file changed after the diff was generated the approval is refused rather than
overwriting your edit. The previous version of any overwritten or deleted file
is copied to `data/workspace-backups/`.

This folder is indexed, so Billie can search and cite what it has drafted here.
Its contents are git-ignored apart from this README.
