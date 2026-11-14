# FINN UI kit (free, MIT-licensed sources)

A curated set of free components for FINN's stack (HTML + Tailwind + Alpine.js). They're **raw building blocks**: restyle everything with the tokens in `docs/UI_BRIEF.md`. Never ship them with their default look.

Put this folder at `vendor/ui-kit/` in the FINN repo.

| Folder | Source | Licence | Notes |
|---|---|---|---|
| `pines/` | Pines UI by DevDojo (github.com/thedevdojo/pines) | MIT | Alpine + Tailwind interactive components. Best fit for our stack |
| `hyperui/` | HyperUI by Mark Mead (github.com/markmead/hyperui) | MIT | Static Tailwind HTML patterns (light + dark variants) |
| `tailadmin/` | TailAdmin free (github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template) | MIT | Tables, badges, alerts, metric cards. `reference-style.css` is for the dark-mode approach only |

Licences are in `licenses/`. Credit all three in the README.

## Where each piece goes in FINN

| FINN element | Start from |
|---|---|
| Bottom sheets (evidence, copilot, window picker, "Why am I seeing this?") | `pines/slide-over.html` (change it to slide up from the bottom) |
| Tabs (Bull/Bear, Research sections) | `pines/tabs.html`, `hyperui/tabs/` |
| Window picker dropdown | `pines/dropdown-menu.html`, custom range with `pines/date-picker.html` |
| Card long-press menu (Mute, Not useful, Acting on this) | `pines/context-menu.html` |
| Toasts ("Marked reviewed", feedback) | `pines/toast.html`, `hyperui/toasts/` |
| Alert threshold sliders | `pines/range-slider.html` |
| Notification / type toggles | `pines/switch.html`, `hyperui/toggles/` |
| Verdict and type chips, confidence badges | `pines/badge.html`, `hyperui/badges/`, `tailadmin/badges/` |
| Results 4×3 table, promise ledger | `hyperui/tables/`, `tailadmin/basic-tables/`, `pines/table.html` |
| Pulse tiles, credibility stat | `hyperui/stats/`, `tailadmin/ecommerce/` (metric cards) |
| Research progress stepper | `hyperui/steps/`, `pines/progress.html` |
| Thesis status timeline, journal | `hyperui/timelines/` |
| Skeleton and loading states | `hyperui/loaders/` |
| Empty states ("You're caught up", "No alerts") | `hyperui/empty-states/` |
| Evidence list, filing details | `hyperui/details-list/` |
| Copy citation / share | `pines/copy-to-clipboard.html` |
| Tooltips on metrics (EBITDA, OCF/PAT) | `pines/tooltip.html`, `pines/popover.html` |
| Copilot command palette (optional) | `pines/command.html` |

## Rules
- Restyle with FINN tokens (colours, Inter + IBM Plex Mono, 8pt spacing, radius 14, 1px borders, no shadows or gradients).
- Don't use admin layouts (sidebars, top navbars, marketing heroes). FINN is a mobile inbox with bottom tabs.
- Charts stay as inline SVG, styled after Tremor (thin lines, muted grid, direct labels).
