# Internal knowledge

This branch is `main` plus the working material that does not belong in a public
repo. It exists so nothing is lost structurally when `main` is cleaned.

**Do not push this branch to a public remote.** Everything here was removed from
`main` because publishing it was the problem. Pushing it restores that problem
under a different name.

## What lives here and why it left main

| File | Removed in | Reason |
|---|---|---|
| `STEWARD_INSTRUCTION_LOG.md` | `06e5bae0` | 1,812 verbatim private messages, colleagues' emails, a personal phone number, internal hosts, a shipped default credential |
| `FLYWHEEL_RECOVERY_BRIEF.md` | `06e5bae0` | public IP to hostname map, LAN topology, cloud hosts with usernames, master key path. **Credentials redacted in `8ae702e6`; the originals are in git history and must be rotated, not deleted** |
| `HARTOS_PARALLEL_PATH_AUDIT.md` | `06e5bae0` | ten live defects with file and line, including a key leak and an unauthenticated route |
| the rest of `docs/internal/` | `47772d65`, `4ccea8c0` | session ledgers, dated self-audits, remaining-work trackers, agent prompts |

## Keeping it current

```
git checkout internal-knowledge
git merge main
```

`main` stays the public truth. This branch is main plus context.

## Still outstanding on main

Third-party subscriber emails in `tests/test_mailing_list_syntax.py` and
`integrations/channels/mailing_list.py`, a personal phone number across seven
source files, the superadmin allowlist in `core/superadmins.py`, commercial
figures, and the pre-release checklist at `docs/architecture/technical-reference.md`
section 42. Fourteen files under `docs/architecture/` still quote typed private
directives verbatim.
