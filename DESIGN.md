# CloseTheBooks: Interface System

## Direction: ledger sheet

Calm paper, graphite ink, hairline rules, and one bank-stamp blue accent. The
interface should feel like a well-kept ledger, not a SaaS dashboard: strong
type hierarchy, restrained status colour, borders-only depth, and no
decorative cards. Visual order everywhere is book health, then the next
action, then account evidence, then financial context.

The signature element is the **coverage strip**: twelve small cells per
account, one per month. A filled cell with a tick is verified by a statement
balance, an outlined cell is imported but not verified, a hatched red outline
is missing, and a sunken cell predates the account's first activity. It
appears on Overview and Accounts and always comes with a text key.

Fava remains the detailed report surface under `/reports/`. Product pages do
not imitate Fava, and Fava is not restyled.

## Accessibility target

WCAG 2.2 AA. Every control works by keyboard and has a visible
`:focus-visible` ring in the accent colour. Status colour always comes with a
text label. Targets are at least 44 px. Dynamic states (uploads, Ask progress,
flash messages) use live regions. Respect `prefers-reduced-motion`. Pages
survive 320 px widths and long names. Wide tables become labelled rows below
640 px.

## Tokens (`bookkeeping/app/static/app.css`)

| Role | Light | Dark |
| --- | --- | --- |
| `--paper` page | `#faf9f6` | `#141618` |
| `--paper-raised` dropzones, hand-off panels | `#fffefb` | `#191c1f` |
| `--paper-sunken` inputs, code, prompt chips | `#f2f0ea` | `#0f1113` |
| `--ink` / `--ink-2` / `--ink-3` / `--ink-4` | primary, supporting, metadata, muted | same hierarchy, inverted |
| `--rule` / `--rule-2` / `--rule-3` | hairline, separation, section rule | same at lower alpha |
| `--stamp` accent: links, primary buttons, focus, chart line | `#2a4a8a` | `#93b1ee` |
| `--tick` current, verified, paid | `#2b6b40` | `#74c08b` |
| `--ochre` due, open, needs attention | `#855600` | `#e0ae4c` |
| `--red-ink` overdue, blocked, errors | `#a3261c` | `#f08a80` |

Dark mode follows `prefers-color-scheme`. Status colours are reserved for
status. Never use them as decoration or as a series colour.

Spacing is a 4 px base (`--s1` to `--s12`). Radius is 4 px for controls and
6 px for surfaces. Content width is 1080 px, and Ask is 760 px.

## Typography

- UI and body: the system sans stack, 15 px, tabular numerals throughout.
- Page titles and the health state: a system serif (Iowan Old Style, Charter,
  Georgia), weight 500. Use it only there.
- Section labels: 13 px uppercase, 0.06em tracking, `--ink-2`.
- Monospace (`--font-mono`) only for ledger account names, file names, queries,
  and agent prompts.

## Components

- **Masthead.** The company name (serif) and product name, with top
  navigation. The current page has an ink underline and `aria-current`. On
  mobile the nav wraps; no item is hidden.
- **Health block.** A seal mark, the state word (Current, Needs attention,
  Blocked, or Incomplete), a one-sentence reason, and three facts: imported
  through, verified through, and ledger validity.
- **Next actions.** A numbered list, most urgent first. The first item is
  larger. A statement action carries an account-specific dropzone that also
  works as a plain form. Actions that need the agent show a prompt chip, the
  exact words with a Copy button.
- **Prompt chip.** "Ask your coding agent" plus the quoted prompt in
  monospace. This is the hand-off between the app and the agent.
- **Status.** A dot plus a short label. Never colour alone.
- **Financial snapshot.** One ruled row of six figures, each with a basis
  line (period or "As of today") and its currency. Period choice is a link
  group, so the URL holds the state.
- **Cash chart.** A single 2 px accent line with a 9% area wash, hairline
  grid, labelled end value, crosshair tooltip, and a "Show as table"
  disclosure. No legend, since there is one series.
- **Forms.** Labels above sunken inputs, hints below, errors in a red-ruled
  block at the top with `role="alert"`. Primary and quiet buttons sit in a
  ruled action row.

## Content rules

- Sentence case everywhere. Full dates where recency matters ("Oct 14, 2026").
- State the period and currency beside every financial value.
- Start errors with the outcome, then the recovery step.
- Reserve "verified" for a statement-backed balance assertion or a
  deterministic check. Never call stale imported data current.
- The app never says it posted something. It says what it saved or prepared,
  and names the review step.

## Exclusions

No KPI card grids, gradients, illustrations, celebratory states, or motion
beyond 120 ms state transitions. No web fonts, so the app stays fully offline.
No company names or branding in the product. The workspace supplies them.
