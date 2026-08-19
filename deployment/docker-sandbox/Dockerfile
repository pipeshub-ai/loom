FROM python:3.12-slim

LABEL maintainer="loom" \
      description="Reference image for loom.runtime.sandboxes.docker.DockerSandbox -- runs a generated workflow body, nothing else"

# loomsdk IS required: generated workflow source begins `from loom import
# workflow` and the child `exec()`s it verbatim. Loom in this image carries
# no store and no credentials -- it is just the decorator/definition layer
# (`@workflow`, `@step`, `WorkflowDefinition`) the source needs to import
# successfully. pydantic is required for the same reason: generated code
# routinely types its input/output with a BaseModel.
#
# Version-pin this to the same `loomsdk` release the process constructing
# `DockerSandbox` runs -- a workflow compiled against a newer/older Loom
# than the one that executes it is a correctness gap this image cannot see,
# so keep them in lockstep via one build arg rather than letting
# `pip install loomsdk` drift to "whatever is latest today".
ARG LOOMFLOW_VERSION=""
RUN pip install --no-cache-dir --no-compile \
    pydantic \
    "loomsdk${LOOMFLOW_VERSION:+==${LOOMFLOW_VERSION}}"

# Deliberately nothing else: no other pip packages, no network tools. The
# image's own contents ARE the import surface a sandboxed body has -- a
# host bridging its own tool step into the sandbox (see `ctx_shims` on
# `DockerSandbox.__init__`) still needs no extra package here, since the
# shim only ever emits a line on the wire back to the parent.
RUN useradd --create-home --shell /usr/sbin/nologin sandbox
USER sandbox
WORKDIR /home/sandbox

# No source is baked in here and none should ever be: the child script
# arrives as the container command's own argv
# (`DockerSandbox._container_entrypoint`) and the workflow source arrives
# over the stdin bootstrap message. No `put_archive`, no volume mount, no
# host path this container ever needs to see -- safe by construction under
# Docker-in-Docker (sibling container) deployments.
