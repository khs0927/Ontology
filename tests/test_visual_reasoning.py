import io
import json

from aec_intelligence.visual_reasoning import NvidiaCosmosVision, parse_final_json, prepare_visual_input


def test_parse_final_json_discards_reasoning_trace():
    result = parse_final_json('<think>private chain</think>\n```json\n{"document_type":"floor_plan","objects":[]}\n```')
    assert result["document_type"] == "floor_plan"
    assert "private chain" not in json.dumps(result)


def test_prepare_png_without_optional_dependencies(tmp_path):
    path = tmp_path / "drawing.png"
    path.write_bytes(b"not-a-real-png-but-transport-is-byte-preserving")
    payload, mime, selected = prepare_visual_input(path)
    assert payload == path.read_bytes()
    assert mime == "image/png"
    assert selected == path


def test_nvidia_client_sends_bearer_and_image(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '<think>discard</think>{"document_type":"floor_plan","objects":[],"quality_issues":[]}'
                            }
                        }
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = request.data.decode()
        return FakeResponse()

    monkeypatch.setattr("aec_intelligence.visual_reasoning.urlrequest.urlopen", fake_urlopen)
    client = NvidiaCosmosVision(api_key="nvapi-test")
    result = client.analyze_image(b"abc", "image/png", "inspect")

    assert captured["authorization"] == "Bearer nvapi-test"
    assert "data:image/png;base64," in captured["body"]
    assert result["document_type"] == "floor_plan"
    assert "discard" not in json.dumps(result)

def test_from_env_selects_hosted_8b_and_local_2b(monkeypatch):
    monkeypatch.setenv("NVIDIA_COSMOS_ENDPOINT", "https://integrate.api.nvidia.com/v1/chat/completions")
    monkeypatch.delenv("NVIDIA_COSMOS_MODEL", raising=False)
    assert NvidiaCosmosVision.from_env().model == "nvidia/cosmos-reason2-8b"

    monkeypatch.setenv("NVIDIA_COSMOS_ENDPOINT", "http://127.0.0.1:8000/v1/chat/completions")
    assert NvidiaCosmosVision.from_env().model == "nvidia/cosmos-reason2-2b"
