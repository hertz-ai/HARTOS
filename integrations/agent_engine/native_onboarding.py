#!/usr/bin/env python3
"""
HART OS Native Onboarding — "Light Your HART"

GTK4/libadwaita native application for the first-boot onboarding ceremony.
Runs before any web server. Pure native UI. Zero JavaScript.

Architecture:
  - Imports hart_onboarding.py directly (no HTTP dependency)
  - GTK4/libadwaita for native GNOME experience
  - Full-screen dark ceremony with phase transitions
  - Auto-advances with timed pauses (like the PA is speaking)

Launch:
  python native_onboarding.py              # Normal launch
  python native_onboarding.py --user-id 1  # Specify user
  python native_onboarding.py --check      # Just check if onboarded

Requires: PyGObject, GTK4, libadwaita (all standard on GNOME/NixOS)
"""

import os
import sys

# Ensure HART OS root is in path
_HART_DIR = os.environ.get(
    'HART_INSTALL_DIR',
    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
)
if _HART_DIR not in sys.path:
    sys.path.insert(0, _HART_DIR)

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk  # noqa: E402

from hart_onboarding import (  # noqa: E402
    HARTOnboardingSession, CONVERSATION_SCRIPT,
    PASSION_OPTIONS, ESCAPE_OPTIONS,
    ACKNOWLEDGMENTS_PASSION, ACKNOWLEDGMENT_ESCAPE,
    has_hart_name, get_hart_profile,
    ELEMENTS, SPIRITS,
)

# ═══════════════════════════════════════════════════════════════════════
# CSS — Dark ceremony aesthetic
# ═══════════════════════════════════════════════════════════════════════

_CSS = """
window {
    background: #080808;
}

.ceremony-bg {
    background: #080808;
}

/* ── Language selection ── */

.lang-grid {
    margin: 48px;
}

.lang-whisper {
    color: rgba(255, 255, 255, 0.3);
    font-size: 15px;
    font-weight: 400;
    letter-spacing: 3px;
}

.lang-btn {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 14px;
    padding: 18px 28px;
    min-width: 170px;
    min-height: 52px;
    transition: all 250ms ease;
}

.lang-btn:hover {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.12);
}

.lang-btn:active {
    background: rgba(255, 255, 255, 0.12);
}

.lang-native {
    color: #d8d8d8;
    font-size: 19px;
    font-weight: 400;
}

.lang-english {
    color: rgba(255, 255, 255, 0.35);
    font-size: 13px;
    font-weight: 300;
    margin-top: 4px;
}

/* ── PA text (the voice) ── */

.pa-text {
    color: #e0e0e0;
    font-size: 28px;
    font-weight: 300;
    line-height: 1.7;
}

.pa-text-warm {
    color: #f0e8e0;
    font-size: 24px;
    font-weight: 300;
    line-height: 1.5;
}

/* ── Questions ── */

.question-text {
    color: #ffffff;
    font-size: 26px;
    font-weight: 400;
    margin-bottom: 40px;
}

.option-card {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 16px;
    padding: 22px 32px;
    min-width: 220px;
    min-height: 52px;
    transition: all 250ms ease;
}

.option-card:hover {
    background: rgba(255, 255, 255, 0.09);
    border-color: rgba(255, 255, 255, 0.14);
}

.option-card:active {
    background: rgba(255, 255, 255, 0.14);
}

.option-label {
    color: #c8c8c8;
    font-size: 18px;
    font-weight: 400;
}

/* ── Acknowledgment ── */

.ack-text {
    color: rgba(255, 255, 255, 0.7);
    font-size: 26px;
    font-weight: 300;
    font-style: italic;
}

/* ── Pre-reveal ── */

.pre-reveal {
    color: rgba(255, 255, 255, 0.6);
    font-size: 30px;
    font-weight: 300;
    font-style: italic;
}

/* ── The Name ── */

.name-reveal {
    color: #ffffff;
    font-size: 72px;
    font-weight: 600;
    letter-spacing: 8px;
}

.hart-tag {
    color: rgba(255, 255, 255, 0.4);
    font-size: 18px;
    font-weight: 300;
    letter-spacing: 1px;
    margin-top: 12px;
}

.emoji-display {
    font-size: 40px;
    margin-top: 20px;
}

.reveal-intro {
    color: rgba(255, 255, 255, 0.5);
    font-size: 24px;
    font-weight: 300;
    margin-bottom: 32px;
}

/* ── Post-reveal ── */

.post-text {
    color: rgba(255, 255, 255, 0.5);
    font-size: 22px;
    font-weight: 300;
    line-height: 1.7;
    margin-top: 48px;
}

/* ── Buttons ── */

.action-btn {
    background: rgba(255, 255, 255, 0.06);
    color: #d0d0d0;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 28px;
    padding: 12px 36px;
    font-size: 16px;
    font-weight: 400;
    min-width: 140px;
    transition: all 200ms ease;
}

.action-btn:hover {
    background: rgba(255, 255, 255, 0.12);
}

.action-btn-primary {
    background: rgba(255, 255, 255, 0.10);
    color: #ffffff;
    font-weight: 500;
}

.action-btn-primary:hover {
    background: rgba(255, 255, 255, 0.18);
}

/* ── Sealed identity card ── */

.sealed-name {
    color: #ffffff;
    font-size: 48px;
    font-weight: 600;
    letter-spacing: 4px;
}

.sealed-tag {
    color: rgba(255, 255, 255, 0.4);
    font-size: 16px;
    letter-spacing: 1px;
    margin-top: 8px;
}

.sealed-message {
    color: rgba(255, 255, 255, 0.4);
    font-size: 17px;
    font-weight: 300;
    margin-top: 40px;
}
"""

