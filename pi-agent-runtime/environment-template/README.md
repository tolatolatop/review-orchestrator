# Prebuilt Task environment

This directory is the default clonable environment template. Deployments may
mount a prepared directory here with `node_modules`, build tools, and packaged
Skills. The Runtime clones it into a per-Task overlay and never modifies the
template itself.
