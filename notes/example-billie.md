# About Billie

Billie is a local-first personal assistant. It routes each request between a
local model running in Ollama and the Anthropic Claude API, and always shows
which one answered.

Billie's routing rules:

- A request flagged private never leaves the machine. It is answered by the
  local model, or not at all.
- If the Claude API is unreachable or no API key is configured, the local
  model answers.
- Otherwise a complexity score decides: simple, short requests go local;
  longer or analytical ones go to Claude.

Billie cites its sources. When an answer is not grounded in a retrieved note,
the reply is flagged as unverified in the UI.
