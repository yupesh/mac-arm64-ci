# mac-arm64

Manual macOS arm64 build and smoke run for a private project.
The repository holds only the workflow: sources are fetched at run time with a token from repository secrets,
the job log shows step names and results, full logs are uploaded encrypted.

Run: Actions → mac-arm64 → Run workflow (`ref`, `pytest`, `os`).
