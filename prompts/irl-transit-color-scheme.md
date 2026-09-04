# IRL Transit — Color & Type System
### Handoff brief for Claude Code (React + Capacitor.js)

Reference mockup: `irl-transit-color-type-system.html` — both light and dark phone
screens side by side, plus raw swatches/type specimen.

Since Capacitor wraps a regular web app (your React app) in a native shell rather than
compiling to native views, this is standard CSS/web tooling — not React Native
primitives. The one thing that's genuinely Capacitor-specific is status bar / safe-area
handling, covered in section 5.

---

## 1. Direction, in one paragraph

Clean and fun, built for daily glanceable use in both light and dark. Green/yellow/blue
stay reserved as **functional line colors** (they already mean "which route/system" —
don't reuse them decoratively). One new **coral accent** carries everything else: live
pulse indicators, primary emphasis, delay states. Fredoka carries personality (app name,
headers, big countdown numbers); Inter carries density (lists, station names, metadata).

---

## 2. Color tokens

Add as CSS custom properties, e.g. `src/styles/theme.css`, loaded once at the app root.

```css
:root {
  --bg: #F5F7F1;
  --surface: #FFFFFF;
  --surface-raised: #FFFFFF;
  --hairline: #E4E7DE;

  --ink: #16241C;
  --ink-secondary: #5B6B60;
  --ink-muted: #8B978C;

  --coral: #FF5A4E;
  --coral-ink: #B23226;
  --coral-tint: #FFE7E3;

  --line-green: #2FAE66;
  --line-green-tint: #E3F5EA;
  --line-yellow: #E8940C;
  --line-yellow-tint: #FCEFD9;
  --line-blue: #2F8FE0;
  --line-blue-tint: #E4F1FC;
}

[data-theme="dark"] {
  --bg: #10201A;
  --surface: #17281F;
  --surface-raised: #1D3226;
  --hairline: #24392C;

  --ink: #EDF3EC;
  --ink-secondary: #A9BBAE;
  --ink-muted: #74897B;

  --coral: #FF7A63;
  --coral-ink: #FFD9CF;
  --coral-tint: #33261F;

  --line-green: #4BC685;
  --line-green-tint: #1D3327;
  --line-yellow: #F5B93E;
  --line-yellow-tint: #362D18;
  --line-blue: #5CACEF;
  --line-blue-tint: #1B2E3F;
}
```

**Wiring dark mode:** set `data-theme="dark"` on `<html>` based on
`window.matchMedia('(prefers-color-scheme: dark)')`, with a manual override stored in
`localStorage` if you want an in-app toggle rather than only following system setting:

```ts
const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const saved = localStorage.getItem('theme'); // 'light' | 'dark' | null
document.documentElement.dataset.theme = saved ?? (prefersDark ? 'dark' : 'light');
```

**Contrast note:** `--coral-ink` on `--coral-tint` and `--ink` on `--bg` are both tuned
for AA contrast in their respective modes. Don't use `--coral` itself as small text
color on light backgrounds — it's built for icon/pulse/CTA fills, not body text.

---

## 3. Typography

```css
@import url('https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&family=Inter:wght@400;500;600;700&display=swap');

:root {
  --font-display: 'Fredoka', system-ui, sans-serif;
  --font-body: 'Inter', system-ui, sans-serif;
}
```

For production, self-host both font files (via `@fontsource/fredoka` and
`@fontsource/inter` npm packages) rather than relying on the Google Fonts CDN at
runtime — meaningfully faster first paint inside the Capacitor webview, especially
offline/poor-connectivity, which matters for a transit app.

```bash
npm install @fontsource/fredoka @fontsource/inter
```

```ts
// main entry file
import '@fontsource/fredoka/500.css';
import '@fontsource/fredoka/600.css';
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
```

**Tabular numerals for times:** apply `font-variant-numeric: tabular-nums;` to any
element showing a countdown/time so digits don't jitter as they update.

Type scale:
- App name / large countdown: 24–52px, `--font-display`, weight 600
- Section headers: 13px, `--font-body`, weight 600, `--ink-secondary`
- List primary (destination name): 15px, `--font-body`, weight 600
- List secondary (platform/meta): 12.5px, `--font-body`, weight 400, `--ink-muted`
- Departure time: 15px, `--font-body`, weight 600, tabular nums

---

## 4. Component patterns

**Line badge** — 38×38px, `border-radius: 12px`, background = `--line-{color}-tint`,
text/icon color = `--line-{color}`, label in `--font-display` (one of the few places
Fredoka appears outside headers — reads well at small size for 2–3 letter codes).

**Hero/next-departure card** — `--surface-raised` background, `border-radius: 20px`, no
border. Pulse dot: 8px circle in `--coral`, with a second larger semi-transparent ring
behind it, animated with a CSS `@keyframes` scale/opacity pulse loop.

**Departure list rows** — no card wrapper per row; `1px solid var(--hairline)` bottom
border between rows instead (avoid the identical-rounded-card-per-item look). Delay
state: `--coral-ink` on the time text plus a small `+N delay` caption underneath in
`--ink-muted`.

**Bottom nav** — `--surface` background, `1px solid var(--hairline)` top border, active
tab icon + label in `--coral`, inactive in `--ink-muted` at reduced opacity. Remember
this sits above the iOS home-indicator safe area — see below.

---

## 5. Capacitor-specific notes

**Status bar color** — the native status bar doesn't automatically follow your CSS
theme. Use the `@capacitor/status-bar` plugin to set it explicitly whenever the theme
changes:

```ts
import { StatusBar, Style } from '@capacitor/status-bar';

async function syncStatusBar(isDark: boolean) {
  await StatusBar.setStyle({ style: isDark ? Style.Dark : Style.Light });
  await StatusBar.setBackgroundColor({ color: isDark ? '#10201A' : '#F5F7F1' }); // Android only
}
```

**Safe areas** — use `env(safe-area-inset-*)` in CSS for the bottom nav and any
full-bleed header, so content doesn't sit under the iOS notch/home-indicator or Android
gesture bar:

```css
.bottom-nav {
  padding-bottom: calc(14px + env(safe-area-inset-bottom));
}
```

Requires `<meta name="viewport" content="viewport-fit=cover">` in `index.html`, which
most Capacitor starter templates already include — verify it's there.

**Splash screen / app icon** — the coral accent and dark-mode background pair well as a
native splash screen background; use `@capacitor/splash-screen` config in
`capacitor.config.ts` to set it, rather than a plain white flash before your JS loads.

---

## 6. What NOT to carry over

- The HTML/CSS in the reference mockup file itself — it's a reference render, not app code
- The placeholder square nav icons — swap in your real icon set (suggest a rounded icon
  family to match Fredoka's warmth, e.g. Phosphor Icons "duotone" or "fill" style)
- Any inline `<svg>` markup for the logo — use your existing exported logo assets

---

## 7. Suggested prompt for Claude Code

> Apply the design system in `irl-transit-color-tokens.md` to this React + Capacitor
> app. Add the CSS custom properties from section 2 as a theme stylesheet loaded at the
> app root, wire up light/dark mode via `prefers-color-scheme` with a `data-theme`
> override, self-host the two font families via `@fontsource`, and restyle the existing
> departure list, hero card, and bottom nav to match — don't change data-fetching logic,
> only styling. Also wire up `@capacitor/status-bar` per section 5 so the native status
> bar follows the theme. Flag anywhere the spec is ambiguous rather than guessing.