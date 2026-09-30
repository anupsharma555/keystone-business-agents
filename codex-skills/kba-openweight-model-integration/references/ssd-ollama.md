# SSD and Ollama setup

Read for installation, upgrades, or storage/runtime troubleshooting. Refresh
[configuration](https://docs.ollama.com/faq),
[import support](https://docs.ollama.com/import), and
[Modelfile](https://docs.ollama.com/modelfile) guidance for the installed version.

Apply the model storage policy in `SKILL.md`: all model downloads, imports,
weight caches and staging belong on the external SSD unless the user clearly
requests otherwise. The SSD paths below are the default; for an explicit exception,
record the authorized destination and apply the same path, capacity and effective
configuration checks there. Never infer an exception from a missing drive or a
tool's internal-drive default. Check browser download destinations too when using
the browser to acquire weights.

## Before downloading

1. Inspect private `.local/nemo-pilot/config.json` if present; record only relevant
   non-secret settings. Verify the configured mount with OS volume information,
   free space, and a bounded write probe. A directory under `/Volumes` is not
   proof of a mounted external device. Resolve symlinks and confirm weight paths
   stay on that volume. Do not create a missing mount point on the internal disk.
2. Snapshot prior server/app storage, cloud and launch settings, runtime version,
   tag/digest, and custom profile/template locally. Inspect both app settings and
   process configuration before changing either.
3. Estimate weights plus download/import duplication, temporary files, KV cache,
   context, and OS headroom. Total parameters determine weight storage even for
   sparse MoE models. SSD swapping cannot replace sufficient inference memory.
4. Prefer a supported native runtime on Apple Silicon; inspect actual Metal/GPU
   placement rather than assuming CPU-only operation or CUDA availability.

Use artifact byte size as the weight-memory starting estimate, then add documented
runtime/cache requirements at the intended context and measured OS/app usage.
Label unknown overhead as uncertainty; disk size alone is not evidence of fit.
An isolated server still shares physical RAM/GPU: defer a cold benchmark if active
workloads prevent adequate headroom or an interpretable timing result.

## Setup sequence

Select the transfer route from the verified artifact, not just the model name:

1. **Ollama package:** verify the exact local tag in the library, then pull it
   through the SSD-configured server. Capture its resulting full digest.
2. **Hub artifact:** inspect the exact revision/file list, download only the
   required weights and supporting files to an SSD staging directory, and set any
   Hub cache location on the SSD. Inspect the installed downloader's help before
   using revision and destination options; do not add a dependency to KBA just
   to download a file. Preserve shard names and verify completeness.
3. **Import:** first confirm this architecture/format is supported by the installed
   Ollama version. Create a local Modelfile pointing to the verified artifact,
   preserve the required template/tokenizer, and use `ollama create` with a new
   explicit tag on the SSD-configured server. Include staging plus imported blobs
   in the disk budget. Test before removing or replacing any prior profile.

Do not convert or import unsupported artifacts speculatively. A documented
alternative runtime can be proposed when Ollama support is missing.

- Configure `OLLAMA_MODELS` on the **server process** before pulling/importing.
  Setting it only on a CLI client cannot relocate a running server's storage.
  Bind a task-owned server to an unused loopback port, disable cloud features with
  `OLLAMA_NO_CLOUD=1`, and verify effective settings after startup.
  Check that the port is unused before launch; retain the new process identity
  and verify its endpoint. On collision choose another unused port, never kill
  the existing listener. Stop only the process started for this task afterward.
- For desktop use, align the app's saved model location with the verified SSD.
  Restart only when active inference is clear; inspect the UI afterward. Preserve
  prior settings for restoration.
- Pull the explicit tag, or import a supported pinned artifact with its matching
  template/tokenizer. Stage weights and conversion caches on the SSD too. Do not
  execute arbitrary repository code or enable remote code merely to load a model;
  inspect the requirement first.
- Retain a versioned previous tag/profile before replacing a mutable tag. Verify
  manifest digest, referenced blobs, resolved storage, and a bounded local answer.
  Record third-party quantization provenance and any locally generated import.
- A startup preflight must refuse a missing SSD or model. It must never pull,
  create substitute internal storage, or silently select another model. Simulate
  an unavailable path without physically removing an active drive.

## Scoped commands

From the repo root, first check whether these local pilot wrappers exist. They
may be unpublished or absent in a fresh checkout. If present, inspect them without
inference using the commands below; otherwise use the documented Ollama CLI and
create only the bounded evaluation utility needed for the requested model.

```sh
.venv/bin/python scripts/run_local_model_pilot.py --help
.venv/bin/python scripts/run_local_model_skills.py --help
.venv/bin/python scripts/build_ollama_gui_skills.py --help
ollama --version
```

Substitute the verified task-owned server port and tag below. `list`, `show`,
and `ps` inspect state; `pull` downloads and `stop` unloads:

```sh
OLLAMA_HOST=127.0.0.1:11435 ollama list
OLLAMA_HOST=127.0.0.1:11435 ollama show MODEL_TAG
OLLAMA_HOST=127.0.0.1:11435 ollama ps
# Only after download authorization and verified server storage:
OLLAMA_HOST=127.0.0.1:11435 ollama pull MODEL_TAG
# Only this task's model, after tests finish:
OLLAMA_HOST=127.0.0.1:11435 ollama stop MODEL_TAG
```

Do not execute placeholder model names. The current pilot allowlist covers two
Nemotron tags; it rejects other families until deliberately extended. Its cold
load operation may unload resident models, so use an isolated task-owned server
rather than the shared desktop endpoint.

## Restore and disconnect

Verify restoration by selecting the preserved tag/profile and checking digest
and effective configuration. Keep downloaded weights unless removal is requested.
Stop this task's inference/downloads, unload its models, quit the app when safe,
then eject through the OS. Empty `ollama ps` does not prove other apps released
that drive. Never unplug during inference or download.
