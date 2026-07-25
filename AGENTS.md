<!-- engram:start -->
## Project memory (engram)

This repo uses [engram](https://github.com/Victorchatter/engramkit) for cross-agent memory. Memories live in `.engram/` as plain, git-diffable markdown.

- **Before starting work** — call the `recall` MCP tool (or run `engramkit recall "<query>"`) to surface relevant past decisions, fixes, and context.
- **After a decision, root-cause, or established preference** — call the `remember` MCP tool (or run `engramkit add "<content>" --type <type>`) so future sessions inherit it.

Memory types: `decision` | `fix` | `preference` | `context`.
<!-- engram:end -->
