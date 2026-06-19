# CLAUDE.md

Guía para Claude Code en este repositorio (bot de trading "HyperGrinder v20").

## Memoria del proyecto (versionada)

La memoria del proyecto vive en **`docs/memory/`** y está bajo control de versiones para
máxima trazabilidad y para que sea idéntica en cualquier máquina con un `git pull`.

- **Índice:** [`docs/memory/MEMORY.md`](docs/memory/MEMORY.md) — una línea por memoria. Léelo al inicio.
- **Detalle:** cada `docs/memory/<slug>.md` contiene un hecho con frontmatter (`name`, `description`, `metadata.type`).

### Convención al guardar memoria nueva

`docs/memory/` es la **fuente canónica**. Al crear o actualizar una memoria del proyecto:

1. Escribe el archivo en `docs/memory/` (no solo en el store local `~/.claude/.../memory/`).
2. Añade su pointer de una línea en `docs/memory/MEMORY.md`.
3. Commitea el cambio para que viaje por git a las demás PCs.

El store local de auto-memoria (`~/.claude/projects/.../memory/`) es por-máquina y NO se
versiona; trátalo como espejo de esta PC. Ante divergencia, gana `docs/memory/`.

## Convenciones de trabajo

Ver [`docs/memory/workflow-implement-then-commit.md`](docs/memory/workflow-implement-then-commit.md):
cambios del bot se implementan → commit → push directo; no correr backtest salvo que se pida
(el arnés es bar-close, no testea spikes intra-vela).
