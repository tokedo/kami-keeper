# keeper/data

Catalog data the chain-backed HP fallback (`keeper/chainhp.py`) reads.
Chain facts and catalogs, never code.

| file | what | source |
|---|---|---|
| `nodes.csv` | node index -> `Affinity` column (the node's harvest affinity, upper-cased by the loader; `Normal`, `Eerie`, `Scrap`, `Insect`), what `harvest_efficacy` needs to project a harvesting kami's HP from its last sync | the game's node table (index, name, affinity); refreshed by hand when the game adds or changes a node |

Refresh when the catalog changes (a new node, a changed affinity): the
projection for a kami on an unlisted node treats the node as `NORMAL`
and the tick summary says so (`nodeAffinityUnknown`).
