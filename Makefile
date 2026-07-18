PYTHON ?= python3
COMMON_SRC ?= ../renquant-common/src
# Publish-time peer dep (RFC RenQuant#492 §5): only the phase-3 binding
# tests need it; the package imports it lazily inside the factory.
PIPELINE_SRC ?= ../renquant-pipeline/src
export PYTHONPATH := $(COMMON_SRC):$(PIPELINE_SRC):src:$(PYTHONPATH)

.PHONY: test doctor

test:
	$(PYTHON) -m pytest -q

doctor:
	$(PYTHON) -c "from renquant_artifacts import ArtifactManifestValidationPipeline, validate_artifact_manifest; from renquant_common import Pipeline; print('renquant-artifacts ok')"
