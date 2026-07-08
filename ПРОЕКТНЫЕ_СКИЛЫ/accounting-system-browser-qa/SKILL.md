---
name: accounting-system-browser-qa
description: Browser QA workflow for the Russian Django accounting-system project. Use after UI, template, CSS, JavaScript, form, admin, report, navigation, responsive, Figma-to-code, or visual changes to verify the running local app in the browser, including desktop and mobile viewports, console errors, HTTP status, layout overflow, text overlap, and basic interaction paths.
---

# Accounting System Browser QA

Use this skill after interface changes and before telling the user the UI is ready.

## Server Rule

Use the project standard workflow:

- start: `START_SERVER_MVP.bat`;
- stop: `STOP_SERVER_MVP.bat`;
- do not launch manually through global `python`, `py`, or ad hoc `python manage.py runserver`.

If the server is already running, identify whether it belongs to this project before reusing it.

## Minimum Verification

Verify the changed screen in a real browser target:

- HTTP status is successful;
- page title or key text matches the intended screen;
- no obvious server error page;
- no console errors relevant to the changed feature;
- primary controls are visible;
- changed action can be clicked or submitted when safe;
- responsive layout works for desktop and mobile-sized viewport;
- no horizontal overflow on mobile;
- text does not overlap buttons, cards, fields, or table controls.

## Viewports

Use at least:

- desktop: around `1280px` wide;
- mobile: around `390px` wide.

Add tablet width when the screen is likely used on tablets in the quarry or dispatcher room.

## What To Inspect

For forms:

- required fields;
- validation messages;
- save/cancel placement;
- upload controls;
- success/error feedback.

For tables:

- filters;
- sort/search entry points;
- empty state;
- long text;
- narrow viewport behavior.

For cards/dashboards:

- status colors;
- click targets;
- numbers and units;
- loading/empty states;
- whether cards look like actions when they are actions.

## Reporting

In the final answer, include only observed facts:

- server start method;
- URLs checked;
- desktop/mobile result;
- tests or Django checks run;
- remaining risk if a path could not be exercised.

Stop the server when the user is leaving the chat or when the task only required a temporary QA run.
