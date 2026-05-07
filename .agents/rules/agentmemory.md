---
trigger: always_on
---

Core Memory Directive:

You have access to the agentmemory toolset.

Self-Correction: Before answering a prompt, silently use search_memory to see if we've already defined variables, logic, or project goals in previous sessions.

Persistence: At the end of every successful task (e.g., "I finished the LSTM model"), use save_memory to log the final state and any "lessons learned" so we don't repeat mistakes.

Privacy: Keep memories scoped to this workspace directory.