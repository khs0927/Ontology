"""Optional SHACL validation of CAIR Turtle exports through pySHACL."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any


SHAPES_RESOURCE = "cair-shapes.ttl"


class ShaclUnavailable(RuntimeError):
    """Raised when pySHACL/RDFLib are not installed."""


@dataclass
class ShaclReport:
    conforms: bool
    violation_count: int
    violations: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"conforms": self.conforms, "violation_count": self.violation_count, "violations": self.violations}


def shapes_text() -> str:
    return files("aec_intelligence").joinpath("resources", SHAPES_RESOURCE).read_text(encoding="utf-8")


def shacl_available() -> bool:
    try:
        import pyshacl  # noqa: F401
        import rdflib  # noqa: F401
    except ImportError:
        return False
    return True


def validate_turtle(data: str, *, max_violations: int = 20) -> ShaclReport:
    try:
        import rdflib
        from pyshacl import validate
    except ImportError as exc:
        raise ShaclUnavailable("install the 'shacl' extra (pyshacl) to run SHACL validation") from exc
    data_graph = rdflib.Graph().parse(data=data, format="turtle")
    shapes_graph = rdflib.Graph().parse(data=shapes_text(), format="turtle")
    conforms, results_graph, _ = validate(data_graph, shacl_graph=shapes_graph, inference="none")
    sh = rdflib.Namespace("http://www.w3.org/ns/shacl#")
    violations = []
    for result in results_graph.subjects(rdflib.RDF.type, sh.ValidationResult):
        violations.append({
            "focus_node": str(results_graph.value(result, sh.focusNode)),
            "path": str(results_graph.value(result, sh.resultPath)),
            "message": str(results_graph.value(result, sh.resultMessage)),
        })
    violations.sort(key=lambda row: (row["focus_node"], row["path"], row["message"]))
    return ShaclReport(bool(conforms), len(violations), violations[:max_violations])


def validate_turtle_file(path: str | Path, *, max_violations: int = 20) -> ShaclReport:
    return validate_turtle(Path(path).read_text(encoding="utf-8"), max_violations=max_violations)
