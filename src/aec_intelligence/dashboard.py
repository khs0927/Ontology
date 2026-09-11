"""Source-backed, self-contained repository status dashboard artifact."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from typing import Any

from .validation_engine import validate_repository


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_dashboard_data(repository_root: str | Path) -> dict[str, Any]:
    root = Path(repository_root).resolve()
    global_root = root / "global" / "00_GLOBAL"
    projects = _jsonl(global_root / "global-project-registry.jsonl")
    artifacts = _jsonl(global_root / "global-artifact-registry.jsonl")
    objects = _jsonl(global_root / "global-object-registry.jsonl")
    validation = validate_repository(root)
    reports_by_project = {report.get("project_id"): report for report in validation.checks.get("project_reports", [])}
    project_rows = []
    for project in projects:
        project_id = str(project.get("project_id"))
        project_objects = [row for row in objects if row.get("project_id") == project_id]
        project_artifacts = [row for row in artifacts if row.get("project_id") == project_id]
        report = reports_by_project.get(project_id, {})
        manifest_path = root / "projects" / project_id / "00_MANIFEST" / "project-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        type_counts = Counter(str(row.get("type")) for row in project_objects)
        project_rows.append({
            "project_id": project_id,
            "name": project.get("name", manifest.get("name", project_id)),
            "status": project.get("status", manifest.get("status", "UNKNOWN")),
            "object_count": len(project_objects),
            "artifact_count": len(project_artifacts),
            "validation_status": report.get("status", "NOT_VALIDATED"),
            "latest_iteration": manifest.get("latest_iteration"),
            "type_counts": dict(sorted(type_counts.items())),
        })
    type_counts = Counter(str(row.get("type")) for row in objects)
    confidence_values = [float((row.get("classification") or {}).get("confidence", 0.0)) for row in objects]
    return {
        "schema_version": "0.1.0",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": validation.status,
        "metrics": {
            "project_count": len(projects),
            "artifact_count": len(artifacts),
            "object_count": len(objects),
            "validated_project_count": sum(row.get("validation_status") == "SUCCESS" for row in project_rows),
            "average_classification_confidence": sum(confidence_values) / len(confidence_values) if confidence_values else 0.0,
        },
        "object_type_counts": dict(sorted(type_counts.items())),
        "projects": sorted(project_rows, key=lambda row: row["project_id"]),
        "validation": validation.to_dict(),
        "sources": [
            "global/00_GLOBAL/global-project-registry.jsonl",
            "global/00_GLOBAL/global-artifact-registry.jsonl",
            "global/00_GLOBAL/global-object-registry.jsonl",
            "global/08_VALIDATION/repository-validation.json",
            "project 11_VALIDATION/cross-format/project-validation.json",
        ],
    }


def render_dashboard_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = "AEC semantic repository status"
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: light; font-family: system-ui, sans-serif; background:#f5f7fa; color:#17202a; }}
body {{ margin:0; padding:32px; max-width:1440px; margin-inline:auto; }}
.toolbar {{ display:flex; justify-content:flex-end; gap:12px; margin-bottom:24px; }}
label {{ font-size:12px; color:#52606d; }} select {{ padding:8px 28px 8px 10px; border:1px solid #cbd5e1; border-radius:6px; background:#fff; }}
.kpis {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; margin-bottom:28px; }}
.card {{ background:#fff; border:1px solid #d9e2ec; border-radius:10px; padding:16px; box-shadow:0 1px 2px #0000000d; }}
.label {{ color:#52606d; font-size:12px; }} .value {{ font-size:28px; font-weight:650; margin-top:7px; }}
section {{ margin-top:28px; }} h2 {{ font-size:18px; margin:0 0 12px; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
.bars {{ display:grid; gap:10px; }} .bar-row {{ display:grid; grid-template-columns:minmax(120px,1fr) 3fr 40px; gap:8px; align-items:center; font-size:13px; }}
.track {{ height:10px; background:#e5e7eb; border-radius:5px; overflow:hidden; }} .fill {{ height:100%; background:#2f6fed; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }} th,td {{ text-align:left; padding:10px 8px; border-bottom:1px solid #e5e7eb; }} th {{ color:#52606d; font-size:12px; }} .numeric {{ text-align:right; }}
.status {{ font-weight:600; }} .source {{ color:#52606d; font-size:12px; margin-top:20px; }}
@media(max-width:800px) {{ body {{ padding:16px; }} .kpis,.grid {{ grid-template-columns:1fr 1fr; }} .kpis .card:last-child {{ grid-column:span 2; }} }}
@media(max-width:520px) {{ .kpis,.grid {{ grid-template-columns:1fr; }} .kpis .card:last-child {{ grid-column:auto; }} .bar-row {{ grid-template-columns:90px 1fr 32px; }} }}
</style>
</head>
<body>
<h1 style="position:absolute;left:-10000px">{html.escape(title)}</h1>
<div class="toolbar"><label for="project">Project scope</label><select id="project"><option value="all">All projects</option></select></div>
<div class="kpis" id="kpis"></div>
<div class="grid"><section class="card"><h2>Object types</h2><div class="bars" id="types"></div></section>
<section class="card"><h2>Projects</h2><table><thead><tr><th>Project</th><th>Status</th><th class="numeric">Objects</th><th class="numeric">Artifacts</th><th>Validation</th></tr></thead><tbody id="projects"></tbody></table></section></div>
<div class="source" id="source"></div>
<script>
const data = {payload};
const projectSelect = document.getElementById('project');
for (const project of data.projects) {{ const option=document.createElement('option'); option.value=project.project_id; option.textContent=project.name + ' (' + project.project_id + ')'; projectSelect.appendChild(option); }}
function render() {{
  const selected=projectSelect.value;
  const rows=selected==='all'?data.projects:data.projects.filter(row=>row.project_id===selected);
  const metrics=selected==='all'?data.metrics:{{project_count:rows.length, artifact_count:rows.reduce((sum,row)=>sum+row.artifact_count,0), object_count:rows.reduce((sum,row)=>sum+row.object_count,0), validated_project_count:rows.filter(row=>row.validation_status==='SUCCESS').length, average_classification_confidence:null}};
  document.getElementById('kpis').innerHTML=[['Projects',metrics.project_count],['Artifacts',metrics.artifact_count],['Objects',metrics.object_count],['Validated projects',metrics.validated_project_count],['Average classification confidence',metrics.average_classification_confidence===null?'—':(metrics.average_classification_confidence*100).toFixed(1)+'%']].map(([label,value])=>`<div class="card"><div class="label">${{label}}</div><div class="value">${{value}}</div></div>`).join('');
  const counts={{}}; for(const row of rows) for(const [type,count] of Object.entries(row.type_counts)) counts[type]=(counts[type]||0)+count; const max=Math.max(...Object.values(counts),1);
  document.getElementById('types').innerHTML=Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([type,count])=>`<div class="bar-row"><span>${{type}}</span><span class="track"><span class="fill" style="width:${{(count/max)*100}}%"></span></span><span class="numeric">${{count}}</span></div>`).join('')||'<span>No reviewed objects</span>';
  document.getElementById('projects').innerHTML=rows.map(row=>`<tr><td>${{row.name}}<br><small>${{row.project_id}}</small></td><td class="status">${{row.status}}</td><td class="numeric">${{row.object_count}}</td><td class="numeric">${{row.artifact_count}}</td><td>${{row.validation_status}}</td></tr>`).join('');
}}
projectSelect.addEventListener('change',render); document.getElementById('source').textContent='Source-backed artifact. Generated '+data.generated_at+'. Sources: '+data.sources.join(', ')+'.'; render();
</script>
</body>
</html>'''


def write_dashboard(repository_root: str | Path, output_directory: str | Path | None = None) -> dict[str, Path]:
    root = Path(repository_root).resolve()
    output = Path(output_directory).resolve() if output_directory else root / "global" / "10_EXPORTS" / "dashboard"
    output.mkdir(parents=True, exist_ok=True)
    data = build_dashboard_data(root)
    data_path = output / "dashboard-data.json"
    html_path = output / "aec-dashboard.html"
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_dashboard_html(data), encoding="utf-8")
    return {"data": data_path, "html": html_path}
