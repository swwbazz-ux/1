---
name: accounting-system-design-system
description: Design-system workflow for the Russian accounting-system project. Use when Codex creates, refactors, reviews, or aligns UI components, visual style, colors, typography, spacing, cards, tables, forms, buttons, navigation, role dashboards, status chips, admin screens, Figma layouts, or code-to-Figma/Figma-to-code work for this project.
---

# Accounting System Design System

Use this skill whenever interface work risks creating a one-off visual style.

## Goal

Keep all workplace screens visually consistent, restrained, and operational. The system should feel like a serious production tool for repeated daily use.

## Foundations

Use a neutral base with functional accents. Avoid decorative gradients, oversized hero layouts, and card-heavy marketing composition.

Recommended semantic groups:

- primary action: one main accent per screen;
- success/normal: completed, active, accepted;
- warning: missing data, pending attention, unusual value;
- danger: destructive action, conflict, blocked state;
- neutral: inactive, archive, metadata.

## Components To Reuse

Prefer existing local patterns before inventing a new component:

- top navigation and role-aware entry points;
- status cards;
- dense admin tables;
- filter rows;
- form sections;
- employee/equipment cards;
- photo upload block;
- modal dialogs for focused actions;
- export/report controls.

## Layout Rules

- Desktop dispatcher/admin screens may be dense, but must remain scannable.
- Mobile field screens must prioritize large controls, short labels, and one-column flow.
- Cards are for repeated items or compact summaries, not for wrapping whole page sections.
- Tables need clear empty state, loading state, and overflow behavior.
- Forms need grouped fields, predictable save/cancel placement, and visible validation.
- Status colors and labels must mean the same thing across screens.

## Figma And Code

When working with Figma:

- create foundations first: colors, text styles, spacing, core components;
- use auto-layout and reusable components;
- do not hardcode every screen differently;
- after Figma changes, define what must be mirrored in code.

When working in code:

- inspect existing CSS/templates/components first;
- extend established classes when practical;
- avoid adding a new visual language for one screen;
- verify responsive behavior after changes.

## Review Output

For design-system work, report:

1. Existing pattern reused or extended.
2. New component/style added, if any.
3. Consistency risks.
4. Browser/Figma verification performed or still needed.
