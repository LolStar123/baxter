# Baxter workflow room

## Product and direction

The audience is a developer inspecting how a Discord-first coordinator turns an order file into a verified handoff. The primary action is Run workflow. The visual reference is the repository's actual request/worker/proof model and Discord's channel grammar: scope at left, a chronological run thread in the centre, run files at right. No simulated server controls or invented conversations.

The previous narrow showcase hid input and proof behind disclosures. This redesign fills the browser with a working room. The signature is a connected execution thread that ends in a report only when every declared job passes. Keep the attachment, run button and artifact selector visible.

## System

- Page `#17151c`; scope `#201d26`; channel `#26222d`; file rail `#1d1923`.
- Text `#f0edf5`; secondary text `#b1a7be`; line `#423a4e`.
- Plum action `#c4aceb`; passed `#a2d7bb`; failed/blocked `#f0aaa6`. States also have explicit text. Secondary text exceeds 4.5:1 against the darkest and channel surfaces.
- Segoe UI / Arial system stack for messages and controls. System monospace for filenames, durations and state labels. No downloaded fonts, font requests or redistributed font files.
- Type: 19-20 px headings, 12-14 px messages, 10-11 px supporting text, 9 px state labels. A 32 px lettermark and small CSS operator glyphs belong to the actual roles.
- Desktop columns: 218 px scope, flexible channel, 326 px proof. Maximum canvas 1800 px. Spacing uses 8, 12, 18, 22 and 28 px steps; 7 px controls nest inside 9-11 px panels.
- Below 950 px, proof moves below the channel. Below 600 px, scope becomes a compact channel header and content stacks. The audit checks a 390 px viewport without document overflow.

## Interaction and truth

CSV replacement is an actual file input. It updates the worker input and clears old receipts. Source metadata uses the loaded row count, filename and synthetic/uploaded origin. Capacity, file editor, failures and graph editing are disabled during execution. A failed dependency blocks downstream tasks; a generated but unverified report is never shown as a verified handoff.

Artifacts and receipts are two views of the same run. Editing a generated file is unavailable; editing an input requires Save input. Export contains the complete execution record. Loading failures disable actions and provide a refresh instruction. All text comes from local executable work or explicit fixture scope.

Focus rings use the plum token with a 4 px offset. Buttons have hover and disabled states. The CSV input retains a visible focus ring through its label. Reduced motion removes transitions and smooth scrolling. No blinking statuses or decorative motion.

## Acceptance and review

Frozen checklist: actual upload changes output; ten original jobs reconcile 220 unique orders; a negative quantity blocks the handoff; restore recovers; invalid definitions explain failure; tabs survive repeated clicks; exports contain proof; keyboard can run; failed fixture fetch disables controls; desktop/mobile screenshots load without page errors or overflow.

Solo rendered review inspected `examples/portfolio/preview.png` and ignored `output/playwright/mobile.png`, plus failure behavior. These are application screenshots, not mockups. No raster illustration or external asset dependency is needed.
