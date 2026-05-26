PYTHON ?= python3
COMMON_SRC ?= ../renquant-common/src
export PYTHONPATH := $(COMMON_SRC):src:$(PYTHONPATH)

.PHONY: test doctor

test:
	$(PYTHON) -m pytest -q

doctor:
	$(PYTHON) -c "from renquant_artifacts import ArtifactManifestValidationPipeline, validate_artifact_manifest; from renquant_common import Pipeline; print('renquant-artifacts ok')"
