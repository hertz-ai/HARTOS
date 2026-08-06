"""
HART OS — DNS / Firewall / Email module behavioral tests  (stream: dns_fw)

Covers the "re-enable hart.X local features one at a time" regression that
broke iso-desktop x4 (the 2026-06-24 everything-on sweep): the desktop closure
must turn ON `hart.firewall`, `hart.dns`, `hart.email`, the three modules must
be IMPORTED in the flake, and the firewall must use the nftables-native hook
(`extraInputRules`) instead of the iptables-only `extraCommands` /
`extraStopCommands` that trips NixOS's iptables-vs-nftables assertion and fails
the whole eval.

WHY THIS IS NOT A GREP TEST
---------------------------
A naive `assert "extraCommands" not in open(firewall).read()` would FAIL — the
word `extraCommands` is literally present in the explanatory comment inside
hart-firewall.nix. So this module ships a small *comment / string aware* Nix
structural reader and asserts on the PARSED result (import list, attribute
assignments, string-map dicts, the `config = lib.mkIf` guard form). The parser
is itself exercised with synthetic inputs (TestNixReaderBoundary) so the
comment-awareness and the `''`-string escape handling are proven behaviorally,
not assumed.

Nix cannot be evaluated on the Windows dev box, so these are structural
behavioral tests of the configuration source (see the per-stream brief:
"verify via tests/unit/test_nixos_configs.py (structural)"). They are the
sibling/extension of test_nixos_configs.py for the dns/firewall/email surface
and own a separate file so parallel streams never collide on the shared one.

Usage:
    pytest tests/unit/test_nixos_dns_firewall.py -v
"""

import os
import re
import pytest


# ─── Paths ────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIXOS_DIR = os.path.join(REPO_ROOT, "nixos")
MODULES_DIR = os.path.join(NIXOS_DIR, "modules")
CONFIGS_DIR = os.path.join(NIXOS_DIR, "configurations")

FIREWALL_NIX = os.path.join(MODULES_DIR, "hart-firewall.nix")
DNS_NIX = os.path.join(MODULES_DIR, "hart-dns.nix")
EMAIL_NIX = os.path.join(MODULES_DIR, "hart-email.nix")
FLAKE_NIX = os.path.join(NIXOS_DIR, "flake.nix")
DESKTOP_NIX = os.path.join(CONFIGS_DIR, "desktop.nix")


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def desktop_closure():
    """Every local Nix source the desktop closure is built from: the variant
    entry point plus the profiles it imports.

    NOT just configurations/desktop.nix. The variant-level surface was migrated
    into profiles/desktop.nix so ONE block could drive the ISO, the raw image
    and a nixos-rebuild alike. These tests read only the entry point, so they
    reported that MOVE as a deletion -- ten reds asserting `hart = { ... }`
    block not found, while every option they care about was set, correctly, one
    file over.

    Following the imports asserts the property that actually matters ("the
    desktop closure turns hart.firewall on") wherever the option is written, so
    the next refactor that preserves the OUTCOME does not red this file again.
    That is the difference between checking a layout and checking a claim.
    """
    raw = read(DESKTOP_NIX)
    sources = [raw]
    block = find_block(nix_skeleton(raw), r"imports\s*=\s*\[",
                       open_ch="[", close_ch="]")
    for rel in list_paths(block):
        path = os.path.normpath(os.path.join(CONFIGS_DIR, rel))
        # Non-local entries (e.g. "${modulesPath}/...") never resolve to a file
        # here and are skipped; they are upstream NixOS, not our surface.
        if os.path.isfile(path):
            sources.append(read(path))
    return "\n".join(sources)


# ═══════════════════════════════════════════════════════════════
# A tiny comment / string aware Nix structural reader.
#
# This is the "code under test" for the boundary suite: it mirrors how the
# Nix evaluator (not a text search) would see the file, so a token that only
# appears inside a `#` comment or a `"`/`''` string is NOT mistaken for a
# real attribute. None of these functions evaluate Nix — they extract enough
# structure to assert observable facts about the configuration.
# ═══════════════════════════════════════════════════════════════


