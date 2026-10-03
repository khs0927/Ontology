# NVIDIA Cosmos Reason2 + Google Drive visual validation

This extension adds a **read-only visual evidence lane** to Drive-backed ingestion using `nvidia/cosmos-reason2-2b`.

## Official Reason2 2B runtime

NVIDIA's current Reason2 NIM documentation serves the 2B model from an OpenAI-compatible endpoint such as:

```text
http://127.0.0.1:8000/v1/chat/completions
```

The NIM container is pulled/launched with `NGC_API_KEY`. Local inference itself does not need a bearer token. For a remote private NIM that is protected by bearer auth, set `NVIDIA_API_KEY`.

```bash
export NVIDIA_COSMOS_ENDPOINT="http://127.0.0.1:8000/v1/chat/completions"
export NVIDIA_COSMOS_MODEL="nvidia/cosmos-reason2-2b"
# Optional for an authenticated remote NIM:
export NVIDIA_API_KEY="..."
pip install -e ".[vision]"
```

## Drive-first flow

```text
Google Drive source
  -> immutable source upload/materialization
  -> authoritative parser (DXF/IFC/PDF/...)
  -> visual representation
       PDF: first-page raster
       SVG: rasterized
       DXF/DWG: generated parser preview -> PNG
  -> Cosmos Reason2 2B
  -> VisualObservation / CandidateObject JSON
  -> parser/CAIR cross-check
  -> 11_VALIDATION/visual/
  -> Drive sync
```

The visual report is **non-canonical**. It never rewrites CAIR classifications. Objects and relations are candidates that can be reconciled against parser geometry, handles, provenance, and CAIR relations.

## Tools

- `aec.visual_inspect_artifact`: inspect one local visual artifact or an ingested CAD/PDF source.
- `aec.visual_validate_drive_project`: materialize a bounded set of visual Drive artifacts and create advisory reports.

The Drive batch is bounded by `limit` to control inference traffic. Original DWG/DXF bytes are not sent through the visual lane; only the rasterized review view is sent. Model `<think>` traces are discarded and only final structured evidence is persisted.
