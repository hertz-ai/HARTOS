## What this changes

<!-- One or two sentences. The diff shows what; this should say why. -->

## Why

<!-- What was wrong, or what became possible. Link the issue if there is one. -->

## What breaks if this is wrong

<!-- Tells a reviewer where to spend their attention. "Nothing, it is a
     docs change" is a perfectly good answer. -->

## How it was tested

<!-- Commands you ran, hardware you ran them on. GPU/VRAM matters here more
     than in most projects: a lot of code paths are tiered on it. -->

- [ ] `pytest tests/ -q` passes
- [ ] Added a test that fails without this change (or explained why not)
- [ ] Ran it on real hardware — OS: <!-- --> GPU/VRAM: <!-- -->

## Checklist

- [ ] One logical change (a fix and a refactor belong in separate PRs)
- [ ] No telemetry, analytics, or anything that transmits user content
- [ ] No newly-required cloud provider
- [ ] Docs/README updated if behaviour changed