def _transform(content, drop_dquote_strings):
    """Single left-to-right scan that removes comments and (optionally)
    double-quoted strings, and ALWAYS removes Nix indented `''...''` strings.

    Correctly handles Nix indented-string escapes so a shell snippet such as
    `''  echo ''${1}  ''` does not terminate the string early at `''$`.
    """
    out = []
    i = 0
    n = len(content)
    while i < n:
        c = content[i]

        # ── line comment ──
        if c == "#":
            k = content.find("\n", i)
            if k == -1:
                out.append("\n")
                break
            out.append("\n")
            i = k + 1
            continue

        # ── block comment /* ... */ ──
        if c == "/" and i + 1 < n and content[i + 1] == "*":
            k = content.find("*/", i + 2)
            i = (k + 2) if k != -1 else n
            out.append(" ")
            continue

        # ── double-quoted string ──
        if c == '"':
            start = i
            i += 1
            while i < n:
                if content[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if content[i] == '"':
                    i += 1
                    break
                i += 1
            out.append(" " if drop_dquote_strings else content[start:i])
            continue

        # ── indented string '' ... '' (with escapes ''' / ''$ / ''\ ) ──
        if c == "'" and i + 1 < n and content[i + 1] == "'":
            i += 2
            while i < n:
                if content[i] == "'" and i + 1 < n and content[i + 1] == "'":
                    nxt = content[i + 2] if i + 2 < n else ""
                    if nxt == "'":          # ''' -> literal '' , stay in string
                        i += 3
                        continue
                    if nxt == "$":          # ''${ -> literal ${ , stay in string
                        i += 2
                        continue
                    if nxt == "\\":         # ''\X -> escaped char , stay in string
                        i += 3
                        continue
                    i += 2                  # real terminator
                    break
                i += 1
            out.append(" ")
            continue

        out.append(c)
        i += 1
    return "".join(out)


def nix_skeleton(content):
    """Code only: comments + ALL strings removed (attribute names/booleans kept)."""
    return _transform(content, drop_dquote_strings=True)


def strip_comments(content):
    """Comments removed, double-quoted strings KEPT (for string-map parsing)."""
    return _transform(content, drop_dquote_strings=False)


def find_block(content, header_re, open_ch="{", close_ch="}"):
    """Return the inner text of the first `<header> { ... }` (or `[ ... ]`)
    block, balanced and comment/string aware. None if not found."""
    m = re.search(header_re, content)
    if not m:
        return None
    i = content.find(open_ch, m.start())
    if i == -1:
        return None
    depth = 0
    j = i
    n = len(content)
    while j < n:
        c = content[j]
        if c == "#":
            k = content.find("\n", j)
            j = (k + 1) if k != -1 else n
            continue
        if c == "/" and j + 1 < n and content[j + 1] == "*":
            k = content.find("*/", j + 2)
            j = (k + 2) if k != -1 else n
            continue
        if c == '"':
            j += 1
            while j < n:
                if content[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if content[j] == '"':
                    j += 1
                    break
                j += 1
            continue
        if c == "'" and j + 1 < n and content[j + 1] == "'":
            j += 2
            while j < n:
                if content[j] == "'" and j + 1 < n and content[j + 1] == "'":
                    nxt = content[j + 2] if j + 2 < n else ""
                    if nxt == "'":
                        j += 3
                        continue
                    if nxt == "$":
                        j += 2
                        continue
                    if nxt == "\\":
                        j += 3
                        continue
                    j += 2
                    break
                j += 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return content[i + 1:j]
        j += 1
    return None


def list_paths(block):
    """Real relative `./...nix` and `../...nix` import paths in a list block
    (comment-stripped first so a commented-out path is NOT counted).

    The parent-relative form matters: a variant at configurations/ imports its
    shared profile as `../profiles/desktop.nix`. Matching only `./` did not
    miss it outright — worse, it matched from the SECOND dot and yielded
    `./profiles/desktop.nix`, a path that resolves under configurations/ and
    does not exist. A silently wrong path reads as "no such import"."""
    if block is None:
        return []
    return re.findall(r"\.{1,2}\/[\w./-]+\.nix", strip_comments(block))


# NixOS priority wrappers. The VALUE is what these tests assert; the priority
# is a separate concern (which option definition wins a merge). hart-firewall
# moved to `lib.mkDefault true` so an image format can still set the option to
# false without a "has conflicting definition values" eval failure — a real fix
# that made bool_assignment return None and read as "the firewall is not
# enabled". Unwrapping keeps the assertion about the value, where it belongs.
_PRIORITY_WRAPPER = (
    r"(?:(?:lib\.)?mk(?:Default|Force)\s+|(?:lib\.)?mkOverride\s+\d+\s+)*"
)


def bool_assignment(code, dotted):
    """Return 'true'/'false'/None for `<dotted> = true|false`, seeing through
    NixOS priority wrappers (`lib.mkDefault true`, `mkForce false`,
    `lib.mkOverride 50 true`). Comment-aware: caller passes comment-stripped
    code. The leading boundary stops `networking.firewall.enable` from matching
    a query for `firewall.enable`."""
    pat = (r"(?:^|[^\w.])" + re.escape(dotted) + r"\s*=\s*"
           + _PRIORITY_WRAPPER + r"(true|false)\b")
    m = re.search(pat, code)
    return m.group(1) if m else None


def has_assignment(code, name):
    """True if `<name> =` appears as a real attribute assignment, INCLUDING the
    dotted form `a.b.<name> = ...`. The boundary excludes only word characters
    (not `.`) so the dangerous `networking.firewall.extraCommands = ...` form is
    caught, while `someExtraCommands` (different leading word char) is not."""
    pat = r"(?:^|[^\w])" + re.escape(name) + r"\s*="
    return re.search(pat, code) is not None


def string_map(block):
    """Parse `"key" = "value";` pairs out of a (comment-stripped) attr block."""
    if block is None:
        return {}
    block = strip_comments(block)
    out = {}
    for m in re.finditer(r'"([^"\\]+)"\s*=\s*"([^"\\]+)"', block):
        out[m.group(1)] = m.group(2)
    return out


def config_form(content):
    """Classify the top-level `config = ...`:
        ('gated', guard_text)   when `config = lib.mkIf <guard> ...`
        ('ungated', '')         when `config = { ... }`  (always applied)
        None                    when there is no top-level config attr
    Comment-aware (so a commented example never counts)."""
    code = strip_comments(content)
    m = re.search(r"(?:^|[^\w.])config\s*=\s*", code)
    if not m:
        return None
    rest = code[m.end():].lstrip()
    gm = re.match(r"(?:lib\.)?mkIf\b(.*)", rest, re.DOTALL)
    if gm:
        return ("gated", gm.group(1)[:200])
    if rest.startswith("{"):
        return ("ungated", "")
    return ("other", rest[:40])


# ═══════════════════════════════════════════════════════════════
# Section 0: Boundary — the parser itself (synthetic inputs)
#
# These prove the comment/string awareness that separates this suite from a
# grep test. Empty / missing / commented / disabled are all exercised here.
# ═══════════════════════════════════════════════════════════════

class TestNixReaderBoundary:

    def test_empty_input_is_graceful(self):
        assert nix_skeleton("") == ""
        assert strip_comments("") == ""
        assert list_paths("") == []
        assert string_map("") == {}
        assert bool_assignment("", "a.b") is None
        assert has_assignment("", "x") is False
        assert config_form("") is None
        assert find_block("", r"hart\s*=\s*\{") is None

    def test_line_comment_is_stripped(self):
        code = nix_skeleton("a = 1; # extraCommands = true;\nb = 2;")
        assert "extraCommands" not in code
        assert "b = 2" in code

    def test_token_only_in_comment_is_not_an_assignment(self):
        """THE regression guard: a name appearing only in a comment must not
        be read as a real attribute assignment."""
        src = "config = lib.mkIf x {\n  # the iptables-only `extraCommands` would break\n};"
        code = strip_comments(src)
        assert has_assignment(code, "extraCommands") is False

    def test_real_assignment_is_detected_dotted(self):
        """The dangerous iptables form is a DOTTED path — must be caught."""
        code = strip_comments("networking.firewall.extraCommands = ''iptables -A'';")
        assert has_assignment(code, "extraCommands") is True

    def test_real_assignment_is_detected_nested(self):
        """...and the nested form inside `networking.firewall = { ... }`."""
        code = strip_comments("networking.firewall = {\n  extraCommands = ''iptables -A'';\n};")
        assert has_assignment(code, "extraCommands") is True

    def test_assignment_not_matched_inside_longer_word(self):
        code = strip_comments("someExtraCommandsList = [];")
        assert has_assignment(code, "extraCommands") is False

    def test_bool_assignment_ignores_commented_value(self):
        src = "x.enable = true;\n# y.enable = true;\n"
        code = strip_comments(src)
        assert bool_assignment(code, "x.enable") == "true"
        assert bool_assignment(code, "y.enable") is None

    def test_bool_assignment_dotted_boundary(self):
        """A query for `firewall.enable` must NOT match `networking.firewall.enable`."""
        code = strip_comments("networking.firewall.enable = true;")
        assert bool_assignment(code, "firewall.enable") is None
        assert bool_assignment(code, "networking.firewall.enable") == "true"

    def test_indented_string_escape_does_not_swallow_following_code(self):
        """`''$` escape inside a shell `''` string must not terminate the string
        early — the `b = true;` that follows must still be parseable."""
        src = "a = '' echo ''${1:-status} and ''' done ''; b.enable = true;"
        skel = nix_skeleton(src)
        assert "echo" not in skel              # string body removed
        assert bool_assignment(skel, "b.enable") == "true"

    def test_find_block_is_balanced(self):
        inner = find_block("hart = { a = { z = 1; }; b = 2; };", r"hart\s*=\s*\{")
        assert "b = 2" in inner
        assert inner.count("{") == inner.count("}")  # nested block fully captured

    def test_bool_assignment_sees_through_priority_wrappers(self):
        """A NixOS option's VALUE is what these tests assert; mkDefault /
        mkForce / mkOverride only decide which definition wins a merge. Reading
        a wrapped `true` as "unset" is how a real priority fix in
        hart-firewall.nix came to look like a disabled firewall."""
        assert bool_assignment("networking.firewall.enable = true;",
                               "networking.firewall.enable") == "true"
        assert bool_assignment("networking.firewall.enable = lib.mkDefault true;",
                               "networking.firewall.enable") == "true"
        assert bool_assignment("x.enable = mkDefault true;", "x.enable") == "true"
        assert bool_assignment("x.enable = lib.mkForce false;", "x.enable") == "false"
        assert bool_assignment("x.enable = lib.mkOverride 50 true;",
                               "x.enable") == "true"

    def test_bool_assignment_still_rejects_a_non_boolean_rhs(self):
        """Unwrapping must not turn the parser into "matches anything": a
        non-boolean RHS is still None, so `enable = cfg.something` cannot be
        mistaken for a literal true."""
        assert bool_assignment("x.enable = cfg.wanted;", "x.enable") is None
        assert bool_assignment("x.enable = lib.mkDefault cfg.wanted;",
                               "x.enable") is None
        assert bool_assignment("x.enable = truthy;", "x.enable") is None

    def test_bool_assignment_keeps_its_dotted_boundary(self):
        """The pre-existing guarantee, re-asserted against the new pattern: a
        query for the short name must not match the longer dotted option."""
        assert bool_assignment("networking.firewall.enable = true;",
                               "firewall.enable") is None

    def test_list_paths_captures_parent_relative_imports(self):
        """A variant imports its shared profile as `../profiles/x.nix`. Matching
        only `./` silently produced `./profiles/x.nix` — a path that resolves in
        the wrong directory and looks like a missing import."""
        block = "[\n  ../profiles/desktop.nix\n  ./local.nix\n]"
        paths = list_paths(block)
        assert "../profiles/desktop.nix" in paths
        assert "./local.nix" in paths

    def test_list_paths_skips_commented_path(self):
        block = "[\n  ./modules/a.nix\n  # ./modules/ghost.nix\n  ./modules/b.nix\n]"
        paths = list_paths(block)
        assert "./modules/a.nix" in paths
        assert "./modules/b.nix" in paths
        assert "./modules/ghost.nix" not in paths

    def test_string_map_parses_pairs_and_skips_comments(self):
        block = '{\n  "x-scheme-handler/http" = "firefox.desktop";\n  # "mailto" = "x";\n}'
        m = string_map(block)
        assert m["x-scheme-handler/http"] == "firefox.desktop"
        assert "mailto" not in m

    def test_config_form_detects_ungated_vs_gated(self):
        assert config_form("config = { x = 1; };")[0] == "ungated"
        g = config_form("config = lib.mkIf cfg.enable { x = 1; };")
        assert g[0] == "gated"
        assert "enable" in g[1]


# ═══════════════════════════════════════════════════════════════
# Section 1: Modules are IMPORTED in the flake
# ═══════════════════════════════════════════════════════════════

class TestModulesImported:

    @pytest.fixture(autouse=True)
    def load(self):
        self.flake = read(FLAKE_NIX)
        block = find_block(self.flake, r"hartModules\s*=\s*\[", open_ch="[", close_ch="]")
        assert block is not None, "hartModules = [ ... ] list not found in flake.nix"
        self.imports = list_paths(block)

    def test_firewall_module_imported(self):
        assert "./modules/hart-firewall.nix" in self.imports

    def test_dns_module_imported(self):
        assert "./modules/hart-dns.nix" in self.imports

    def test_email_module_imported(self):
        assert "./modules/hart-email.nix" in self.imports

    def test_all_three_imported_together(self):
        for mod in ("hart-firewall.nix", "hart-dns.nix", "hart-email.nix"):
            assert ("./modules/" + mod) in self.imports, \
                "Module not imported in hartModules: " + mod

    def test_module_files_exist_on_disk(self):
        for p in (FIREWALL_NIX, DNS_NIX, EMAIL_NIX):
            assert os.path.isfile(p), "Imported module file missing: " + p


# ═══════════════════════════════════════════════════════════════
# Section 2: hart-firewall uses nftables (NO iptables extraCommands)
#            — THE regression this stream fixes
# ═══════════════════════════════════════════════════════════════

class TestFirewallUsesNftables:

    @pytest.fixture(autouse=True)
    def load(self):
        self.raw = read(FIREWALL_NIX)
        self.code = strip_comments(self.raw)          # comment-aware view

    def test_nftables_is_enabled(self):
        assert bool_assignment(self.code, "networking.nftables.enable") == "true"

    def test_firewall_is_enabled(self):
        assert bool_assignment(self.code, "networking.firewall.enable") == "true"

    def test_no_iptables_extra_commands(self):
        """The regression: iptables-only extraCommands under nftables trips the
        incompatibility assertion and fails the desktop ISO eval. Comment-aware
        so the explanatory comment that NAMES extraCommands does not false-fail."""
        assert has_assignment(self.code, "extraCommands") is False
        assert has_assignment(self.code, "extraStopCommands") is False

    def test_uses_nftables_native_extra_input_rules(self):
        """The nftables-native replacement for the old iptables rate-limit."""
        assert has_assignment(self.code, "extraInputRules") is True

    def test_rate_limit_rule_is_nftables_syntax(self):
        """The appended input rule must be nftables `limit rate over ... drop`,
        not an `iptables -A` shell line."""
        rule = find_block(
            self.raw,
            r"extraInputRules\s*=\s*lib\.optionalString[^\n]*",
            open_ch="'", close_ch="'",
        )
        # The rule body lives in a '' string; read it from the raw source region.
        idx = self.raw.find("extraInputRules")
        body = self.raw[idx:idx + 400]
        assert "limit rate over" in body
        assert "iptables" not in body

    def test_config_is_gated_so_disabled_is_a_noop(self):
        """Boundary: when hart/hart.firewall is off the module pulls NO closure
        (the 'opt-in, pure no-op for every variant' contract the everything-on
        sweep violated)."""
        form = config_form(self.raw)
        assert form is not None and form[0] == "gated"
        assert "enable" in form[1]

    def test_firmware_check_is_offline_resilient(self):
        """Boundary/offline: the weekly fwupd check must not hard-fail when the
        box is offline — the refresh is guarded so the oneshot still exits 0."""
        idx = self.raw.find("fwupdmgr refresh")
        assert idx != -1
        window = self.raw[idx:idx + 120]
        assert "|| true" in window


# ═══════════════════════════════════════════════════════════════
# Section 3: The three enables are present on the desktop (and ONLY there)
# ═══════════════════════════════════════════════════════════════

class TestDesktopEnables:

    @pytest.fixture(autouse=True)
    def load(self):
        self.raw = desktop_closure()
        block = find_block(nix_skeleton(self.raw), r"(?:^|[^\w.])hart\s*=\s*\{")
        assert block is not None, (
            "`hart = { ... }` block not found anywhere in the desktop closure "
            "(configurations/desktop.nix + the profiles it imports)")
        self.hart = block

    def test_firewall_enabled(self):
        assert bool_assignment(self.hart, "firewall.enable") == "true"

    def test_dns_enabled(self):
        assert bool_assignment(self.hart, "dns.enable") == "true"

    def test_email_enabled(self):
        assert bool_assignment(self.hart, "email.enable") == "true"

    def test_all_three_enables_present(self):
        for name in ("firewall.enable", "dns.enable", "email.enable"):
            assert bool_assignment(self.hart, name) == "true", \
                "desktop hart block missing enable: " + name

    @pytest.mark.parametrize("variant", ["server", "edge", "phone"])
    def test_not_enabled_on_other_variants(self, variant):
        """Boundary: the three features are opt-in and default OFF everywhere
        except desktop — re-enabling them one variant at a time is exactly the
        discipline the everything-on regression broke."""
        cfg = read(os.path.join(CONFIGS_DIR, variant + ".nix"))
        code = strip_comments(cfg)
        for name in ("firewall.enable", "dns.enable", "email.enable"):
            assert bool_assignment(code, name) is None, \
                variant + ".nix unexpectedly sets " + name


# ═══════════════════════════════════════════════════════════════
# Section 4: DNS module behavior
# ═══════════════════════════════════════════════════════════════

class TestDnsModule:

    @pytest.fixture(autouse=True)
    def load(self):
        self.raw = read(DNS_NIX)
        self.code = strip_comments(self.raw)

    def test_uses_systemd_resolved(self):
        assert bool_assignment(self.code, "services.resolved.enable") == "true" \
            or has_assignment(self.code, "services.resolved")

    def test_config_is_gated(self):
        form = config_form(self.raw)
        assert form is not None and form[0] == "gated"
        assert "enable" in form[1]

    def test_no_plaintext_fallback_by_default(self):
        """Security/offline boundary: the default must NOT silently downgrade to
        unencrypted DNS when the encrypted upstream is unreachable."""
        block = find_block(self.raw, r"fallbackToPlaintext\s*=\s*lib\.mkOption")
        assert block is not None
        assert bool_assignment(block, "default") == "false"

    def test_dnssec_on_by_default(self):
        block = find_block(self.raw, r"(?:^|[^\w.])dnssec\s*=\s*lib\.mkOption")
        assert block is not None
        assert bool_assignment(block, "default") == "true"


# ═══════════════════════════════════════════════════════════════
# Section 5: Email module owns the mailto handler (collision-avoidance)
# ═══════════════════════════════════════════════════════════════

class TestEmailOwnsMailto:

    def test_email_registers_mailto(self):
        raw = read(EMAIL_NIX)
        block = find_block(raw, r"xdg\.mime\.defaultApplications\s*=")
        m = string_map(block)
        assert m.get("x-scheme-handler/mailto") == "thunderbird.desktop"

    def test_desktop_does_not_also_set_mailto(self):
        """Regression: a second, conflicting attrsOf-str definition of
        x-scheme-handler/mailto would fail eval. The desktop xdg.mime block must
        NOT set it (comment-aware: the explanatory comment that names it must
        not count as a key)."""
        raw = desktop_closure()
        block = find_block(raw, r"xdg\.mime\.defaultApplications\s*=")
        m = string_map(block)
        assert "x-scheme-handler/mailto" not in m
        # sanity: the parser does see the other real handlers in that same block
        assert m.get("x-scheme-handler/http") == "firefox.desktop"

    def test_email_config_is_gated(self):
        form = config_form(read(EMAIL_NIX))
        assert form is not None and form[0] == "gated"
        assert "enable" in form[1]
