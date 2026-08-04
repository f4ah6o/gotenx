---
description: Validate Gotenx agent CLI adapters without making model calls.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*)
---

Run the operational preflight:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" doctor
```

Report each required adapter's resolved executable, version, and failure reason.
Do not proceed to a paid live run when `ok` is false. Adapter overrides are read
from the current project's `.gotenx/adapters.json`.
