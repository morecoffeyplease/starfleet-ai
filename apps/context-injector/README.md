# Context Injector compatibility path

Context Injector was renamed to Pattern Buffer.
The maintained app, guide, source, tests and documentation now live in [apps/pattern-buffer](../pattern-buffer/README.md).

The old CLI path remains available as a source-checkout compatibility shim:

```sh
python3 apps/context-injector/context_injector.py guide --print
```

It delegates to the Pattern Buffer source while preserving the existing `.context-injector` worktree files, Git metadata, hook markers, installed `context_injector_{harness}.py` hook commands, and task identifiers.
The old installed runtime location remains supported by the Pattern Buffer installer.
