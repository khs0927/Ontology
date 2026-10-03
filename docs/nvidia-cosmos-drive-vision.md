# NVIDIA Cosmos + Google Drive visual validation

This extension adds a **read-only visual evidence lane** to Drive-backed ingestion.

## Configuration

```bash
export NVIDIA_API_KEY="nvapi-..."
export NVIDIA_COSMOS_ENDPOINT="https://integrate.api.nvidia.com/v1/chat/completions"
export NVIDIA_COSMOS_MODEL="nvidia/cosmos-reason2-2b"
pip install -e ".[vision]"
```

The model ID and endpoint are configurable so the same contract can target the NVIDIA hosted catalog or a self-hosted NIM.

## Automatic Drive-first flow

When `aec.ingest_file` runs with an injected Google Drive client, visual validation is enabled by default after authoritative parsing and before derived artifacts are synchronized back to Drive.

```text
Drive source
  -> immutable source upload
  -> authoritative parser (DXF/IFC/PDF/...)
  -> local visual representation
       PDF: first-page raster
       SVG: rasterized
       DXF/DWG: generated preview SVG -> PNG
  -> NVIDIA Cosmos Reason
  -> VisualObservation / CandidateObject JSON
  -> 11_VALIDATION/visual/
  -> Drive sync
```

The visual report is **non-canonical**. It does not rewrite CAIR classifications. Its objects and relations are candidates that can later be reconciled against parser geometry, handles, provenance and CAIR relations.

## Tools

- `aec.visual_inspect_artifact`: inspect one local visual artifact or an ingested CAD/PDF source.
- `aec.visual_validate_drive_project`: materialize visual files from one Drive project and create bounded visual-observation reports.

The Drive batch defaults to a bounded number of files to control API egress/cost.

## Data governance

Only the rasterized page/view used for inspection is sent to the configured NVIDIA endpoint. Original DWG/DXF bytes and canonical CAIR are not sent by this visual lane. Model `<think>` traces are discarded; only final structured evidence is persisted.
