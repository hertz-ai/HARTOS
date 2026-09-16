//! The A2UI home payload the SHELL actually produces, frozen for the native decoder.
//!
//! GENERATED, not hand-written. `tests/unit/test_native_wire_contract.py` runs the real
//! `liquid_ui_service._sanitize_home_payload` on a realistic LLM-authored home and asserts
//! this string still matches it byte for byte, so a change to the wire shape fails there
//! with the regeneration command rather than silently leaving the decoder tested against a
//! payload no producer sends any more. That is not hypothetical: the decoder once read
//! four keys (`hero.title`, `hero.copy`, `row.label`, `card.subtitle`) that NO producer has
//! ever emitted, and every unit test passed because each side built its own fixtures.
//!
//! A `.rs` file rather than the `.json` it obviously is, for one build reason: the crane
//! source filter keeps `Cargo.toml`/`Cargo.lock` and `*.rs` ONLY (hart-comp.nix says so at
//! its `compCleanSrc`), so a `.json` beside the crate would be filtered out of the build
//! sandbox and `include_str!` would fail in CI while passing locally.

/// The verbatim output of the home-compose sanitizer. Test-only.
#[cfg(test)]
pub const HOME_COMPOSE_SANITIZED: &str = r#"{
  "hero": {
    "agents": 3,
    "amount": 1284,
    "amount_unit": "Spark",
    "eyebrow": "Earned on the hive",
    "local": true,
    "payout_pending": true,
    "primary": {
      "action": "resume",
      "label": "Resume",
      "target": "recipes"
    },
    "secondary": {
      "action": "ask",
      "label": "Ask anything"
    },
    "tasks": 7
  },
  "mood": "aurora",
  "rows": [
    {
      "accent": "teal",
      "cards": [
        {
          "action": "resume",
          "icon": "code",
          "meta": "3 files changed",
          "progress": 0.62,
          "target": "recipes",
          "title": "Refactor the parser",
          "topic": "Refactor the parser"
        },
        {
          "action": "open",
          "image_url": "https://example.invalid/a.jpg",
          "live": "RUNNING",
          "meta": "12 sources",
          "title": "Morning briefing",
          "topic": "Morning briefing"
        }
      ],
      "see_all": "recipes",
      "title": "Continue"
    },
    {
      "accent": "magenta",
      "cards": [
        {
          "action": "open",
          "badge": "NEW",
          "icon": "explore",
          "meta": "412 tasks",
          "title": "Scout",
          "topic": "Scout"
        },
        {
          "action": "open",
          "image": "/shell/static/app_art/a.svg",
          "meta": "388 tasks",
          "title": "Archivist",
          "topic": "Archivist"
        }
      ],
      "ranked": true,
      "see_all": "agents_browse",
      "title": "Top agents"
    }
  ]
}"#;
