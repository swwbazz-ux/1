---
name: accounting-system-ux-review
description: UX review workflow for the Russian accounting-system project. Use when Codex designs, reviews, changes, or critiques role-based workplace interfaces for dispatcher, driver, excavator operator, mining master, administrator, management dashboards, reports, shift workflows, mobile/PWA screens, forms, cards, tables, filters, and operational UI ergonomics.
---

# Accounting System UX Review

Use this skill before and after changing user-facing interfaces in this project.

## Project Rule

Treat every screen as a workplace, not as a marketing page. The UI must help a tired worker complete a real shift task quickly, with low ambiguity and few actions.

## Start With The Role

Identify the primary role before proposing UI:

- driver: fast mobile input, current shift, assigned truck, fuel, mileage, trips, simple confirmations;
- excavator operator: equipment state, loading rhythm, downtime signals, shift context;
- mining master: placement, deviations, shift control, operational decisions;
- dispatcher: dense desktop control, reports, corrections, exceptions, exports;
- administrator: employees, access, directories, audit, photos, data cleanup;
- management: compact status, risks, daily totals, trend and drill-down entry points.

If several roles share a screen, separate primary action, secondary checks, and management-only information.

## UX Checklist

Before implementation, decide:

- What is the one job of this screen?
- What must be visible without scrolling?
- What can be hidden behind detail, filter, or drill-down?
- What is the fastest normal path?
- What happens when data is missing, late, suspicious, or conflicting?
- What device is primary: phone, tablet, desktop, or mixed?
- Which actions are dangerous enough to need confirmation?

## Interface Rules

- Prefer concrete operational labels over abstract system words.
- Use large touch targets for field roles and denser tables for dispatcher/admin roles.
- Keep primary actions visually obvious and close to the data they affect.
- Avoid extra confirmation dialogs unless the action destroys data, closes a shift, submits a final report, or changes access.
- Use status color consistently: ok, warning, critical, blocked, draft, pending.
- Show timestamps and responsible role when the user needs trust in the data.
- Do not put long explanatory text into the interface when a better control or label can solve it.

## Review Output

When reviewing or proposing UI, answer in this order:

1. Role and real task.
2. Main UX risk.
3. Specific layout/control changes.
4. What to verify in browser.

Keep recommendations actionable and tied to files or screens when possible.