# Language display names (native + English)
_LANG_DISPLAY = {
    'en': ('English', ''),
    'ta': ('\u0ba4\u0bae\u0bbf\u0bb4\u0bcd', 'Tamil'),
    'hi': ('\u0939\u093f\u0928\u094d\u0926\u0940', 'Hindi'),
    'es': ('Espa\u00f1ol', 'Spanish'),
    'fr': ('Fran\u00e7ais', 'French'),
    'de': ('Deutsch', 'German'),
    'ja': ('\u65e5\u672c\u8a9e', 'Japanese'),
    'ko': ('\ud55c\uad6d\uc5b4', 'Korean'),
    'zh': ('\u4e2d\u6587', 'Chinese'),
    'pt': ('Portugu\u00eas', 'Portuguese'),
    'ar': ('\u0627\u0644\u0639\u0631\u0628\u064a\u0629', 'Arabic'),
    'ru': ('\u0420\u0443\u0441\u0441\u043a\u0438\u0439', 'Russian'),
}


class HARTOnboardingWindow(Adw.ApplicationWindow):
    """Full-screen onboarding ceremony window."""

    def __init__(self, user_id='1', **kwargs):
        super().__init__(**kwargs)
        self.user_id = user_id
        self.session = HARTOnboardingSession(user_id)
        self._pending_timeout = None

        self.set_default_size(1200, 800)
        self.fullscreen()

        # Load CSS
        provider = Gtk.CssProvider()
        provider.load_from_string(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Main stack for phase transitions
        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=800,
        )
        self.set_content(self.stack)

        # Build first phase
        self._build_language_page()

    # ── Phase builders ──────────────────────────────────────────

    def _center_box(self, **kwargs):
        """Create a centered vertical box (common layout)."""
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            spacing=0,
        )
        outer.add_css_class('ceremony-bg')
        return outer

    def _build_language_page(self):
        """Phase 1: Language selection grid."""
        page = self._center_box()

        # Whisper title
        title = Gtk.Label(label='CHOOSE YOUR LANGUAGE')
        title.add_css_class('lang-whisper')
        page.append(title)

        spacer = Gtk.Box()
        spacer.set_size_request(-1, 40)
        page.append(spacer)

        # Language grid (4 columns)
        grid = Gtk.FlowBox(
            max_children_per_line=4,
            min_children_per_line=2,
            row_spacing=16,
            column_spacing=16,
            selection_mode=Gtk.SelectionMode.NONE,
            halign=Gtk.Align.CENTER,
            homogeneous=True,
        )
        grid.add_css_class('lang-grid')

        for lang_code, (native_name, english_name) in _LANG_DISPLAY.items():
            btn = Gtk.Button()
            btn.add_css_class('lang-btn')

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            native_lbl = Gtk.Label(label=native_name)
            native_lbl.add_css_class('lang-native')
            box.append(native_lbl)

            if english_name:
                eng_lbl = Gtk.Label(label=english_name)
                eng_lbl.add_css_class('lang-english')
                box.append(eng_lbl)

            btn.set_child(box)
            btn.connect('clicked', self._on_language_selected, lang_code)
            grid.append(btn)

        page.append(grid)
        self.stack.add_named(page, 'language')
        self.stack.set_visible_child_name('language')

    def _build_text_page(self, name, text, css_class='pa-text',
                         auto_advance_ms=0, next_builder=None):
        """Build a page that shows PA text, optionally auto-advances."""
        page = self._center_box()

        # Clamp width for readability
        clamp = Adw.Clamp(maximum_size=700, tightening_threshold=500)
        label = Gtk.Label(
            label=text,
            wrap=True,
            wrap_mode=2,  # WORD_CHAR
            justify=Gtk.Justification.CENTER,
            halign=Gtk.Align.CENTER,
        )
        label.add_css_class(css_class)
        clamp.set_child(label)
        page.append(clamp)

        self.stack.add_named(page, name)
        self.stack.set_visible_child_name(name)

        if auto_advance_ms and next_builder:
            self._schedule(auto_advance_ms, next_builder)

    def _build_question_page(self, name, question_text, options, callback):
        """Build a question page with selectable option cards."""
        page = self._center_box()

        # Question
        clamp = Adw.Clamp(maximum_size=700, tightening_threshold=500)
        q_label = Gtk.Label(
            label=question_text,
            wrap=True,
            wrap_mode=2,
            justify=Gtk.Justification.CENTER,
        )
        q_label.add_css_class('question-text')
        clamp.set_child(q_label)
        page.append(clamp)

        spacer = Gtk.Box()
        spacer.set_size_request(-1, 20)
        page.append(spacer)

        # Options grid (2 columns)
        grid = Gtk.FlowBox(
            max_children_per_line=2,
            min_children_per_line=1,
            row_spacing=14,
            column_spacing=14,
            selection_mode=Gtk.SelectionMode.NONE,
            halign=Gtk.Align.CENTER,
            homogeneous=True,
        )

        for opt in options:
            btn = Gtk.Button()
            btn.add_css_class('option-card')

            lbl = Gtk.Label(label=opt['label'])
            lbl.add_css_class('option-label')
            btn.set_child(lbl)

            btn.connect('clicked', callback, opt['key'])
            grid.append(btn)

        page.append(grid)
        self.stack.add_named(page, name)
        self.stack.set_visible_child_name(name)

    def _build_reveal_page(self, result):
        """Build the name reveal page."""
        page = self._center_box()
        page.set_spacing(0)

        # "I'm going to call you..."
        intro_text = self._line('reveal_intro')
        intro = Gtk.Label(label=intro_text)
        intro.add_css_class('reveal-intro')
        page.append(intro)

        spacer1 = Gtk.Box()
        spacer1.set_size_request(-1, 32)
        page.append(spacer1)

        # THE NAME (large, dramatic)
        name_label = Gtk.Label(label=result['name'])
        name_label.add_css_class('name-reveal')
        page.append(name_label)

        # HART tag: @element.spirit.name
        tag_label = Gtk.Label(label=result.get('hart_tag', ''))
        tag_label.add_css_class('hart-tag')
        page.append(tag_label)

        # Emoji combo
        emoji_label = Gtk.Label(label=result.get('emoji_combo', ''))
        emoji_label.add_css_class('emoji-display')
        page.append(emoji_label)

        spacer2 = Gtk.Box()
        spacer2.set_size_request(-1, 48)
        page.append(spacer2)

        # Buttons
        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=16,
            halign=Gtk.Align.CENTER,
        )

        # "That's me" button
        accept_btn = Gtk.Button(label="That's me")
        accept_btn.add_css_class('action-btn')
        accept_btn.add_css_class('action-btn-primary')
        accept_btn.connect('clicked', self._on_accept_name)
        btn_box.append(accept_btn)

        # "Try another" (only if first attempt)
        if result.get('can_try_another', True):
            retry_btn = Gtk.Button(label='Try another')
            retry_btn.add_css_class('action-btn')
            retry_btn.connect('clicked', self._on_try_another)
            btn_box.append(retry_btn)

        page.append(btn_box)

        self.stack.add_named(page, 'reveal')
        self.stack.set_visible_child_name('reveal')

    def _build_sealed_page(self, result):
        """Build the final sealed identity page."""
        page = self._center_box()

        # Post-reveal PA line
        post_text = self._line('post_reveal')
        post = Gtk.Label(
            label=post_text,
            wrap=True,
            wrap_mode=2,
            justify=Gtk.Justification.CENTER,
        )
        post.add_css_class('post-text')
        page.append(post)

        spacer1 = Gtk.Box()
        spacer1.set_size_request(-1, 48)
        page.append(spacer1)

        # The sealed name
        name = Gtk.Label(label=result.get('hart_name', ''))
        name.add_css_class('sealed-name')
        page.append(name)

        # HART tag
        tag = result.get('hart_tag', result.get('emoji_combo', ''))
        if tag:
            tag_label = Gtk.Label(label=tag)
            tag_label.add_css_class('sealed-tag')
            page.append(tag_label)

        # Emoji
        emoji = result.get('emoji_combo', '')
        if emoji:
            emoji_label = Gtk.Label(label=emoji)
            emoji_label.add_css_class('emoji-display')
            page.append(emoji_label)

        spacer2 = Gtk.Box()
        spacer2.set_size_request(-1, 48)
        page.append(spacer2)

        # "Begin" button — closes the ceremony
        begin_btn = Gtk.Button(label='Begin')
        begin_btn.add_css_class('action-btn')
        begin_btn.add_css_class('action-btn-primary')
        begin_btn.connect('clicked', self._on_begin)
        page.append(begin_btn)

        self.stack.add_named(page, 'sealed')
        self.stack.set_visible_child_name('sealed')

    # ── Event handlers ──────────────────────────────────────────

    def _on_language_selected(self, btn, lang_code):
        """User picked their language."""
        result = self.session.advance(
            action='select_language',
            data={'language': lang_code},
        )
        # Show greeting, then auto-advance to passion question
        greeting = self._line('greeting')
        self._build_text_page(
            'greeting', greeting, 'pa-text',
            auto_advance_ms=4000,
            next_builder=self._show_passion,
        )

    def _show_passion(self):
        """Show the passion question."""
        lang = self.session.language
        question = self._line('question_passion')
        options = [{
            'key': p['key'],
            'label': p['labels'].get(lang, p['labels']['en']),
        } for p in PASSION_OPTIONS]

        self._build_question_page('passion', question, options,
                                  self._on_passion_selected)

    def _on_passion_selected(self, btn, key):
        """User selected their passion."""
        result = self.session.advance(action='answer', data={'key': key})

        # Show acknowledgment
        ack = ACKNOWLEDGMENTS_PASSION.get(key, {})
        ack_text = ack.get(self.session.language, ack.get('en', ''))
        self._build_text_page(
            'ack_passion', ack_text, 'ack-text',
            auto_advance_ms=3000,
            next_builder=self._show_escape,
        )

    def _show_escape(self):
        """Show the escape question."""
        # Advance session past ack_passion
        self.session.advance()

        lang = self.session.language
        question = self._line('question_escape')
        options = [{
            'key': e['key'],
            'label': e['labels'].get(lang, e['labels']['en']),
        } for e in ESCAPE_OPTIONS]

        self._build_question_page('escape', question, options,
                                  self._on_escape_selected)

    def _on_escape_selected(self, btn, key):
        """User selected their escape."""
        result = self.session.advance(action='answer', data={'key': key})

        # Show escape acknowledgment
        ack_text = ACKNOWLEDGMENT_ESCAPE.get(
            self.session.language, ACKNOWLEDGMENT_ESCAPE['en'])
        self._build_text_page(
            'ack_escape', ack_text, 'ack-text',
            auto_advance_ms=3000,
            next_builder=self._show_pre_reveal,
        )

    def _show_pre_reveal(self):
        """'I think I know you.'"""
        # Advance session past ack_escape
        self.session.advance()

        pre_text = self._line('pre_reveal')
        self._build_text_page(
            'pre_reveal', pre_text, 'pre-reveal',
            auto_advance_ms=4000,
            next_builder=self._show_reveal,
        )

    def _show_reveal(self):
        """Generate and reveal the name."""
        # Advance session to do the reveal
        result = self.session.advance()
        self._last_reveal = result
        self._build_reveal_page(result)

    def _on_accept_name(self, btn):
        """Seal the name forever."""
        result = self.session.advance(action='accept_name')
        if result.get('sealed'):
            self._build_sealed_page(result)
        else:
            # Error — show message and retry
            error = result.get('error', 'Something went wrong')
            self._build_text_page(
                'error', error, 'pa-text',
                auto_advance_ms=3000,
                next_builder=self._show_reveal,
            )

    def _on_try_another(self, btn):
        """Generate an alternative name."""
        result = self.session.advance(action='try_another')
        self._last_reveal = result
        # Remove old reveal page and build new one
        old = self.stack.get_child_by_name('reveal')
        if old:
            self.stack.remove(old)
        self._build_reveal_page(result)

    def _on_begin(self, btn):
        """Ceremony complete — close and start the desktop."""
        self.close()

    # ── Utilities ───────────────────────────────────────────────

    def _line(self, key):
        """Get a PA line in the current session language."""
        lines = CONVERSATION_SCRIPT.get(key, {})
        return lines.get(self.session.language, lines.get('en', ''))

    def _schedule(self, ms, callback):
        """Schedule a callback after ms milliseconds."""
        if self._pending_timeout:
            GLib.source_remove(self._pending_timeout)
        self._pending_timeout = GLib.timeout_add(ms, self._run_scheduled, callback)

    def _run_scheduled(self, callback):
        """Run a scheduled callback (returns False to prevent repeat)."""
        self._pending_timeout = None
        callback()
        return False


