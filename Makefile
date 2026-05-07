SH_FILES ?= $(shell file --mime-type $$(git ls-files) test/*.t | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p')
SH_SHELLCHECK_FILES ?= $(shell file --mime-type * | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p')
PY_FILES ?= $(shell git ls-files | xargs file --mime-type 2>/dev/null | grep -E 'text/x-script\.python|text/x-python' | cut -d: -f1)

ifndef CI
include .setup.mk
endif

ifndef test
test := test/
ifdef GIT_STATUS_IS_CLEAN
test += xt/
endif
endif

PROVE ?= tools/prove_wrapper
BPAN := .bpan

#------------------------------------------------------------------------------
# User targets
#------------------------------------------------------------------------------
default:

.PHONY: test
ifeq ($(CHECKSTYLE),0)
checkstyle_tests =
else
checkstyle_tests = checkstyle
endif
test: $(checkstyle_tests) test-unit

test-unit: test-bash test-python

test-bash: $(BPAN)
	"${PROVE}" -r $(if $v,-v )$(test)

test-python:
	py.test tests

test-online:
	dry_run=1 bash -x ./openqa-label-known-issues-multi < ./tests/incompletes
	dry_run=1 ./trigger-openqa_in_openqa
	# Invalid JSON causes the job to abort with an error
	-tw_openqa_host=example.com dry_run=1 ./trigger-openqa_in_openqa

checkstyle: test-shellcheck test-yaml checkstyle-python check-code-health

shfmt:
	shfmt -w ${SH_FILES}

test-shellcheck:
	@which shfmt >/dev/null 2>&1 || echo "Command 'shfmt' not found, can not execute shell script formating checks"
	shfmt -d ${SH_FILES}
	@which shellcheck >/dev/null 2>&1 || echo "Command 'shellcheck' not found, can not execute shell script checks"
	if [ -n "${SH_SHELLCHECK_FILES}" ]; then shellcheck -x ${SH_SHELLCHECK_FILES}; fi

test-yaml:
	@which yamllint >/dev/null 2>&1 || echo "Command 'yamllint' not found, can not execute YAML syntax checks"
	yamllint --strict $$(git ls-files "*.yml" "*.yaml" ":!external/")

checkstyle-python: check-ruff check-conventions check-ty
check-ruff:
	@which ruff >/dev/null 2>&1 || echo "Command 'ruff' not found, can not execute python style checks"
	@if [ -n "$(PY_FILES)" ]; then ruff format --check $(PY_FILES) && ruff check $(PY_FILES); fi

check-conventions:
	@if git grep -nE '^\s*@(unittest\.mock\.|mock\.)?patch' tests/; then \
		echo "Error: @patch decorator detected. Avoid to prevent argument ordering bugs."; \
		echo "   Fix: Use the 'mocker' fixture (pytest-mock) or a 'with patch():' context manager."; \
		exit 1; \
	fi

.PHONY: check-ty
check-ty: ## Run ty type checker
	uv run ty check

check-code-health:
	@echo "Checking code health…"
	@vulture $$(git ls-files "**.py") --min-confidence 80


.PHONY: tidy
tidy: ## Format code and fix linting issues
	ruff format $(PY_FILES)
	ruff check --fix $(PY_FILES)

update-deps:
	tools/update-deps --cpanfile cpanfile --specfile dist/rpm/os-autoinst-scripts-deps.spec

clean:
	$(RM) job_post_response
	$(RM) -r $(BPAN)
	$(RM) -r .pytest_cache/
	find . -name __pycache__ | xargs -r $(RM) -r

#------------------------------------------------------------------------------
# Internal targets
#------------------------------------------------------------------------------
$(BPAN):
	git clone https://github.com/bpan-org/bpan.git --depth 1 $@
