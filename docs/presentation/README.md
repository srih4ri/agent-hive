# Hive team introduction

Open `index.html` directly in a browser. It is self-contained and requires no server, network, build step, or third-party assets. External repository links need network access.

Five slides: four presentation sections timed to five minutes, followed by one to two minutes of Q&A. Use arrow keys to navigate, **D** to advance the three-window illustrative demo on slide 2 (press again during playback to finish the step immediately), **N** for speaker notes, and **F** for fullscreen. Notes appear on the same screen; close them before screen sharing. Hash links such as `index.html#3` open a specific slide. Browser printing renders all slides, including all illustrative demo messages.

## Before presenting

- Rehearse once with the notes open. Timings: takeaways 0:45, demo 1:45, setup 1:10, architecture 1:20.
- Choose either the built-in illustrative rehearsal or a live exchange. The deck never connects to Hive or sends messages. Its terminal and Element windows animate fictional messages; Reset cancels playback. Reduced-motion preferences skip typing animation.
- For a live demo, connect two sessions with distinct identities, `frontend` and `backend`, and open their conversation in Element. Verify delivery, including while idle.
- Give `backend` this demo fixture: “The orders API response adds deliveryDate, nullable. Suggested empty state: Not scheduled.”
- Ask `frontend`: “Ask backend what changed in the API and wait for its reply.” Show the exchange in Element, then send: “Use Date to be confirmed for the empty state.”
- Keep the on-slide rehearsal available if the live exchange takes too long.

## Content sources

The deck describes the repository implementation rather than promising support in arbitrary host versions. Setup prompts are suggested instructions for the user's agent; Hive has no dedicated automatic updater.

- [README](../../README.md): installation, prerequisites, client setup, tools, observer and local services.
- [Agent guide](../../AGENT_GUIDE.md): identities, message delivery, bulletin behavior and message economy.
- [Claude notes](../../CLAUDE.md): native inbox socket support and process version behavior.
- [Codex delivery](../codex-delivery.md): app-server integration, idle delivery and hook fallback.
- [LAN access](../lan-access.md): access from another device.

## GitHub Pages

The `presentation` workflow publishes only `index.html` at the Pages site root. Enable Pages with **GitHub Actions** as the source in repository settings, then run the workflow manually. For automatic deployment on deck changes, set the repository Actions variable `PAGES_ENABLED` to `true`. Private repositories require a GitHub plan that supports Pages for private repositories. The workflow cannot deploy until Pages is enabled.
