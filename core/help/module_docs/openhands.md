# OpenHands

OpenHands is the autonomous AI software-development agent in your stack. You
describe a coding task in plain language; the agent plans it, writes and edits
code, runs commands and iterates — all inside a sandboxed runtime container that
OpenHands spawns per task through the Docker socket, so its work stays isolated
from the host.

## How to reach it

Open [https://openhands.<domain>](https://openhands.<domain>) and sign in with
your razzfazz.ai single sign-on.

## First steps

1. Start a **new conversation** and describe the task (for example, "add a unit
   test for module X" or "scaffold a small Flask endpoint").
2. The agent uses the local models served by GPUStack
   (`https://llm.<domain>`) — a capable coding model is selected by default, so
   no external API key is needed.
3. Watch the agent work in the built-in editor/terminal view; review its diffs
   and approve or redirect as it goes.
4. Connect a repository (or work in the sandbox workspace) to have the agent
   make changes against real code.

## Full upstream documentation

For the complete usage guide, configuration and agent internals, see the
official OpenHands documentation: [https://docs.openhands.dev/](https://docs.openhands.dev/)
