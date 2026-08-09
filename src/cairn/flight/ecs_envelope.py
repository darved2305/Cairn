"""Bundled jsonl-map ECS task envelope (Day 3 AWS surface).

Only the project-controlled mapper path is accepted. Generic remote workspace
execution is out of v0.1. Envelope fields: argv array, full immutable OCI
digest ref, image-baked mapper, immutable slice manifest, one output file,
minimum trusted-worker role (task role already provisioned for CRDB/S3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class EnvelopeError(ValueError):
    """Envelope refused — do not start a task."""


@dataclass(frozen=True, slots=True)
class JsonlMapEcsEnvelope:
    argv: tuple[str, ...]
    oci_image: str
    image_digest: str
    input_file: str
    id_field: str
    partitions: int
    output_file: str
    namespace_id: str
    slice_manifest_digest: str
    semantic_work_key: str | None = None
    generation: int | None = None
    claim_key: str | None = None
    fence: int | None = None

    def __post_init__(self) -> None:
        if not self.argv:
            raise EnvelopeError("argv must be a non-empty array, never a shell string")
        if "@sha256:" not in self.oci_image:
            raise EnvelopeError("oci_image must be a full immutable repo@sha256:HEX ref")
        expected = "sha256:" + self.oci_image.rsplit("@sha256:", 1)[1]
        if self.image_digest != expected:
            raise EnvelopeError("image_digest must match oci_image digest")
        if self.partitions != 64:
            raise EnvelopeError("jsonl-map/v1 partitions are fixed at 64 for the submission")
        if not self.output_file or self.output_file.startswith("/"):
            raise EnvelopeError("output_file must be a workspace-relative regular file")
        if "embed_mapper.py" not in " ".join(self.argv):
            raise EnvelopeError(
                "ECS envelope only accepts the bundled embed_mapper.py — "
                "generic remote commands are refused"
            )
        if len(self.slice_manifest_digest) != 64:
            raise EnvelopeError("slice_manifest_digest must be a measured 64-hex digest")

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract": "jsonl-map/v1",
            "argv": list(self.argv),
            "oci_image": self.oci_image,
            "image_digest": self.image_digest,
            "input_file": self.input_file,
            "id_field": self.id_field,
            "partitions": self.partitions,
            "output_file": self.output_file,
            "namespace_id": self.namespace_id,
            "slice_manifest_digest": self.slice_manifest_digest,
            "network_boundary": "supervisor_trusted_mapper",
        }
        if self.semantic_work_key is not None:
            payload["semantic_work_key"] = self.semantic_work_key
        if self.generation is not None:
            payload["generation"] = self.generation
        if self.claim_key is not None:
            payload["claim_key"] = self.claim_key
        if self.fence is not None:
            payload["fence"] = self.fence
        return payload



def cairn_exec_command_from_envelope(env: JsonlMapEcsEnvelope) -> list[str]:
    """Build the container argv for the trusted worker (array, not shell)."""
    return [
        "cairn",
        "exec",
        "--contract",
        "jsonl-map/v1",
        "--remote",
        "local",
        "--oci-image",
        env.oci_image,
        "--input-file",
        env.input_file,
        "--id-field",
        env.id_field,
        "--partitions",
        str(env.partitions),
        "--output-file",
        env.output_file,
        "--namespace",
        env.namespace_id,
        "--",
        *env.argv,
    ]