class HARTOnboardingApp(Adw.Application):
    """GTK4/libadwaita application for the HART onboarding ceremony."""

    def __init__(self, user_id='1'):
        super().__init__(application_id='ai.hartos.onboarding')
        self.user_id = user_id

    def do_activate(self):
        # Check if already onboarded
        if has_hart_name(self.user_id):
            profile = get_hart_profile(self.user_id)
            if profile:
                # Show identity card briefly, then quit
                self._show_identity_card(profile)
                return

        # Run the ceremony
        win = HARTOnboardingWindow(
            user_id=self.user_id,
            application=self,
        )
        win.present()

    def _show_identity_card(self, profile):
        """Show existing HART identity (already onboarded)."""
        win = Adw.ApplicationWindow(application=self)
        win.set_default_size(600, 400)
        win.set_title('Your HART')

        # Load CSS
        provider = Gtk.CssProvider()
        provider.load_from_string(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            spacing=8,
        )
        box.add_css_class('ceremony-bg')

        name = Gtk.Label(label=profile.get('name', ''))
        name.add_css_class('sealed-name')
        box.append(name)

        tag = Gtk.Label(label=profile.get('hart_tag', profile.get('display', '')))
        tag.add_css_class('sealed-tag')
        box.append(tag)

        emoji = Gtk.Label(label=profile.get('emoji_combo', ''))
        emoji.add_css_class('emoji-display')
        box.append(emoji)

        msg = Gtk.Label(label=f"Sealed {profile.get('sealed_at', '')[:10]}")
        msg.add_css_class('sealed-message')
        box.append(msg)

        win.set_content(box)
        win.present()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='HART OS Onboarding')
    parser.add_argument('--user-id', default='1', help='User ID')
    parser.add_argument('--check', action='store_true',
                        help='Check if already onboarded and exit')
    args = parser.parse_args()

    if args.check:
        if has_hart_name(args.user_id):
            profile = get_hart_profile(args.user_id)
            if profile:
                print(f"Onboarded: {profile.get('hart_tag', profile.get('name'))}")
                sys.exit(0)
        print("Not onboarded")
        sys.exit(1)

    app = HARTOnboardingApp(user_id=args.user_id)
    app.run(None)


if __name__ == '__main__':
    main()
