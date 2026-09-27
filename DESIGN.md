# Baxter demo design

## Job

Show what makes Baxter different in one working flow: a request enters through Discord, bounded workers execute a dependency graph, an independent verifier can block the handoff, and the final artifact keeps its receipts.

## Visual system

- A compact Discord-like operations room, because Discord is the actual interface Atul chose for Baxter.
- Charcoal `#292a35`, lavender `#b9a7e7`, verifier green `#8ed1ae` and failure coral `#ef9a94`.
- Segoe UI carries the chat. Monospace is limited to commands, progress and machine states.
- Product manager, developer, verifier and Baxter tasks receive different meowl operator colours.

## Signature

The workflow is a live channel thread. The slash command starts real Web Workers, state changes land on role-specific meowl messages, and the verified report returns as a Baxter attachment in the same conversation.

## Restraint

The ten underlying jobs remain inspectable but live inside one bounded thread. Receipts, source files, failure injection and graph editing stay collapsed until requested. The first screen is the request, the run and the result.

## Motion

Only the currently running job blinks. Button presses scale to `0.96`; reduced motion removes both. State colour and text always carry the same meaning without animation.
