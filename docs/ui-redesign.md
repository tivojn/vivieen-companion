# Vivieen UI redesign — review, design system, and rollback

*2026-07-31 · branch `feat/notion-theme` · rollback tag `pre-redesign-v0.5.0`*

## 1. The review (why it felt 粗糙)

The functions were sound; the *surface* undermined them. Specific findings from a
full pass over every screen:

**Visual noise fought the content.**
- The red accent (`#e2483a`) appeared in ~40 places at full strength: glows,
  gradients, borders, kickers, pills, radial washes behind the body stage. When
  everything is highlighted, nothing is.
- Three-plus levels of "dark gray on darker gray" (`#0b0b0d/#131316/#0e0e11/#111115…`)
  were hard-coded per component instead of drawn from a scale, so panels never
  quite matched.
- Uppercase letter-spaced labels (`.24em` tracking) on *headers, labels, and
  section titles alike* flattened the hierarchy — a page title shouted exactly
  as loudly as a form label.

**Hierarchy was implicit, not designed.** Primary, secondary, and destructive
buttons shared one visual weight. Status colors (ok/warn/bad) used four slightly
different greens/ambers across components.

**No theme system.** Only a heavy dark look; no light option, no tokens, so any
change meant hunting hex codes through 380 lines of CSS.

## 2. What changed (this branch)

A token-driven design system in `web/settings.html`, Notion-quiet:

- **Tokens first**: every color, border, shadow, and radius flows from ~40 CSS
  custom properties on `:root`. The dark theme is *one token swap* on
  `:root[data-theme=dark]` — no component overrides.
- **Light by default** (paper white `#ffffff`, warm gray `#f7f7f5`, ink
  `#37352f`, muted `#787774` — Notion's palette), with a matching Notion-dark
  (`#191919/#202020`). Follows the system scheme on first run; the ◐ button in
  the header toggles and persists to `localStorage` (`vivieen-theme`), resolved
  *before first paint* to avoid flashes.
- **One accent, used sparingly**: brand red survives only on primary buttons,
  active selections, and live progress. Everything else is neutral. Semantic
  ok/warn/bad each get one text/bg/line triple.
- **Real hierarchy**: page sections are sentence-case 14px/600 ink; only micro
  kickers stay uppercase; labels are quiet 12px/500. Radii tightened to
  6/8/10/12 (controls/cards/panels/dialogs), shadows reduced to Notion's
  hairline-plus-soft-drop.
- **Every ID, class name, data attribute, and copy string is unchanged.** Zero
  JS behavior changes beyond the 6-line theme toggle. The whole test suite +
  4 QA scripts pass untouched.

The desktop overlay surfaces (`index.html` chat bar, `bubble.html`) deliberately
keep their dark-glass look — they float over the wallpaper, where translucent
dark reads as "part of the desktop", not part of a document.

## 3. Verified

- `npm test` fully green (unit suite + track/monitor/replay/window-bounds QA).
- Rendered live against the real backend (port 8791, real avatar library):
  light theme screenshot-verified; dark theme, Full Body Studio dialog, Models
  tab, and the ≤860px responsive branch verified by computed-style inspection.

## 4. Rollback (guaranteed, two ways)

The pre-redesign state is permanently recoverable:

```bash
# See the old UI exactly as released:
git checkout pre-redesign-v0.5.0

# Undo the redesign on main while keeping later work:
git revert -m 1 <merge-commit-of-feat/notion-theme>
```

The tag `pre-redesign-v0.5.0` is pushed to GitHub, so the functional v0.5.0 UI
can never be lost.

## 5. Phase 2 candidates (recommended, not yet built)

Deeper information-architecture moves that deserve their own pass (and your
sign-off, since they change layout/flow rather than skin):

1. **Full Body Studio sub-navigation** — the right panel stacks provider → art
   direction → prompt → progress → sets → walk → idle → moves in one long
   scroll; a slim sticky in-panel nav (Body / Walk / Idle / Moves) would cut
   the scrolling.
2. **Gesture discoverability** — a small "How to interact" card (hold head to
   talk, double-tap hair for moves, drag to reposition) on the Avatar tab.
3. **Avatar card action overflow** — 7 buttons per ready card; keep Use/Full
   body primary, fold Export/Delete/Rebuild into a ⋯ menu.
4. **Empty-state onboarding** — first-run walkthrough from portrait → build →
   body → walk in one guided strip.
